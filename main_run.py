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

EXCLUDED_SPAC_CODES = {
    "206400",  # 아인스팩씨엔에스
}

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
    """원/억원/백만원/천만원/만원/단순숫자 금액 문자열을 원화 정수(KRW)로 변환"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(round(val)) if math.isfinite(float(val)) else None
    s = str(val).replace(",", "").replace("\u3000", " ").strip()
    if not s or s in {"-", "미확인", "N/A", "null", "None"}:
        return None

    m_compound = re.match(
        r"^([0-9]+(?:\.[0-9]+)?)\s*억(?:\s*([0-9]+(?:\.[0-9]+)?)\s*(?:천만|백만|만))?\s*원?$",
        s,
    )
    if m_compound:
        total = float(m_compound.group(1)) * 100_000_000
        if m_compound.group(2):
            sub_val = float(m_compound.group(2))
            if "천만" in s:
                total += sub_val * 10_000_000
            elif "백만" in s:
                total += sub_val * 1_000_000
            else:
                total += sub_val * 10_000
        return int(round(total))

    m_baekman = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*백만\s*원?$", s)
    if m_baekman:
        return int(round(float(m_baekman.group(1)) * 1_000_000))

    m_cheonman = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*천만\s*원?$", s)
    if m_cheonman:
        return int(round(float(m_cheonman.group(1)) * 10_000_000))

    m_man = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*만\s*원?$", s)
    if m_man:
        return int(round(float(m_man.group(1)) * 10_000))

    digits = re.sub(r"[^0-9.\-]", "", s)
    if not digits or digits in {"-", "."}:
        return None
    try:
        num = float(digits)
        return int(round(num)) if math.isfinite(num) else None
    except ValueError:
        return None


def parse_share_count(val: Any) -> int | None:
    """주식수 문자열(천주/만주/주/숫자)을 주(integer) 단위로 변환"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(round(val)) if math.isfinite(float(val)) and val > 0 else None
    s = str(val).replace(",", "").replace("\u3000", " ").strip()
    if not s or s in {"-", "미확인", "N/A", "null", "None"}:
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


def is_spac_entity(name: Any, dept_val: Any) -> bool:
    """KRX 소속부(SPAC) 또는 종목명(스팩/SPAC/기업인수목적) 기준 판별"""
    dept = str(dept_val or "").strip().upper()
    if "SPAC" in dept:
        return True
    nm = str(name or "").strip()
    return bool(re.search(r"(스팩|SPAC|기업인수목적)", nm, re.IGNORECASE))


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
# 2. KRX 종목/시세/상장일/상장주식수 조회
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
    prev_by_code: dict[str, dict[str, Any]],
) -> tuple[list[dict], dict[str, dict[str, Any]], str]:
    candidate_dates = [
        (ref_dt - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(0, 12)
    ]

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
        for mkt_id in ("KSQ", "STK"):
            base_rows = _fetch_krx_web_bld_rows(
                session, "dbms/MDC/STAT/standard/MDCSTAT01901", used_date, mkt_id
            )
            for r in base_rows:
                c = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드"))
                if not c:
                    continue
                base_info_map[c] = {
                    "SECT_TP_NM": str(r.get("SECT_TP_NM") or r.get("소속부") or "").strip(),
                    "LIST_DD": str(r.get("LIST_DD") or r.get("상장일") or "").strip(),
                    "LIST_SHRS": parse_krw_int(r.get("LIST_SHRS") or r.get("상장주식수")),
                    "PARVAL": parse_krw_int(r.get("PARVAL") or r.get("액면가")),
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
    for src in (info_rows, trade_rows, web_ksq_rows, web_stk_rows):
        for r in src:
            c = normalize_code(
                r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("ISU_CD") or r.get("code")
            )
            if re.fullmatch(r"[0-9A-Za-z]{6}", c) and c not in seen_all_codes:
                seen_all_codes.add(c)
                all_codes.append(c)

    spacs: list[dict] = []
    seen_codes: set[str] = set()

    for code in all_codes:
        if code in seen_codes or code in EXCLUDED_SPAC_CODES:
            continue
        info = info_by_code.get(code, {})
        q_info = quote_by_code.get(code, {})
        prev_rec = prev_by_code.get(code, {})

        dept = str(
            info.get("SECT_TP_NM")
            or info.get("소속부")
            or q_info.get("dept")
            or prev_rec.get("krx_dept")
            or ""
        ).strip()

        name = str(
            info.get("ISU_ABBRV")
            or info.get("ISU_NM")
            or info.get("종목명")
            or info.get("회사명")
            or q_info.get("name")
            or prev_rec.get("name")
            or ""
        ).strip()

        if not name or not is_spac_entity(name, dept):
            continue

        raw_list_dd = str(
            info.get("LIST_DD")
            or q_info.get("listing_date")
            or prev_rec.get("listing_date")
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
            "market": q_info.get("market") or prev_rec.get("market") or "KOSDAQ",
            "listing_date": listing_date,
            "krx_dept": dept,
            "krx_listed_shares": krx_listed_shares if krx_listed_shares > 0 else None,
            "krx_par_value": krx_par_value if krx_par_value > 0 else None,
        })

    print(
        f"[INFO] KRX 기준일={used_date or '미확인'}, info={len(info_rows)}건, "
        f"trade={len(trade_rows)}건, web={len(web_ksq_rows) + len(web_stk_rows)}건, SPAC후보={len(spacs)}개"
    )
    return spacs, quote_by_code, used_date


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
