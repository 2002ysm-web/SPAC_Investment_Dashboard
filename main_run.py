import io
import json
import math
import os
import re
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# =========================
# 환경변수 및 기본 설정
# =========================
DART_API_KEY = os.environ.get("DART_API_KEY", "").strip()
KRX_OPENAPI_KEY = os.environ.get("KRX_OPENAPI_KEY", "").strip()
ECOS_API_KEY = os.environ.get("ECOS_API_KEY", "").strip()

OUTPUT_PATH = Path(os.environ.get("SPACS_OUTPUT_PATH", "data/spacs.json"))
LIQUIDATIONS_PATH = Path(os.environ.get("LIQUIDATIONS_OUTPUT_PATH", "data/liquidations.json"))
DEPOSIT_RATE_PATH = Path(os.environ.get("DEPOSIT_RATE_PATH", "data/deposit_rates.json"))

EXCLUDE_DELISTING_NOTICE = os.environ.get("EXCLUDE_DELISTING_NOTICE", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
}

TARGET_SPAC_COUNT = int(os.environ.get("TARGET_SPAC_COUNT", "65"))
EXCLUDED_SPAC_CODES: set[str] = set()

KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
    "Origin": "https://data.krx.co.kr",
}

GENERAL_TAX_RATE = 0.154
DEPOSIT_INTEREST_TAX_RATE = 0.154
DEPOSIT_AFTER_TAX_FACTOR = 1.0 - DEPOSIT_INTEREST_TAX_RATE
FINAL_YEAR_TAX_FACTOR = 0.845
FINAL_YEAR_MONTH_RATIO = 6.0 / 12.0
MERGER_THRESHOLD_DAYS = 365
DEFAULT_SPAC_IPO_PRICE = 2000

DEFAULT_DEPOSIT_RATES: dict[str, float] = {
    "2024-01": 3.67, "2024-02": 3.63, "2024-03": 3.58, "2024-04": 3.53,
    "2024-05": 3.55, "2024-06": 3.51, "2024-07": 3.41, "2024-08": 3.35,
    "2024-09": 3.40, "2024-10": 3.37, "2024-11": 3.35, "2024-12": 3.22,
    "2025-01": 3.07, "2025-02": 2.97, "2025-03": 2.84, "2025-04": 2.71,
    "2025-05": 2.63, "2025-06": 2.58, "2025-07": 2.55, "2025-08": 2.52,
    "2025-09": 2.50, "2025-10": 2.48, "2025-11": 2.45, "2025-12": 2.45,
    "2026-01": 2.42, "2026-02": 2.40, "2026-03": 2.40, "2026-04": 2.38,
    "2026-05": 2.38, "2026-06": 2.35, "2026-07": 2.35, "2026-08": 2.35,
    "2026-09": 2.35, "2026-10": 2.35, "2026-11": 2.35,
}


# =========================
# 공통 유틸리티
# =========================
def load_existing_items(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data, list):
            return data
    except Exception as exc:
        print(f"[WARN] 기존 {path} 읽기 실패: {exc}")
    return []


def normalize_code(val: Any) -> str:
    raw = str(val or "").strip()
    if raw.endswith(".0"):
        raw = raw[:-2]
    digits = re.sub(r"[^0-9A-Za-z]", "", raw)
    if len(digits) > 6:
        digits = digits[-6:]
    return digits.zfill(6) if digits else ""


def normalize_name(name: Any) -> str:
    s = re.sub(r"\s+", "", str(name or ""))
    s = re.sub(r"^KB", "케이비", s, flags=re.IGNORECASE)
    s = re.sub(r"^IBKS", "아이비케이에스", s, flags=re.IGNORECASE)
    return s.lower()


def parse_krw_amount(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(round(val)) if math.isfinite(float(val)) else None
    s = str(val).replace(",", "").replace("\u3000", " ").strip()
    if not s or "%" in s or s in {"-", "미확인", "N/A", "null", "None"}:
        return None

    total = 0.0
    matched_unit = False
    for pat, mult in (
        (r"([0-9]+(?:\.[0-9]+)?)\s*조", 1_000_000_000_000.0),
        (r"([0-9]+(?:\.[0-9]+)?)\s*억", 100_000_000.0),
        (r"([0-9]+(?:\.[0-9]+)?)\s*천만", 10_000_000.0),
        (r"([0-9]+(?:\.[0-9]+)?)\s*백만", 1_000_000.0),
        (r"([0-9]+(?:\.[0-9]+)?)\s*만", 10_000.0),
    ):
        for m in re.finditer(pat, s):
            matched_unit = True
            total += float(m.group(1)) * mult

    if matched_unit:
        m_tail = re.search(r"억\s*([1-9][0-9]{2,7})\s*(?:원)?(?![0-9]*(?:조|억|만|%))", s)
        if m_tail:
            tail_val = float(m_tail.group(1))
            if tail_val < 100_000_000:
                total += tail_val
        return int(round(total)) if total > 0 else None

    digits = re.sub(r"[^0-9.\-]", "", s)
    if not digits or digits in {"-", "."}:
        return None
    try:
        num = float(digits)
        return int(round(num)) if math.isfinite(num) else None
    except ValueError:
        return None


def parse_share_count(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(round(val)) if math.isfinite(float(val)) and val > 0 else None
    s = str(val).replace(",", "").replace("\u3000", " ").strip()
    if not s or "%" in s or s in {"-", "미확인", "N/A", "null", "None"}:
        return None

    m_man = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*만\s*주?$", s)
    if m_man:
        return int(round(float(m_man.group(1)) * 10_000))

    m_cheon = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*천\s*주?$", s)
    if m_cheon:
        return int(round(float(m_cheon.group(1)) * 1_000))

    digits = re.sub(r"[^0-9]", "", s)
    if not digits:
        return None
    try:
        num = int(digits)
        return num if num > 0 else None
    except ValueError:
        return None


def parse_krw_int(val: Any) -> int:
    parsed = parse_krw_amount(val)
    return parsed if parsed is not None else 0


def parse_krw_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(float(val)) else None
    s = re.sub(r"[,%\s원억만]|%p", "", str(val).replace("\u3000", " ")).strip()
    if not s or s in {"-", "미확인", "N/A", "null", "None"}:
        return None
    try:
        num = float(s)
        return num if math.isfinite(num) else None
    except ValueError:
        return None


def parse_date_yyyymmdd(date_str: Any) -> datetime | None:
    if date_str is None:
        return None
    s = str(date_str).strip()
    if not s or s in {"-", "미확인", "N/A", "null", "None"}:
        return None
    m = re.match(r"^(\d{4})[./-]?(\d{2})[./-]?(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    digits = re.sub(r"[^0-9]", "", s)
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d")
        except ValueError:
            return None
    return None


def add_years_safe(dt: datetime, years: int) -> datetime:
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:
        return dt.replace(year=dt.year + years, month=2, day=28)


def normalize_krx_dept(dept_val: Any) -> str:
    return re.sub(r"\s+", "", str(dept_val or "").strip()).upper()


def is_krx_spac_dept(dept_val: Any) -> bool:
    """KRX 종목정보 소속부가 SPAC(소속부 없음)에 해당하는 종목만 True를 반환합니다."""
    norm_dept = normalize_krx_dept(dept_val)
    return norm_dept in {"SPAC(소속부없음)", "SPAC"} or norm_dept.startswith("SPAC(")


def sanitize_spac_ipo_price(val: Any) -> int | None:
    """일반 스팩 공모가(1,000~5,000원) 범위 외 값(전환·행사가 등 오파싱 값)을 걸러냅니다."""
    p = parse_krw_amount(val)
    if p is not None and 1000 <= p <= 5000:
        return p
    return None


# =========================
# 1. 한국은행(ECOS) 기준 예금금리
# =========================
def to_after_tax_rate(gross_rate: float) -> float:
    return round(float(gross_rate) * DEPOSIT_AFTER_TAX_FACTOR, 4)


def fetch_ecos_deposit_rate(ref_dt: datetime) -> tuple[str | None, float | None, dict[str, float]]:
    fetched_map: dict[str, float] = {}
    start_ym = "202401"
    end_ym = ref_dt.strftime("%Y%m")

    api_key = ECOS_API_KEY or "sample"
    urls = [
        f"https://ecos.bok.or.kr/api/StatisticSearch/{api_key}/json/kr/1/200/121Y002/M/{start_ym}/{end_ym}/010200000",
    ]
    if ECOS_API_KEY:
        urls.append(
            f"https://ecos.bok.or.kr/api/StatisticSearch/sample/json/kr/1/200/121Y002/M/{start_ym}/{end_ym}/010200000"
        )

    for url in urls:
        try:
            res = requests.get(url, timeout=15)
            res.raise_for_status()
            payload = res.json()
            rows = (payload.get("StatisticSearch") or {}).get("row") or []
            valid_rows: list[tuple[str, float]] = []
            for r in rows:
                ym_raw = str(r.get("TIME") or "").strip()
                val = parse_krw_float(r.get("DATA_VALUE"))
                if len(ym_raw) == 6 and val is not None and val > 0:
                    ym_fmt = f"{ym_raw[:4]}-{ym_raw[4:6]}"
                    fetched_map[ym_fmt] = round(float(val), 4)
                    valid_rows.append((ym_fmt, float(val)))
            if valid_rows:
                valid_rows.sort(key=lambda x: x[0])
                print(
                    f"[INFO] ECOS 정기예금 금리 자동 갱신: 최신={valid_rows[-1][0]} "
                    f"({valid_rows[-1][1]}%), {len(valid_rows)}개월 확보"
                )
                return valid_rows[-1][0], valid_rows[-1][1], fetched_map
        except Exception:
            continue

    print("[WARN] ECOS 금리 자동 조회 실패 — 저장된/기본 월별 금리 테이블 사용")
    return None, None, fetched_map


def get_base_deposit_rate(ref_dt: datetime | None = None) -> tuple[float, float, dict[str, float]]:
    now_ref = ref_dt or datetime.now(KST)
    rates_map: dict[str, float] = dict(DEFAULT_DEPOSIT_RATES)
    last_ecos_sync: str | None = None

    if DEPOSIT_RATE_PATH.exists():
        try:
            with DEPOSIT_RATE_PATH.open("r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                last_ecos_sync = saved.get("last_ecos_sync")
                if isinstance(saved.get("rates"), dict):
                    for k, v in saved["rates"].items():
                        parsed = parse_krw_float(v)
                        if parsed is not None and parsed > 0:
                            rates_map[str(k)] = float(parsed)
        except Exception as exc:
            print(f"[WARN] 기준 예금금리 읽기 실패: {exc}")

    ecos_ym, ecos_rate, ecos_map = fetch_ecos_deposit_rate(now_ref)
    if ecos_map:
        rates_map.update(ecos_map)
        last_ecos_sync = now_ref.strftime("%Y-%m-%d %H:%M:%S KST")

    sorted_keys = sorted(rates_map.keys())
    latest_ym = sorted_keys[-1]
    gross_rate = ecos_rate if (ecos_ym and ecos_rate is not None) else rates_map[latest_ym]

    try:
        DEPOSIT_RATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DEPOSIT_RATE_PATH.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "updated_at": now_ref.strftime("%Y-%m-%d %H:%M:%S KST"),
                    "last_ecos_sync": last_ecos_sync,
                    "latest_month": ecos_ym or latest_ym,
                    "source": "BOK_ECOS_121Y002_010200000",
                    "rates": dict(sorted(rates_map.items())),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as exc:
        print(f"[WARN] deposit_rates.json 저장 실패: {exc}")

    after_tax_rate = to_after_tax_rate(gross_rate)
    return after_tax_rate, round(gross_rate, 4), dict(sorted(rates_map.items()))


# =========================
# 2. KRX API 연동 (소속부 SPAC(소속부 없음) 필터링)
# =========================
def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("OutBlock_1", "outBlock_1", "output", "data", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
    return []


def _fetch_krx_openapi_rows(endpoint: str, bas_dd: str) -> list[dict[str, Any]]:
    if not KRX_OPENAPI_KEY:
        return []
    bases = (
        "https://data-dbg.krx.co.kr/svc/apis",
        "https://openapi.krx.co.kr/svc/apis",
    )
    api_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "AUTH_KEY": KRX_OPENAPI_KEY,
        "Accept": "application/json",
    }
    for base in bases:
        url = f"{base}/{endpoint}"
        for method in ("GET", "POST"):
            try:
                if method == "GET":
                    res = requests.get(url, params={"basDd": bas_dd}, headers=api_headers, timeout=12)
                else:
                    res = requests.post(url, json={"basDd": bas_dd}, headers=api_headers, timeout=12)
                if res.status_code != 200:
                    continue
                rows = _extract_rows(res.json())
                if rows:
                    return rows
            except Exception:
                continue
    return []


def _fetch_krx_web_bld_rows(
    session: requests.Session,
    bld: str,
    bas_dd: str,
    mkt_id: str = "KSQ",
) -> list[dict[str, Any]]:
    endpoints = (
        "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
        "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
    )
    payload = {
        "bld": bld,
        "locale": "ko_KR",
        "mktId": mkt_id,
        "trdDd": bas_dd,
        "share": "1",
        "money": "1",
        "csvxls_isNo": "false",
    }
    for url in endpoints:
        try:
            res = session.post(url, data=payload, headers=HEADERS, timeout=15)
            if res.status_code != 200:
                continue
            rows = _extract_rows(res.json())
            if rows:
                return rows
        except Exception:
            continue
    return []


def _merge_quote_rows(
    rows: list[dict[str, Any]],
    info_by_code: dict[str, dict[str, Any]],
    quote_by_code: dict[str, dict[str, Any]],
    fallback_date: str,
    market_label: str = "",
    overwrite: bool = False,
) -> None:
    for r in rows:
        code = normalize_code(
            r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("ISU_CD") or r.get("code")
        )
        if not re.fullmatch(r"[0-9A-Za-z]{6}", code):
            continue
        if not overwrite and code in quote_by_code:
            continue

        info = info_by_code.get(code, {})
        dept = str(
            info.get("SECT_TP_NM")
            or info.get("소속부")
            or r.get("SECT_TP_NM")
            or r.get("소속부")
            or ""
        ).strip()
        name = str(
            r.get("ISU_ABBRV")
            or r.get("ISU_NM")
            or r.get("종목명")
            or r.get("회사명")
            or info.get("ISU_ABBRV")
            or info.get("ISU_NM")
            or info.get("종목명")
            or ""
        ).strip()
        close_price = parse_krw_int(
            r.get("TDD_CLSPRC") or r.get("종가") or r.get("CLSPRC") or r.get("close_price")
        )
        open_price = parse_krw_int(r.get("TDD_OPNPRC") or r.get("시가"))
        high_price = parse_krw_int(r.get("TDD_HGPRC") or r.get("고가"))
        low_price = parse_krw_int(r.get("TDD_LWPRC") or r.get("저가"))
        prev_diff = parse_krw_int(r.get("CMPPREVDD_PRC") or r.get("대비"))
        chg_rate = parse_krw_float(r.get("FLUC_RT") or r.get("등락률"))
        volume = parse_krw_int(r.get("ACC_TRDVOL") or r.get("거래량"))
        trading_value = parse_krw_int(r.get("ACC_TRDVAL") or r.get("거래대금"))
        market_cap = parse_krw_int(r.get("MKTCAP") or r.get("시가총액"))
        listed_shares = parse_krw_int(
            info.get("LIST_SHRS") or r.get("LIST_SHRS") or r.get("상장주식수")
        )
        par_val = parse_krw_int(
            info.get("PARVAL") or r.get("PARVAL") or r.get("액면가")
        )
        list_dd = str(
            info.get("LIST_DD") or r.get("LIST_DD") or r.get("상장일") or ""
        ).strip()
        mkt = market_label or str(r.get("MKT_NM") or info.get("MKT_NM") or "KOSDAQ").strip()

        quote_by_code[code] = {
            "name": name,
            "dept": dept,
            "market": mkt,
            "close_price": close_price,
            "open_price": open_price,
            "high_price": high_price,
            "low_price": low_price,
            "prev_diff": prev_diff,
            "chg_rate": chg_rate,
            "volume": volume,
            "trading_value": trading_value,
            "market_cap": market_cap,
            "listed_shares": listed_shares if listed_shares > 0 else None,
            "par_value": par_val if par_val > 0 else None,
            "listing_date": list_dd,
            "trade_date": fallback_date,
        }


def get_krx_spac_market_data(
    ref_dt: datetime,
    prev_by_code: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict], dict[str, dict[str, Any]], str, int]:
    candidate_dates = [
        (ref_dt - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(0, 12)
    ]

    prev_by_code = prev_by_code or {}
    info_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    web_ksq_rows: list[dict[str, Any]] = []
    web_stk_rows: list[dict[str, Any]] = []
    used_date = ""

    if KRX_OPENAPI_KEY:
        for d_str in candidate_dates:
            t_rows = _fetch_krx_openapi_rows("sto/ksq_bydd_trd", d_str)
            if t_rows:
                trade_rows = t_rows
                used_date = d_str
                for info_d in ([d_str] + [x for x in candidate_dates if x != d_str]):
                    i_rows = _fetch_krx_openapi_rows("sto/ksq_isu_base_info", info_d)
                    if i_rows:
                        info_rows = i_rows
                        break
                break

    session = requests.Session()
    try:
        session.get(
            "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020101",
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=12,
        )
    except Exception:
        pass

    target_web_dates = ([used_date] if used_date else []) + [
        d for d in candidate_dates if d != used_date
    ]
    for d_str in target_web_dates:
        ksq_rows = _fetch_krx_web_bld_rows(session, "dbms/MDC/STAT/standard/MDCSTAT01501", d_str, "KSQ")
        stk_rows = _fetch_krx_web_bld_rows(session, "dbms/MDC/STAT/standard/MDCSTAT01501", d_str, "STK")
        if ksq_rows or stk_rows:
            web_ksq_rows = ksq_rows
            web_stk_rows = stk_rows
            if not used_date:
                used_date = d_str
            break

    base_info_map: dict[str, dict[str, Any]] = {}
    if used_date:
        for mkt_id in ("KSQ", "STK", "ALL"):
            base_rows = _fetch_krx_web_bld_rows(
                session, "dbms/MDC/STAT/standard/MDCSTAT01901", used_date, mkt_id
            )
            for r in base_rows:
                c = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드"))
                if not c:
                    continue
                prev_b = base_info_map.get(c, {})
                dept_val = str(r.get("SECT_TP_NM") or r.get("소속부") or prev_b.get("SECT_TP_NM") or "").strip()
                base_info_map[c] = {
                    "SECT_TP_NM": dept_val,
                    "LIST_DD": str(r.get("LIST_DD") or r.get("상장일") or prev_b.get("LIST_DD") or "").strip(),
                    "LIST_SHRS": parse_krw_int(r.get("LIST_SHRS") or r.get("상장주식수")) or prev_b.get("LIST_SHRS", 0),
                    "PARVAL": parse_krw_int(r.get("PARVAL") or r.get("액면가")) or prev_b.get("PARVAL", 0),
                }

    info_by_code: dict[str, dict[str, Any]] = {}
    for c, b_info in base_info_map.items():
        info_by_code[c] = dict(b_info)
    for r in info_rows:
        code = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("ISU_CD") or r.get("code"))
        if re.fullmatch(r"[0-9A-Za-z]{6}", code):
            merged = dict(info_by_code.get(code, {}))
            merged.update(r)
            info_by_code[code] = merged

    quote_by_code: dict[str, dict[str, Any]] = {}
    _merge_quote_rows(trade_rows, info_by_code, quote_by_code, used_date, market_label="KOSDAQ", overwrite=True)
    _merge_quote_rows(web_ksq_rows, info_by_code, quote_by_code, used_date, market_label="KOSDAQ", overwrite=False)
    _merge_quote_rows(web_stk_rows, info_by_code, quote_by_code, used_date, market_label="KOSPI", overwrite=False)

    all_codes: list[str] = []
    seen_all_codes: set[str] = set()
    for src in (base_info_map.values(), info_rows, trade_rows, web_ksq_rows, web_stk_rows):
        for r in src:
            c = normalize_code(
                r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("ISU_CD") or r.get("code")
            )
            if re.fullmatch(r"[0-9A-Za-z]{6}", c) and c not in seen_all_codes:
                seen_all_codes.add(c)
                all_codes.append(c)

    spacs: list[dict] = []
    seen_codes: set[str] = set()
    dept_matched_raw = 0

    for code in all_codes:
        if code in seen_codes or code in EXCLUDED_SPAC_CODES:
            continue
        info = info_by_code.get(code, {})
        q_info = quote_by_code.get(code, {})

        dept = str(
            info.get("SECT_TP_NM")
            or info.get("소속부")
            or q_info.get("dept")
            or ""
        ).strip()

        name = str(
            info.get("ISU_ABBRV")
            or info.get("ISU_NM")
            or info.get("종목명")
            or info.get("회사명")
            or q_info.get("name")
            or ""
        ).strip()
        if not name:
            continue

        if not is_krx_spac_dept(dept):
            continue
        dept_matched_raw += 1

        raw_list_dd = str(
            info.get("LIST_DD")
            or q_info.get("listing_date")
            or ""
        ).strip()
        dt_list = parse_date_yyyymmdd(raw_list_dd)
        listing_date = dt_list.strftime("%Y-%m-%d") if dt_list else raw_list_dd

        krx_listed_shares = parse_krw_int(info.get("LIST_SHRS") or q_info.get("listed_shares"))
        krx_par_value = parse_krw_int(info.get("PARVAL") or q_info.get("par_value"))

        seen_codes.add(code)
        spacs.append({
            "code": code,
            "name": name,
            "market": q_info.get("market") or "KOSDAQ",
            "listing_date": listing_date,
            "krx_dept": dept,
            "krx_listed_shares": krx_listed_shares if krx_listed_shares > 0 else None,
            "krx_par_value": krx_par_value if krx_par_value > 0 else None,
        })

    print(
        f"[INFO] KRX 기준일={used_date or '미확인'}, 소속부 SPAC(소속부 없음)={len(spacs)}개 "
        f"(목표={TARGET_SPAC_COUNT}개, 전체KRX종목={len(all_codes)}개)"
    )
    return spacs, quote_by_code, used_date, dept_matched_raw


def get_krx_quote_and_base(
    code: str,
    quote_by_code: dict[str, dict[str, Any]],
) -> tuple[int, bool, dict[str, Any]]:
    q = quote_by_code.get(code, {})
    current_price = parse_krw_int(q.get("close_price"))
    volume = parse_krw_int(q.get("volume"))
    is_suspended = current_price <= 0

    market_meta = {
        "open_price": parse_krw_int(q.get("open_price")),
        "high_price": parse_krw_int(q.get("high_price")),
        "low_price": parse_krw_int(q.get("low_price")),
        "prev_diff": parse_krw_int(q.get("prev_diff")),
        "chg_rate": q.get("chg_rate"),
        "volume": volume,
        "trading_value": parse_krw_int(q.get("trading_value")),
        "market_cap": parse_krw_int(q.get("market_cap")),
        "listed_shares": q.get("listed_shares"),
        "par_value": q.get("par_value"),
        "trade_date": q.get("trade_date", ""),
    }
    return current_price, is_suspended, market_meta


# =========================
# 3. OpenDART 공시/투자설명서/예치신탁 변경 파싱
# =========================
def get_dart_corp_codes() -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not DART_API_KEY:
        print("[INFO] DART_API_KEY 미설정 — 공시 조회 생략")
        return mapping

    url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={DART_API_KEY}"
    try:
        res = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=25)
        res.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            xml_name = zf.namelist()[0]
            with zf.open(xml_name) as xml_file:
                root = ET.parse(xml_file).getroot()
                for item in root.findall("list"):
                    stock_code = (item.findtext("stock_code") or "").strip()
                    corp_code = (item.findtext("corp_code") or "").strip()
                    if stock_code and corp_code:
                        mapping[stock_code.zfill(6)] = corp_code
        print(f"[INFO] DART 고유번호 {len(mapping)}개 로드")
    except Exception as exc:
        print(f"[WARN] DART corpCode 조회 실패: {exc}")
    return mapping


def get_recent_dart_disclosures(corp_code: str, days: int = 1460) -> list[dict]:
    if not DART_API_KEY or not corp_code:
        return []
    now_kst = datetime.now(KST)
    bgn_de = (now_kst - timedelta(days=days)).strftime("%Y%m%d")
    end_de = now_kst.strftime("%Y%m%d")
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "page_count": 100,
            },
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=10,
        )
        res.raise_for_status()
        data = res.json()
        if data.get("status") == "000":
            return [
                {
                    "report_nm": entry.get("report_nm", ""),
                    "rcept_dt": entry.get("rcept_dt", ""),
                    "rcept_no": entry.get("rcept_no", ""),
                }
                for entry in data.get("list", [])
            ]
    except Exception as exc:
        print(f"[WARN] [{corp_code}] DART 공시 조회 실패: {exc}")
    return []


def clean_html_text(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"</(?:td|th)\s*>", " | ", text, flags=re.IGNORECASE)
    clean = re.sub(r"</?(?:tr|p|div|li|br)\b[^>]*>", " ", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    replacements = {
        "&nbsp;": " ",
        "&#160;": " ",
        "&middot;": "·",
        "&#183;": "·",
        "&bull;": "•",
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "\u3000": " ",
        "\xa0": " ",
    }
    for old, new in replacements.items():
        clean = clean.replace(old, new)
    clean = re.sub(r"&#[0-9]+;", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _decode_doc_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("euc-kr", errors="ignore")


def fetch_document_text(rcept_no: str, max_files: int = 80, keywords: tuple[str, ...] = ()) -> str:
    if not DART_API_KEY or not rcept_no:
        return ""
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=18,
        )
        res.raise_for_status()
        if not res.content.startswith(b"PK"):
            return ""

        all_chunks: list[str] = []
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            doc_members = sorted(
                m for m in zf.namelist()
                if m.lower().endswith((".xml", ".html", ".htm"))
            )
            for member in doc_members[:max_files]:
                raw = zf.read(member)
                txt = clean_html_text(_decode_doc_bytes(raw))
                if txt:
                    all_chunks.append(txt)

        return " ".join(all_chunks)
    except Exception:
        return ""


def _parse_rate_value(raw_str: Any) -> float | None:
    val = parse_krw_float(raw_str)
    if val is not None and 0.1 <= val <= 15.0:
        return round(float(val), 4)
    return None


def parse_prospectus_details(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ipo_price": None,
        "ipo_shares": None,
        "ipo_amount": None,
        "deposit_ratio": None,
        "deposit_principal": None,
        "year1_rate": None,
    }
    if not text:
        return out

    price_patterns = [
        r"(?:1주당\s*모집가액|모집가액|공모가액|공모가격|확정\s*공모가|공모가|발행가액)[^0-9]{0,20}([1-9][0-9,]{2,4})\s*원",
        r"주당\s*(?:공모가|발행가|모집가)[^0-9]{0,20}([1-9][0-9,]{2,4})\s*원",
    ]
    for pat in price_patterns:
        for m_price in re.finditer(pat, text):
            p_val = sanitize_spac_ipo_price(m_price.group(1))
            if p_val is not None:
                out["ipo_price"] = p_val
                break
        if out["ipo_price"] is not None:
            break

    shares_patterns = [
        r"(?:총\s*공모주식수|모집주식수|공모주식수|모집할\s*주식수|발행주식수)[^0-9]{0,20}([0-9][0-9,.]*(?:\s*(?:만|천))?\s*주)",
    ]
    for pat in shares_patterns:
        m_shares = re.search(pat, text)
        if m_shares:
            s_val = parse_share_count(m_shares.group(1))
            if s_val is not None and s_val > 0:
                out["ipo_shares"] = s_val
                break

    amount_patterns = [
        r"(?:총\s*공모금액|모집총액|공모금액|공모총액|모집\(매출\)총액)[^0-9]{0,20}([0-9][0-9,.]*(?:\s*(?:억|천만|백만|만))*\s*원?)",
    ]
    for pat in amount_patterns:
        m_amt = re.search(pat, text)
        if m_amt:
            a_val = parse_krw_amount(m_amt.group(1))
            if a_val is not None and a_val >= 100_000_000:
                out["ipo_amount"] = a_val
                break

    if out["ipo_amount"] is None and out["ipo_price"] and out["ipo_shares"]:
        out["ipo_amount"] = out["ipo_price"] * out["ipo_shares"]
    elif out["ipo_shares"] is None and out["ipo_amount"] and out["ipo_price"]:
        out["ipo_shares"] = round(out["ipo_amount"] / out["ipo_price"])
    elif out["ipo_price"] is None and out["ipo_amount"] and out["ipo_shares"]:
        cand_price = sanitize_spac_ipo_price(round(out["ipo_amount"] / out["ipo_shares"]))
        if cand_price is not None:
            out["ipo_price"] = cand_price

    m_ratio = re.search(
        r"(?:예치비율|예치\s*비율|신탁\s*비율|공모자금\s*예치)[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*%",
        text,
    )
    if m_ratio:
        r_val = parse_krw_float(m_ratio.group(1))
        if r_val is not None and 50.0 <= r_val <= 105.0:
            out["deposit_ratio"] = round(float(r_val), 2)

    m_dep_amt = re.search(
        r"(?:예치금액|신탁금액|예치원금)[^0-9]{0,20}([0-9][0-9,.]*(?:\s*(?:억|천만|백만|만))*\s*원?)",
        text,
    )
    if m_dep_amt:
        d_val = parse_krw_amount(m_dep_amt.group(1))
        if d_val is not None and d_val >= 10_000_000:
            out["deposit_principal"] = d_val
            if out["deposit_ratio"] is None and out["ipo_amount"]:
                calc_r = round((d_val / out["ipo_amount"]) * 100.0, 2)
                if 50.0 <= calc_r <= 105.0:
                    out["deposit_ratio"] = calc_r

    if out["deposit_principal"] is None and out["ipo_amount"] and out["deposit_ratio"] is not None:
        out["deposit_principal"] = round(out["ipo_amount"] * (out["deposit_ratio"] / 100.0))

    for m_sec in re.finditer(r"1주당\s*지급\s*예상금액", text):
        snippet = text[m_sec.start(): m_sec.start() + 1200]
        for m_r in re.finditer(
            r"(?:이자율|이율|금리|예치금\s*이율|신탁\s*이율|연\s*이율|연\s*이자율)[^%\d]{0,45}(?:연\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
            snippet,
        ):
            rv = _parse_rate_value(m_r.group(1))
            if rv is not None:
                out["year1_rate"] = rv
                break
        if out["year1_rate"] is not None:
            break
        for m_r2 in re.finditer(r"연\s*([0-9]+(?:\.[0-9]+)?)\s*%", snippet):
            rv = _parse_rate_value(m_r2.group(1))
            if rv is not None:
                out["year1_rate"] = rv
                break
        if out["year1_rate"] is not None:
            break

    if out["year1_rate"] is None:
        for m_prot in re.finditer(r"투자자보호를\s*위해\s*필요한\s*사항", text):
            snippet = text[m_prot.start(): m_prot.start() + 2500]
            for m_r in re.finditer(
                r"(?:이자율|이율|금리|예치|신탁)[^%\d]{0,45}(?:연\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
                snippet,
            ):
                rv = _parse_rate_value(m_r.group(1))
                if rv is not None:
                    out["year1_rate"] = rv
                    break
            if out["year1_rate"] is not None:
                break

    if out["year1_rate"] is None:
        fallback_patterns = [
            r"(?:1년차|1차년도|최초\s*1년|1년\s*예치)[^%\d]{0,35}(?:연\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
            r"(?:예치이자율|예치이율|신탁이자율|예치금리|약정이자율)[^%\d]{0,30}(?:연\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
        ]
        for pat in fallback_patterns:
            m_y1 = re.search(pat, text)
            if m_y1:
                rv = _parse_rate_value(m_y1.group(1))
                if rv is not None:
                    out["year1_rate"] = rv
                    break

    return out


def parse_trust_amendment_single_doc(text: str) -> tuple[float | None, float | None, dict[int, float]]:
    prev_rate: float | None = None
    next_rate: float | None = None
    explicit_years: dict[int, float] = {}
    if not text:
        return prev_rate, next_rate, explicit_years

    m_pair = re.search(
        r"변경\s*전\s*\|\s*변경\s*후.{0,300}?(?:이자율|이율|금리)[^%\d]{0,45}(?:연\s*)?([0-9]+(?:\.[0-9]+)?)\s*%\s*\|\s*(?:연\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
        text,
    )
    if m_pair:
        prev_rate = _parse_rate_value(m_pair.group(1))
        next_rate = _parse_rate_value(m_pair.group(2))

    if prev_rate is None:
        m_p = re.search(
            r"변경\s*전(?:(?!변경\s*후)[^%\d]){0,45}(?:연\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
            text,
        )
        if m_p:
            prev_rate = _parse_rate_value(m_p.group(1))

    if next_rate is None:
        m_n = re.search(
            r"변경\s*후(?:(?!변경\s*전)[^%\d]){0,45}(?:연\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
            text,
        )
        if m_n:
            next_rate = _parse_rate_value(m_n.group(1))

    for yr in (1, 2, 3):
        other = "|".join(str(y) for y in (1, 2, 3) if y != yr)
        m_yr = re.search(
            rf"(?:{yr}년차|{yr}차년도|제{yr}차)(?:(?!(?:{other})년차|변경\s*전|변경\s*후)[^%\d]){{0,35}}(?:연\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
            text,
        )
        if m_yr:
            vy = _parse_rate_value(m_yr.group(1))
            if vy is not None:
                explicit_years[yr] = vy

    return prev_rate, next_rate, explicit_years


def extract_dart_spac_terms(disclosures: list[dict[str, Any]]) -> dict[str, Any]:
    terms: dict[str, Any] = {
        "ipo_price": None,
        "ipo_shares": None,
        "ipo_amount": None,
        "deposit_ratio": None,
        "deposit_principal": None,
        "prospectus_year1_rate": None,
        "amendment_year1_rate": None,
        "year1_deposit_rate": None,
        "year2_deposit_rate": None,
        "year3_deposit_rate": None,
        "yearly_deposit_rates": [None, None, None],
        "prospectus_report": None,
        "trust_amendment_reports": [],
    }
    if not disclosures or not DART_API_KEY:
        return terms

    offering_docs = [
        d for d in disclosures
        if d.get("rcept_no")
        and "예비" not in str(d.get("report_nm") or "")
        and any(k in str(d.get("report_nm") or "") for k in ("투자설명서", "증권신고서"))
    ]
    offering_docs.sort(
        key=lambda x: (
            1 if "투자설명서" in str(x.get("report_nm") or "") else 0,
            str(x.get("rcept_dt") or ""),
        ),
        reverse=True,
    )

    for doc in offering_docs[:3]:
        text = fetch_document_text(doc["rcept_no"], max_files=80)
        parsed = parse_prospectus_details(text)
        if not terms["prospectus_report"] and any(
            parsed.get(k) is not None for k in ("ipo_price", "ipo_shares", "ipo_amount", "year1_rate")
        ):
            terms["prospectus_report"] = doc.get("report_nm")
        for k in ("ipo_price", "ipo_shares", "ipo_amount", "deposit_ratio", "deposit_principal"):
            if terms.get(k) is None and parsed.get(k) is not None:
                terms[k] = parsed[k]
        if terms["prospectus_year1_rate"] is None and parsed.get("year1_rate") is not None:
            terms["prospectus_year1_rate"] = parsed["year1_rate"]

    amend_list = [
        d for d in disclosures
        if d.get("rcept_no")
        and ("예치" in str(d.get("report_nm") or ""))
        and ("신탁" in str(d.get("report_nm") or ""))
        and ("변경" in str(d.get("report_nm") or ""))
    ]
    amend_list.sort(key=lambda x: (str(x.get("rcept_dt") or ""), str(x.get("rcept_no") or "")))

    dedup_amend_by_ym: dict[str, dict[str, Any]] = {}
    for a_doc in amend_list:
        ym_key = str(a_doc.get("rcept_dt") or "")[:6] or str(a_doc.get("rcept_no") or "")
        dedup_amend_by_ym[ym_key] = a_doc
    ordered_amends = [dedup_amend_by_ym[k] for k in sorted(dedup_amend_by_ym.keys())]

    for amend_idx, a_doc in enumerate(ordered_amends, start=1):
        rep_nm = str(a_doc.get("report_nm") or "")
        terms["trust_amendment_reports"].append(rep_nm)
        a_text = fetch_document_text(a_doc["rcept_no"], max_files=30)
        prev_r, next_r, exp_years = parse_trust_amendment_single_doc(a_text)

        if 1 in exp_years:
            terms["amendment_year1_rate"] = exp_years[1]
        if 2 in exp_years:
            terms["year2_deposit_rate"] = exp_years[2]
        if 3 in exp_years:
            terms["year3_deposit_rate"] = exp_years[3]

        if amend_idx == 1:
            if prev_r is not None and 1 not in exp_years:
                terms["amendment_year1_rate"] = prev_r
            if next_r is not None and 2 not in exp_years:
                terms["year2_deposit_rate"] = next_r
        elif amend_idx == 2:
            if prev_r is not None and 2 not in exp_years and terms["year2_deposit_rate"] is None:
                terms["year2_deposit_rate"] = prev_r
            if next_r is not None and 3 not in exp_years:
                terms["year3_deposit_rate"] = next_r

    if terms["amendment_year1_rate"] is not None:
        terms["year1_deposit_rate"] = terms["amendment_year1_rate"]
    elif terms["prospectus_year1_rate"] is not None:
        terms["year1_deposit_rate"] = terms["prospectus_year1_rate"]

    terms["yearly_deposit_rates"] = [
        terms["year1_deposit_rate"],
        terms["year2_deposit_rate"],
        terms["year3_deposit_rate"],
    ]
    return terms


def cross_verify_spac_inputs(
    spac: dict[str, Any],
    market_meta: dict[str, Any],
    dart_terms: dict[str, Any],
    prev_rec: dict[str, Any],
) -> dict[str, Any]:
    krx_shares = parse_share_count(spac.get("krx_listed_shares") or market_meta.get("listed_shares"))
    krx_par = sanitize_spac_ipo_price(spac.get("krx_par_value") or market_meta.get("par_value"))

    ipo_price = sanitize_spac_ipo_price(dart_terms.get("ipo_price"))
    if ipo_price is None and krx_par is not None:
        ipo_price = krx_par
    if ipo_price is None:
        ipo_price = sanitize_spac_ipo_price(prev_rec.get("ipo_price"))
    if ipo_price is None:
        ipo_price = DEFAULT_SPAC_IPO_PRICE

    ipo_shares = parse_share_count(dart_terms.get("ipo_shares"))
    if ipo_shares is None:
        prev_s = parse_share_count(prev_rec.get("ipo_shares"))
        if prev_s and prev_s >= 100_000:
            ipo_shares = prev_s
    if ipo_shares is None and krx_shares and krx_shares >= 100_000:
        ipo_shares = krx_shares

    expected_amount = (ipo_price * ipo_shares) if (ipo_price and ipo_shares) else None
    ipo_amount = parse_krw_amount(dart_terms.get("ipo_amount"))
    if ipo_amount is not None and ipo_amount < 100_000_000:
        ipo_amount = None
    if ipo_amount is not None and expected_amount and (
        ipo_amount < expected_amount * 0.2 or ipo_amount > expected_amount * 5.0
    ):
        ipo_amount = expected_amount
    if ipo_amount is None and expected_amount:
        ipo_amount = expected_amount
    if ipo_amount is None:
        prev_a = parse_krw_amount(prev_rec.get("ipo_amount"))
        if prev_a and prev_a >= 100_000_000:
            ipo_amount = prev_a

    deposit_ratio = parse_krw_float(dart_terms.get("deposit_ratio"))
    if deposit_ratio is not None and not (50.0 <= deposit_ratio <= 105.0):
        deposit_ratio = None
    if deposit_ratio is None and prev_rec.get("deposit_ratio") is not None:
        prev_r = parse_krw_float(prev_rec.get("deposit_ratio"))
        if prev_r is not None and 50.0 <= prev_r <= 105.0:
            deposit_ratio = prev_r

    deposit_principal = parse_krw_amount(dart_terms.get("deposit_principal"))
    if deposit_principal is not None and deposit_principal < 10_000_000:
        deposit_principal = None
    if deposit_principal is None and ipo_amount and deposit_ratio is not None:
        deposit_principal = round(ipo_amount * (deposit_ratio / 100.0))
    if deposit_principal is None:
        prev_dp = parse_krw_amount(prev_rec.get("deposit_principal"))
        if prev_dp and prev_dp >= 10_000_000:
            deposit_principal = prev_dp

    if deposit_ratio is None and deposit_principal and ipo_amount and ipo_amount > 0:
        calc_ratio = round((deposit_principal / ipo_amount) * 100.0, 2)
        if 50.0 <= calc_ratio <= 105.0:
            deposit_ratio = calc_ratio

    cross_check_match: bool | None = None
    if ipo_amount and ipo_price and ipo_shares:
        cross_check_match = abs((ipo_price * ipo_shares) - ipo_amount) <= max(1, int(ipo_amount * 0.05))

    y1 = _parse_rate_value(dart_terms.get("year1_deposit_rate"))
    y2 = _parse_rate_value(dart_terms.get("year2_deposit_rate"))
    y3 = _parse_rate_value(dart_terms.get("year3_deposit_rate"))

    return {
        "ipo_price": ipo_price,
        "ipo_shares": ipo_shares,
        "ipo_amount": ipo_amount,
        "deposit_ratio": deposit_ratio,
        "deposit_principal": deposit_principal,
        "krx_listed_shares": krx_shares,
        "krx_cross_check_match": cross_check_match,
        "year1_deposit_rate": y1,
        "year2_deposit_rate": y2,
        "year3_deposit_rate": y3,
        "yearly_deposit_rates": [y1, y2, y3],
    }


def extract_unit_price(text: str, base_price: int | None = None) -> int | None:
    """실제 해산/청산/분배금 공시에서 1주당 청산/분배 금액을 엄격히 추출합니다."""
    clean_text = clean_html_text(text)
    if not clean_text:
        return None
    ref_base = sanitize_spac_ipo_price(base_price) or DEFAULT_SPAC_IPO_PRICE
    min_plausible = max(1000, int(ref_base * 0.85))
    max_plausible = int(ref_base * 1.15)

    patterns = [
        r"(?:1주당\s*청산\s*분배금|1주당\s*해산\s*분배금|주당\s*청산\s*분배금|주당\s*해산\s*분배금|청산\s*분배금|해산\s*분배금)[^0-9원]{0,20}([1-9][0-9,]{2,4})\s*원(?!\s*(?:만|억|조|주|명))",
    ]
    for pat in patterns:
        for m in re.finditer(pat, clean_text):
            digits = m.group(1).replace(",", "")
            if digits.isdigit():
                val = int(digits)
                if min_plausible <= val <= max_plausible:
                    return val
    return None


def extract_pay_date(text: str, fallback_rcept_dt: str = "") -> str:
    clean_text = clean_html_text(text)
    if clean_text:
        patterns = [
            r"(?:지급\s*예정일|지급일|분배금\s*지급일|청산\s*지급일|반환\s*예정일|반환일)[^0-9]{0,20}(20[2-9][0-9])[년.\-/]?\s*([01]?[0-9])[월.\-/]?\s*([0-3]?[0-9])\s*일?",
            r"(20[2-9][0-9])[년.\-/]\s*([01]?[0-9])[월.\-/]\s*([0-3]?[0-9])\s*일?\s*(?:지급|예정|반환)",
        ]
        for pat in patterns:
            m = re.search(pat, clean_text)
            if m:
                y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
                try:
                    return datetime.strptime(f"{y}-{mo}-{d}", "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    continue

    dt_fb = parse_date_yyyymmdd(fallback_rcept_dt)
    return dt_fb.strftime("%Y-%m-%d") if dt_fb else ""


def extract_merger_meta(disclosures: list[dict]) -> dict[str, str | None]:
    meta: dict[str, str | None] = {
        "merger_status": None,
        "merger_target": None,
        "merger_approved_at": None,
    }
    for d in disclosures:
        rep = str(d.get("report_nm") or "")
        if any(k in rep for k in ("합병", "주식교환", "영업양수도", "주요사항보고서(회사합병결정)")):
            if "철회" in rep or "취소" in rep:
                continue
            if "승인" in rep or "결의" in rep:
                meta["merger_status"] = "합병승인"
            elif "신고서" in rep or "결정" in rep:
                meta["merger_status"] = "합병추진"
            else:
                meta["merger_status"] = "합병관련공시"
            dt_rcept = parse_date_yyyymmdd(d.get("rcept_dt"))
            meta["merger_approved_at"] = dt_rcept.strftime("%Y-%m-%d") if dt_rcept else None
            m_target = re.search(r"합병\s*\(([^)]+)\)", rep)
            if m_target:
                meta["merger_target"] = m_target.group(1).strip()
            break
    return meta


def get_risk_disclosure_titles(disclosures: list[dict]) -> list[str]:
    risk_patterns = (
        r"상장폐지\s*(결정|사유\s*발생|통지|실행)",
        r"주권\s*매매거래\s*정지",
        r"기업인수목적회사\s*해산",
        r"해산\s*(사유\s*발생|결정|등기)",
        r"청산\s*(종결|결정|절차\s*개시)",
    )
    matched: list[str] = []
    for d in disclosures:
        title = str(d.get("report_nm") or "").strip()
        if title and any(re.search(pat, title) for pat in risk_patterns):
            matched.append(title)
    return matched


# =========================
# 4. 청산가 / 연환산 수익률 / 청산형·합병형 필터링
# =========================
def _sanitize_rate_percent(val: Any) -> float | None:
    rv = _parse_rate_value(val)
    if rv is None:
        return None
    # Typical Korean SPAC trust deposit rate is ~0.5% to 8.0% per annum
    if 0.1 <= rv <= 12.0:
        return round(float(rv), 4)
    return None


def calc_sheet_liquidation_unit_price(
    base_price: int,
    r1: float,
    r2: float | None,
    r3: float | None,
    final_year_months: float = 6.0,
) -> float:
    # r1, r2, r3 are passed as after-tax annual percentage rates (e.g., 2.02 for 2.02%)
    rate_1 = max(0.0, min(0.15, float(r1) / 100.0))
    y1 = base_price * (1.0 + rate_1)

    if r2 is None:
        final_rate = rate_1
        prev_amount = y1
    else:
        rate_2 = max(0.0, min(0.15, float(r2) / 100.0))
        y2 = y1 * (1.0 + rate_2)
        if r3 is None:
            final_rate = rate_2
            prev_amount = y2
        else:
            final_rate = max(0.0, min(0.15, float(r3) / 100.0))
            prev_amount = y2

    month_ratio = max(0.0, min(1.0, float(final_year_months) / 12.0))
    final_amount = prev_amount * (1.0 + final_rate * month_ratio)
    return round(final_amount, 2)


def resolve_liquidation_price(
    disclosures: list[dict],
    verified: dict[str, Any],
    deposit_rate_after_tax: float,
) -> tuple[float | None, int | None, str, str | None]:
    base_price = sanitize_spac_ipo_price(verified.get("ipo_price")) or DEFAULT_SPAC_IPO_PRICE
    max_liq = base_price * 1.15
    y1 = verified.get("year1_deposit_rate")
    y2 = verified.get("year2_deposit_rate")
    y3 = verified.get("year3_deposit_rate")

    liq_docs = [
        d for d in disclosures
        if any(k in (d.get("report_nm") or "") for k in ("청산", "해산", "분배금"))
    ]
    for d in liq_docs:
        title = d.get("report_nm", "")
        price = extract_unit_price(title, base_price=base_price)
        if price is not None and price <= max_liq:
            return float(price), base_price, f"공시제목:{title}", None
    for d in liq_docs[:2]:
        if not d.get("rcept_no"):
            continue
        doc_text = fetch_document_text(d["rcept_no"], max_files=15)
        price = extract_unit_price(doc_text, base_price=base_price)
        if price is not None and price <= max_liq:
            return float(price), base_price, f"공시본문:{d.get('report_nm', '')}", None

    sy1 = _sanitize_rate_percent(y1)
    sy2 = _sanitize_rate_percent(y2)
    sy3 = _sanitize_rate_percent(y3)
    base_after_tax = max(0.1, min(10.0, float(deposit_rate_after_tax or 2.0)))

    if sy1 is not None:
        assumed_years: list[str] = []
        calc_y1 = sy1
        if sy2 is not None:
            calc_y2 = sy2
        else:
            calc_y2 = calc_y1
            assumed_years.append("2년차")

        if sy3 is not None:
            calc_y3 = sy3
        else:
            calc_y3 = calc_y2
            assumed_years.append("3년차")

        r1_eff = to_after_tax_rate(calc_y1)
        r2_eff = to_after_tax_rate(calc_y2)
        r3_eff = to_after_tax_rate(calc_y3)

        liq_unit = calc_sheet_liquidation_unit_price(
            base_price=base_price,
            r1=r1_eff,
            r2=r2_eff,
            r3=r3_eff,
            final_year_months=6.0,
        )
        if liq_unit < base_price or liq_unit > max_liq:
            liq_unit = calc_sheet_liquidation_unit_price(
                base_price=base_price,
                r1=base_after_tax,
                r2=base_after_tax,
                r3=base_after_tax,
                final_year_months=6.0,
            )
            return liq_unit, base_price, f"공시공모가({base_price:,}원)+기준예금금리산식(이율이상치보정)", None

        if assumed_years:
            note = (
                f"※ 미정인 {'·'.join(assumed_years)} 예치이자율은 직전 연도 수치로 가정하여 계산한 "
                "잠정 청산가입니다."
            )
            return liq_unit, base_price, f"공시공모가({base_price:,}원)+공시예치이율산식(미정연차전년도가정)", note
        return liq_unit, base_price, f"공시공모가({base_price:,}원)+공시예치이율산식", None

    liq_unit = calc_sheet_liquidation_unit_price(
        base_price=base_price,
        r1=base_after_tax,
        r2=base_after_tax,
        r3=base_after_tax,
        final_year_months=6.0,
    )
    note = "※ 미정인 예치이자율은 기준 예금금리로 가정하여 계산한 잠정 청산가입니다."
    return liq_unit, base_price, f"공시공모가({base_price:,}원)+기준예금금리산식", note


def extract_liquidation_record(spac: dict, disclosures: list[dict], base_price: int | None) -> dict | None:
    keywords = ("청산", "해산", "분배금")
    candidates = [d for d in disclosures if any(k in (d.get("report_nm") or "") for k in keywords)][:2]

    for d in candidates:
        title = d.get("report_nm", "")
        rcept_no = d.get("rcept_no", "")
        unit_price = extract_unit_price(title, base_price=base_price)
        pay_date = extract_pay_date(title, d.get("rcept_dt", ""))

        if (unit_price is None or not pay_date) and rcept_no:
            doc_text = fetch_document_text(rcept_no, max_files=15)
            if unit_price is None:
                unit_price = extract_unit_price(doc_text, base_price=base_price)
            if not pay_date:
                pay_date = extract_pay_date(doc_text, d.get("rcept_dt", ""))

        if unit_price is not None and unit_price > 0 and pay_date:
            return {
                "code": spac["code"],
                "name": spac["name"],
                "unit_price": unit_price,
                "pay_date": pay_date,
                "report_nm": title,
                "rcept_no": rcept_no,
            }
    return None


def infer_delisting_date(listing_date: str) -> str:
    dt = parse_date_yyyymmdd(listing_date)
    if dt:
        return add_years_safe(dt, 3).strftime("%Y-%m-%d")
    return ""


def calc_remaining_days(delisting_date_str: str, ref_dt: datetime) -> int | None:
    dt_delist = parse_date_yyyymmdd(delisting_date_str)
    if not dt_delist:
        return None
    return (dt_delist - ref_dt).days


def calc_annualized_yields(
    s_liq_price: float,
    d_current_price: int,
    listing_date_str: str,
    delisting_date_str: str,
    ref_dt: datetime,
) -> tuple[float | None, float | None, int | None]:
    if d_current_price <= 0 or s_liq_price <= 0:
        return None, None, None

    dt_delist = parse_date_yyyymmdd(delisting_date_str)
    dt_list = parse_date_yyyymmdd(listing_date_str)
    if not dt_delist and dt_list:
        dt_delist = add_years_safe(dt_list, 3)

    if not dt_delist:
        return None, None, None

    f_rem_days = (dt_delist - ref_dt).days
    denom_days = f_rem_days + 30
    if denom_days <= 0:
        return None, None, f_rem_days

    isa_ratio = ((s_liq_price - d_current_price) / d_current_price) / denom_days * 365.0
    if s_liq_price <= d_current_price:
        gen_ratio = isa_ratio
    else:
        net_liq = d_current_price + (s_liq_price - d_current_price) * (1.0 - GENERAL_TAX_RATE)
        gen_ratio = ((net_liq - d_current_price) / d_current_price) / denom_days * 365.0

    return round(isa_ratio * 100.0, 2), round(gen_ratio * 100.0, 2), f_rem_days


def calc_fair_buy_status(
    c_base_price: int,
    s_liq_price: float,
    d_current_price: int,
    e_listing_date_str: str,
    ref_dt: datetime,
    is_suspended: bool,
) -> tuple[str, float | None]:
    if is_suspended or d_current_price <= 0:
        return "X", None

    dt_list = parse_date_yyyymmdd(e_listing_date_str)
    if not dt_list:
        fair_price = float(s_liq_price)
        return ("O" if fair_price >= d_current_price else "X"), round(fair_price, 2)

    elapsed_days = (ref_dt - dt_list).days
    fair_price = c_base_price + ((s_liq_price - c_base_price) / 1095.0) * elapsed_days
    status_yn = "O" if fair_price >= d_current_price else "X"
    return status_yn, round(fair_price, 2)


def is_liquidation_type_fit(record: dict[str, Any], deposit_benchmark_rate: float) -> bool:
    """청산형: 매수적정(O) + 잔존일 수 1년(365일) 이내 + 수익률 예금금리 이상"""
    if record.get("is_fair_buy") != "O":
        return False
    rem_days = record.get("remaining_days")
    if rem_days is None or not (0 < int(rem_days) <= MERGER_THRESHOLD_DAYS):
        return False
    y_isa = parse_krw_float(record.get("yield_isa"))
    y_gen = parse_krw_float(record.get("yield_general"))
    best_yield = max(
        [v for v in (y_isa, y_gen) if v is not None],
        default=None,
    )
    if best_yield is None:
        return False
    return best_yield >= deposit_benchmark_rate


def is_merger_type_fit(record: dict[str, Any]) -> bool:
    """합병형: 매수적정(O) + 잔존일 수 1년(365일) 초과 + 수익률 0% 이상"""
    if record.get("is_fair_buy") != "O":
        return False
    rem_days = record.get("remaining_days")
    if rem_days is None or int(rem_days) <= MERGER_THRESHOLD_DAYS:
        return False
    y_isa = parse_krw_float(record.get("yield_isa"))
    y_gen = parse_krw_float(record.get("yield_general"))
    best_yield = max(
        [v for v in (y_isa, y_gen) if v is not None],
        default=None,
    )
    if best_yield is None:
        return False
    return best_yield >= 0.0


def evaluate_spac_eligibility(record: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    code = normalize_code(record.get("code"))
    name = str(record.get("name") or "").strip()
    dept = str(record.get("krx_dept") or "").strip()

    if not code or code in EXCLUDED_SPAC_CODES:
        reasons.append("excluded_code")
    if not name:
        reasons.append("missing_name")
    if not is_krx_spac_dept(dept):
        reasons.append("not_krx_spac_dept")

    delisting_date = str(record.get("delisting_date") or "").strip()
    rem_days = record.get("remaining_days")
    if not delisting_date or rem_days is None:
        reasons.append("missing_delisting_date")
    elif rem_days <= 0:
        reasons.append("expired_maturity")

    status = str(record.get("status") or "").strip()
    if any(k in status for k in ("상장폐지", "폐지", "해산", "청산", "합병완료")):
        reasons.append(f"status:{status}")

    current_price = record.get("current_price") or 0
    if bool(record.get("is_suspended")) or current_price <= 0:
        reasons.append("suspended_or_invalid_price")

    liq_price = record.get("liquidation_price") or 0
    if liq_price <= 0:
        reasons.append("invalid_liquidation_price")

    if EXCLUDE_DELISTING_NOTICE:
        if "관리종목" in dept:
            reasons.append("administrative_issue")
        if bool(record.get("has_liquidation_notice")):
            reasons.append("liquidation_notice_flag")
        risk_titles = get_risk_disclosure_titles(record.get("recent_disclosures") or [])
        if risk_titles:
            reasons.append(f"risk_disclosure:{risk_titles[0]}")

    return (len(reasons) == 0), reasons


def deduplicate_spacs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_keys: set[str] = set()
    unique_items: list[dict[str, Any]] = []

    for item in items:
        code = normalize_code(item.get("code"))
        norm = normalize_name(item.get("name"))
        dedup_key = code if code else norm
        if not dedup_key:
            continue
        if dedup_key in seen_keys or (norm and norm in seen_keys):
            continue
        seen_keys.add(dedup_key)
        if norm:
            seen_keys.add(norm)
        unique_items.append(item)

    return unique_items


def extract_dart_summary(disclosures: list[dict[str, Any]]) -> str:
    if not disclosures:
        return ""
    summary_parts: list[str] = []
    for d in disclosures[:15]:
        if not isinstance(d, dict):
            continue
        title = str(d.get("report_nm") or "").strip()
        dt_str = str(d.get("rcept_dt") or "").strip()
        if title:
            summary_parts.append(f"[{dt_str}] {title}" if dt_str else title)
    return " / ".join(summary_parts[:8])


def build_spac_record(
    spac: dict,
    corp_codes: dict[str, str],
    quote_by_code: dict[str, dict[str, Any]],
    prev_rec: dict[str, Any],
    deposit_rate_after_tax: float,
    deposit_rate_gross: float,
    ref_dt: datetime,
) -> tuple[dict, dict | None]:
    code = spac["code"]
    corp_code = corp_codes.get(code, "") or str(prev_rec.get("corp_code") or "")
    current_price, is_suspended, market_meta = get_krx_quote_and_base(code, quote_by_code)

    disclosures = get_recent_dart_disclosures(corp_code, days=1460)
    if not disclosures and isinstance(prev_rec.get("recent_disclosures"), list):
        disclosures = prev_rec["recent_disclosures"]

    dart_terms = extract_dart_spac_terms(disclosures)
    verified = cross_verify_spac_inputs(spac, market_meta, dart_terms, prev_rec)

    merger_meta = extract_merger_meta(disclosures)
    has_merger = bool(merger_meta["merger_status"]) or any(
        "합병" in (d.get("report_nm") or "") and "철회" not in (d.get("report_nm") or "")
        for d in disclosures[:15]
    )
    risk_titles = get_risk_disclosure_titles(disclosures[:20])
    has_liq = bool(risk_titles) or any(
        any(k in (d.get("report_nm") or "") for k in ("해산사유", "청산종결", "상장폐지결정", "상장폐지사유"))
        for d in disclosures[:15]
    )

    listing_date = spac.get("listing_date") or str(prev_rec.get("listing_date") or "")
    delisting_date = infer_delisting_date(listing_date) or str(prev_rec.get("delisting_date") or "")

    s_liq_price, c_base_price, liq_source, liq_note = resolve_liquidation_price(
        disclosures, verified, deposit_rate_after_tax
    )
    liq_record = extract_liquidation_record(spac, disclosures, base_price=c_base_price)

    if s_liq_price is not None and s_liq_price > 0:
        yield_isa, yield_general, rem_days = calc_annualized_yields(
            s_liq_price=s_liq_price,
            d_current_price=current_price,
            listing_date_str=listing_date,
            delisting_date_str=delisting_date,
            ref_dt=ref_dt,
        )
    else:
        yield_isa, yield_general = None, None
        rem_days = calc_remaining_days(delisting_date, ref_dt) if delisting_date else None

    if rem_days is not None and rem_days <= 0:
        status = "상장폐지"
    elif is_suspended:
        status = "정지"
    elif has_merger:
        status = "합병"
    else:
        status = "일반"

    if c_base_price is not None and c_base_price > 0 and s_liq_price is not None and s_liq_price > 0:
        is_fair_buy, fair_buy_price = calc_fair_buy_status(
            c_base_price=c_base_price,
            s_liq_price=s_liq_price,
            d_current_price=current_price,
            e_listing_date_str=listing_date,
            ref_dt=ref_dt,
            is_suspended=is_suspended,
        )
    else:
        is_fair_buy, fair_buy_price = "UNKNOWN", None

    if s_liq_price is not None and s_liq_price > 0:
        liq_display = int(round(s_liq_price)) if float(s_liq_price).is_integer() else s_liq_price
        simple_yield = round(((s_liq_price - current_price) / current_price) * 100.0, 2) if current_price > 0 else 0.0
    else:
        liq_display = None
        simple_yield = None

    effective_y1 = verified.get("year1_deposit_rate")
    effective_deposit_rate = effective_y1 if effective_y1 is not None else deposit_rate_gross
    dart_summary = extract_dart_summary(disclosures)

    record = {
        "code": code,
        "name": spac["name"],
        "market": spac.get("market", "KOSDAQ"),
        "krx_dept": spac.get("krx_dept", ""),
        "listing_date": listing_date,
        "delisting_date": delisting_date,
        "maturity_date": delisting_date,
        "remaining_days": rem_days,
        "corp_code": corp_code,
        "base_price": c_base_price,
        "ipo_price": verified.get("ipo_price"),
        "ipo_shares": verified.get("ipo_shares"),
        "ipo_amount": verified.get("ipo_amount"),
        "deposit_ratio": verified.get("deposit_ratio"),
        "deposit_principal": verified.get("deposit_principal"),
        "krx_listed_shares": verified.get("krx_listed_shares"),
        "krx_cross_check_match": verified.get("krx_cross_check_match"),
        "year1_deposit_rate": verified.get("year1_deposit_rate"),
        "year2_deposit_rate": verified.get("year2_deposit_rate"),
        "year3_deposit_rate": verified.get("year3_deposit_rate"),
        "yearly_deposit_rates": verified.get("yearly_deposit_rates"),
        "deposit_interest_rate": effective_deposit_rate,
        "dart_deposit_rate": effective_y1,
        "dart_summary": dart_summary,
        "prospectus_report": dart_terms.get("prospectus_report"),
        "trust_amendment_reports": dart_terms.get("trust_amendment_reports"),
        "current_price": current_price,
        "open_price": market_meta["open_price"],
        "high_price": market_meta["high_price"],
        "low_price": market_meta["low_price"],
        "prev_diff": market_meta["prev_diff"],
        "chg_rate": market_meta["chg_rate"],
        "change_rate": market_meta["chg_rate"] if market_meta["chg_rate"] is not None else 0.0,
        "volume": market_meta["volume"],
        "trading_value": market_meta["trading_value"],
        "market_cap": market_meta["market_cap"],
        "trade_date": market_meta["trade_date"],
        "liquidation_price": liq_display,
        "liquidation_price_source": liq_source,
        "liquidation_source": liq_source,
        "liquidation_note": liq_note,
        "fair_buy_price": fair_buy_price,
        "is_fair_buy": is_fair_buy,
        "yield_isa": yield_isa,
        "yield_general": yield_general,
        "simple_yield": simple_yield,
        "deposit_rate": deposit_rate_after_tax,
        "deposit_rate_after_tax": deposit_rate_after_tax,
        "deposit_rate_gross": deposit_rate_gross,
        "is_suspended": is_suspended,
        "trading_on_krx": not is_suspended,
        "has_liquidation_notice": has_liq,
        "merger_status": merger_meta["merger_status"],
        "merger_target": merger_meta["merger_target"],
        "merger_approved_at": merger_meta["merger_approved_at"],
        "latest_report": disclosures[0].get("report_nm") if disclosures else None,
        "latest_report_date": (
            parse_date_yyyymmdd(disclosures[0].get("rcept_dt")).strftime("%Y-%m-%d")
            if disclosures and parse_date_yyyymmdd(disclosures[0].get("rcept_dt"))
            else None
        ),
        "status": status,
        "recent_disclosures": disclosures[:15],
    }

    ok, reasons = evaluate_spac_eligibility(record)
    record["is_eligible"] = ok
    record["ineligible_reasons"] = reasons

    liq_fit = ok and is_liquidation_type_fit(record, deposit_rate_after_tax)
    merger_fit = ok and is_merger_type_fit(record)
    record["is_liquidation_type"] = liq_fit
    record["is_merger_type"] = merger_fit
    record["invest_type"] = "청산형" if liq_fit else ("합병형" if merger_fit else "일반")

    if not ok:
        print(f"[FILTER] [{code}] {spac['name']} 비적격 -> {', '.join(reasons)}")

    return record, liq_record


def save_json(items: list[dict], path: Path, updated_at: str, extra_meta: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"updated_at": updated_at}
    if extra_meta:
        payload.update(extra_meta)
    payload["count"] = len(items)
    payload["items"] = items
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[INFO] {path} 저장 완료 (updated_at={updated_at}, count={len(items)})")


def main() -> None:
    existing_spacs = load_existing_items(OUTPUT_PATH)
    existing_liqs = load_existing_items(LIQUIDATIONS_PATH)
    prev_by_code = {
        normalize_code(item.get("code")): item
        for item in existing_spacs
        if normalize_code(item.get("code"))
    }

    now_kst = datetime.now(KST)
    ref_dt = datetime(now_kst.year, now_kst.month, now_kst.day)
    updated_at = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

    try:
        deposit_rate_after_tax, deposit_rate_gross, _ = get_base_deposit_rate(now_kst)
        corp_codes = get_dart_corp_codes()
        spac_list, quote_by_code, trade_date, dept_matched_raw = get_krx_spac_market_data(ref_dt, prev_by_code)

        if not spac_list:
            print("[WARN] KRX SPAC(소속부 없음) 종목이 비어 있음 — 기존 데이터 유지")
            save_json(existing_spacs, OUTPUT_PATH, updated_at)
            save_json(existing_liqs, LIQUIDATIONS_PATH, updated_at)
            return

        spac_results: list[dict] = []
        liq_results: list[dict] = []

        for idx, spac in enumerate(spac_list, start=1):
            code = spac["code"]
            try:
                rec, liq = build_spac_record(
                    spac,
                    corp_codes,
                    quote_by_code,
                    prev_by_code.get(code, {}),
                    deposit_rate_after_tax,
                    deposit_rate_gross,
                    ref_dt,
                )
                spac_results.append(rec)
                if liq is not None:
                    liq_results.append(liq)
            except Exception as exc:
                print(f"[WARN] [{code}] 처리 오류: {exc}")
            if idx % 20 == 0:
                print(f"[PROGRESS] {idx}/{len(spac_list)} 완료")
            time.sleep(0.02)

        spac_results = deduplicate_spacs(spac_results)
        spac_results.sort(key=lambda x: (x.get("name") or ""))

        eligible_items = [r for r in spac_results if r.get("is_eligible")]
        liquidation_type_items = [r for r in spac_results if r.get("is_liquidation_type")]
        merger_type_items = [r for r in spac_results if r.get("is_merger_type")]
        valid_prices = sum(1 for r in spac_results if (r.get("current_price") or 0) > 0)
        resolved_liq_cnt = sum(1 for r in spac_results if r.get("liquidation_price") is not None)
        fair_o_cnt = sum(1 for r in spac_results if r.get("is_fair_buy") == "O")

        meta = {
            "trade_date": trade_date,
            "deposit_rate_gross": deposit_rate_gross,
            "deposit_rate_after_tax": deposit_rate_after_tax,
            "summary": {
                "target_spac_count": TARGET_SPAC_COUNT,
                "krx_dept_matched_raw": dept_matched_raw,
                "total_spac_count": len(spac_results),
                "total_candidates": len(spac_results),
                "eligible_count": len(eligible_items),
                "liquidation_type_count": len(liquidation_type_items),
                "merger_type_count": len(merger_type_items),
                "valid_prices": valid_prices,
                "resolved_liquidation_count": resolved_liq_cnt,
                "fair_buy_o_count": fair_o_cnt,
            },
        }
        save_json(spac_results, OUTPUT_PATH, updated_at, extra_meta=meta)
        save_json(liq_results, LIQUIDATIONS_PATH, updated_at)

        print(
            f"[DONE] 전체스팩={len(spac_results)}개 (소속부매칭={dept_matched_raw}개, 적격={len(eligible_items)}개) | "
            f"청산형(잔존365일이내+매수적정+예금이상)={len(liquidation_type_items)}개 | "
            f"합병형(잔존365일초과+매수적정+0%이상)={len(merger_type_items)}개 | "
            f"매수적정O={fair_o_cnt}개"
        )
    except Exception as exc:
        print(f"[ERROR] 메인 실행 실패 — 기존 데이터 유지: {exc}")
        traceback.print_exc()
        if existing_spacs:
            save_json(existing_spacs, OUTPUT_PATH, updated_at)
        if existing_liqs:
            save_json(existing_liqs, LIQUIDATIONS_PATH, updated_at)


if __name__ == "__main__":
    main()
