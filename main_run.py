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

# 2024~2026년 한국은행 정기예금 금리(연 %, 월별 폴백 테이블)
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


def parse_krw_int(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        return int(round(val)) if math.isfinite(float(val)) else 0
    s = str(val).replace(",", "").strip()
    if not s or s in {"-", "미확인", "N/A", "null", "None"}:
        return 0
    try:
        num = float(s)
        return int(round(num)) if math.isfinite(num) else 0
    except ValueError:
        digits = re.sub(r"[^0-9\-]", "", s)
        if not digits or digits == "-":
            return 0
        try:
            return int(digits)
        except ValueError:
            return 0


def parse_krw_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(float(val)) else None
    s = re.sub(r"[,%\s원억만]|%p", "", str(val)).strip()
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
    """한국은행 ECOS API에서 최신 정기예금 금리(세전 %) 조회 -> (최신 YYYY-MM, 최신 금리, 월별 맵)"""
    fetched_map: dict[str, float] = {}
    if not ECOS_API_KEY:
        return None, None, fetched_map

    start_ym = "202401"
    end_ym = ref_dt.strftime("%Y%m")
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/"
        f"{ECOS_API_KEY}/json/kr/1/200/121Y002/M/{start_ym}/{end_ym}/010200000"
    )
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
            return valid_rows[-1][0], valid_rows[-1][1], fetched_map
    except Exception as exc:
        print(f"[WARN] ECOS 금리 조회 실패(폴백 사용): {exc}")
    return None, None, fetched_map


def get_base_deposit_rate(ref_dt: datetime | None = None) -> tuple[float, float, dict[str, float]]:
    """(세후 기준 예금금리, 세전 기준 예금금리, 월별 금리맵) 반환"""
    now_ref = ref_dt or datetime.now(KST)
    rates_map: dict[str, float] = dict(DEFAULT_DEPOSIT_RATES)

    if DEPOSIT_RATE_PATH.exists():
        try:
            with DEPOSIT_RATE_PATH.open("r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict) and isinstance(saved.get("rates"), dict):
                for k, v in saved["rates"].items():
                    parsed = parse_krw_float(v)
                    if parsed is not None and parsed > 0:
                        rates_map[str(k)] = float(parsed)
        except Exception as exc:
            print(f"[WARN] 기준 예금금리 읽기 실패: {exc}")

    ecos_ym, ecos_rate, ecos_map = fetch_ecos_deposit_rate(now_ref)
    if ecos_map:
        rates_map.update(ecos_map)

    latest_ym = sorted(rates_map.keys())[-1]
    gross_rate = ecos_rate if (ecos_ym and ecos_rate is not None) else rates_map[latest_ym]

    try:
        DEPOSIT_RATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DEPOSIT_RATE_PATH.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "updated_at": now_ref.strftime("%Y-%m-%d %H:%M:%S KST"),
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
# 2. KRX 종목/시세/상장일 조회
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

    # 상장일(LIST_DD) 보완을 위해 종목기본정보(MDCSTAT01901) 추가 조회
    listing_map: dict[str, str] = {}
    if used_date:
        for mkt_id in ("KSQ", "STK"):
            base_rows = _fetch_krx_web_bld_rows(
                session, "dbms/MDC/STAT/standard/MDCSTAT01901", used_date, mkt_id
            )
            for r in base_rows:
                c = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드"))
                ld = str(r.get("LIST_DD") or r.get("상장일") or "").strip()
                if c and ld:
                    listing_map[c] = ld

    info_by_code: dict[str, dict[str, Any]] = {}
    for r in info_rows:
        code = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("ISU_CD") or r.get("code"))
        if re.fullmatch(r"[0-9A-Za-z]{6}", code):
            info_by_code[code] = r

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
            or listing_map.get(code)
            or prev_rec.get("listing_date")
            or ""
        ).strip()
        dt_list = parse_date_yyyymmdd(raw_list_dd)
        listing_date = dt_list.strftime("%Y-%m-%d") if dt_list else raw_list_dd

        seen_codes.add(code)
        spacs.append({
            "code": code,
            "name": name,
            "market": q_info.get("market") or prev_rec.get("market") or "KOSDAQ",
            "listing_date": listing_date,
            "krx_dept": dept,
        })

    print(
        f"[INFO] KRX 기준일={used_date or '미확인'}, info={len(info_rows)}건, "
        f"trade={len(trade_rows)}건, web={len(web_ksq_rows) + len(web_stk_rows)}건, SPAC후보={len(spacs)}개"
    )
    return spacs, quote_by_code, used_date


def get_krx_quote_and_base(
    code: str,
    quote_by_code: dict[str, dict[str, Any]],
) -> tuple[int, bool, int | None, dict[str, Any]]:
    q = quote_by_code.get(code, {})
    current_price = parse_krw_int(q.get("close_price"))
    volume = parse_krw_int(q.get("volume"))
    is_suspended = current_price <= 0

    par_base_price: int | None = None
    if current_price > 0:
        par_base_price = 10000 if current_price >= 5000 else 2000

    market_meta = {
        "open_price": parse_krw_int(q.get("open_price")),
        "high_price": parse_krw_int(q.get("high_price")),
        "low_price": parse_krw_int(q.get("low_price")),
        "prev_diff": parse_krw_int(q.get("prev_diff")),
        "chg_rate": q.get("chg_rate"),
        "volume": volume,
        "trading_value": parse_krw_int(q.get("trading_value")),
        "market_cap": parse_krw_int(q.get("market_cap")),
        "trade_date": q.get("trade_date", ""),
    }
    return current_price, is_suspended, par_base_price, market_meta


# =========================
# 3. OpenDART 공시/합병/청산 조회
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


def get_recent_dart_disclosures(corp_code: str, days: int = 730) -> list[dict]:
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
                "page_count": 30,
            },
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=8,
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
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&nbsp;|&#160;", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _decode_doc_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("euc-kr", errors="ignore")


def fetch_document_text(rcept_no: str) -> str:
    if not DART_API_KEY or not rcept_no:
        return ""
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=10,
        )
        res.raise_for_status()
        if not res.content.startswith(b"PK"):
            return ""
        chunks: list[str] = []
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            for member in zf.namelist()[:4]:
                if member.lower().endswith((".xml", ".html", ".htm")):
                    raw = zf.read(member)
                    chunks.append(clean_html_text(_decode_doc_bytes(raw)))
        return " ".join(chunks)
    except Exception:
        return ""


def extract_unit_price(text: str) -> int | None:
    clean_text = clean_html_text(text)
    if not clean_text:
        return None
    patterns = [
        r"(?:청산가|청산\s*분배금|해산\s*분배금|1주당\s*청산\w*|주당\s*분배금|예치금\s*반환\w*)[^0-9]{0,25}([1-9][0-9,]{2,4})\s*원",
        r"(?:공모가|발행가|기준가|예치\s*단가)[^0-9]{0,25}([1-9][0-9,]{2,4})\s*원",
    ]
    for pat in patterns:
        for m in re.finditer(pat, clean_text):
            digits = m.group(1).replace(",", "")
            if digits.isdigit():
                val = int(digits)
                if 1000 <= val <= 50000:
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
    """일반 안내 문구 오탐을 줄이기 위해 확정·사유발생·실행 성격의 상폐/해산 공시 패턴 판별"""
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
# 4. 청산가 / 연환산 수익률 / 매수적정 / 필터링
# =========================
def calc_sheet_liquidation_unit_price(
    base_price: int,
    r1: float,
    r2: float | None,
    r3: float | None,
) -> float:
    rate_1 = max(0.0, float(r1)) / 100.0
    y1 = base_price * (1.0 + rate_1)

    if r2 is None:
        final_rate = rate_1
        prev_amount = y1
    else:
        rate_2 = max(0.0, float(r2)) / 100.0
        y2 = y1 * (1.0 + rate_2)
        if r3 is None:
            final_rate = rate_2
            prev_amount = y2
        else:
            final_rate = max(0.0, float(r3)) / 100.0
            prev_amount = y2

    final_amount = prev_amount * (1.0 + final_rate * FINAL_YEAR_TAX_FACTOR * FINAL_YEAR_MONTH_RATIO)
    return round(final_amount, 2)


def resolve_liquidation_price(
    disclosures: list[dict],
    current_price: int,
    par_base_price: int | None,
    deposit_rate_after_tax: float,
) -> tuple[float, int, str]:
    base_price = par_base_price if par_base_price is not None else (10000 if current_price >= 5000 else 2000)

    for d in disclosures:
        title = d.get("report_nm", "")
        price = extract_unit_price(title)
        if price is not None:
            return float(price), base_price, f"공시제목:{title}"

    liq_docs = [
        d for d in disclosures
        if d.get("rcept_no") and any(k in (d.get("report_nm") or "") for k in ("청산", "해산", "분배금", "예치"))
    ][:2]
    for d in liq_docs:
        doc_text = fetch_document_text(d["rcept_no"])
        price = extract_unit_price(doc_text)
        if price is not None:
            return float(price), base_price, f"공시본문:{d.get('report_nm', '')}"

    liq_unit = calc_sheet_liquidation_unit_price(
        base_price=base_price,
        r1=deposit_rate_after_tax,
        r2=deposit_rate_after_tax,
        r3=deposit_rate_after_tax,
    )
    source = (
        f"공모가({base_price:,}원)+복리+최종6개월산식"
        if par_base_price is not None
        else f"추정공모가({base_price:,}원)+복리+최종6개월산식"
    )
    return liq_unit, base_price, source


def extract_liquidation_record(spac: dict, disclosures: list[dict]) -> dict | None:
    keywords = ("청산", "해산", "분배금", "예치금 반환")
    candidates = [d for d in disclosures if any(k in (d.get("report_nm") or "") for k in keywords)][:2]

    for d in candidates:
        title = d.get("report_nm", "")
        rcept_no = d.get("rcept_no", "")
        unit_price = extract_unit_price(title)
        pay_date = extract_pay_date(title, d.get("rcept_dt", ""))

        if (unit_price is None or not pay_date) and rcept_no:
            doc_text = fetch_document_text(rcept_no)
            if unit_price is None:
                unit_price = extract_unit_price(doc_text)
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


def evaluate_spac_eligibility(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """상장폐지·해산·거래정지·만기경과 종목을 제외하기 위한 검증"""
    reasons: list[str] = []

    code = normalize_code(record.get("code"))
    name = str(record.get("name") or "").strip()
    dept = str(record.get("krx_dept") or "").strip()

    if not code or code in EXCLUDED_SPAC_CODES:
        reasons.append("excluded_code")
    if not name:
        reasons.append("missing_name")
    if not is_spac_entity(name, dept):
        reasons.append("not_spac")

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

    if record.get("yield_isa") is None or record.get("yield_general") is None:
        reasons.append("missing_yield")

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


def build_spac_record(
    spac: dict,
    corp_codes: dict[str, str],
    quote_by_code: dict[str, dict[str, Any]],
    prev_rec: dict[str, Any],
    deposit_rate_after_tax: float,
    deposit_rate_gross: float,
    ref_dt: datetime,
) -> tuple[dict | None, dict | None]:
    code = spac["code"]
    corp_code = corp_codes.get(code, "") or str(prev_rec.get("corp_code") or "")
    current_price, is_suspended, par_base_price, market_meta = get_krx_quote_and_base(code, quote_by_code)

    disclosures = get_recent_dart_disclosures(corp_code, days=730)
    if not disclosures and isinstance(prev_rec.get("recent_disclosures"), list):
        disclosures = prev_rec["recent_disclosures"]

    merger_meta = extract_merger_meta(disclosures)
    has_merger = bool(merger_meta["merger_status"]) or any(
        "합병" in (d.get("report_nm") or "") and "철회" not in (d.get("report_nm") or "")
        for d in disclosures[:15]
    )
    risk_titles = get_risk_disclosure_titles(disclosures[:20])
    has_liq = bool(risk_titles) or any(
        any(k in (d.get("report_nm") or "") for k in ("해산", "청산", "상장폐지"))
        for d in disclosures[:15]
    )

    listing_date = spac.get("listing_date") or str(prev_rec.get("listing_date") or "")
    delisting_date = infer_delisting_date(listing_date) or str(prev_rec.get("delisting_date") or "")

    s_liq_price, c_base_price, liq_source = resolve_liquidation_price(
        disclosures, current_price, par_base_price, deposit_rate_after_tax
    )
    liq_record = extract_liquidation_record(spac, disclosures)

    yield_isa, yield_general, rem_days = calc_annualized_yields(
        s_liq_price=s_liq_price,
        d_current_price=current_price,
        listing_date_str=listing_date,
        delisting_date_str=delisting_date,
        ref_dt=ref_dt,
    )

    if rem_days is not None and rem_days <= 0:
        status = "상장폐지"
    elif is_suspended:
        status = "정지"
    elif has_merger:
        status = "합병"
    else:
        status = "일반"

    # 잔존일수 365일 이상이면 합병, 미만이면 청산
    if rem_days is not None:
        invest_type = "합병" if rem_days >= MERGER_THRESHOLD_DAYS else "청산"
    else:
        invest_type = "합병" if has_merger else "청산"

    is_fair_buy, fair_buy_price = calc_fair_buy_status(
        c_base_price=c_base_price,
        s_liq_price=s_liq_price,
        d_current_price=current_price,
        e_listing_date_str=listing_date,
        ref_dt=ref_dt,
        is_suspended=is_suspended,
    )

    liq_display = int(round(s_liq_price)) if float(s_liq_price).is_integer() else s_liq_price
    simple_yield = round(((s_liq_price - current_price) / current_price) * 100.0, 2) if current_price > 0 else 0.0

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
        "ipo_price": c_base_price,
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
        "fair_buy_price": fair_buy_price,
        "is_fair_buy": is_fair_buy,
        "invest_type": invest_type,
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
    if not ok:
        print(f"[FILTER] [{code}] {spac['name']} 제외 -> {', '.join(reasons)}")
        return None, liq_record

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
        deposit_rate_after_tax, deposit_rate_gross, deposit_rates = get_base_deposit_rate(now_kst)
        corp_codes = get_dart_corp_codes()
        spac_list, quote_by_code, trade_date = get_krx_spac_market_data(ref_dt, prev_by_code)

        if not spac_list:
            print("[WARN] KRX SPAC 종목이 비어 있음 — 기존 데이터 유지")
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
                if rec is not None:
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

        valid_prices = sum(1 for r in spac_results if (r.get("current_price") or 0) > 0)
        resolved_liq_cnt = sum(1 for r in spac_results if r.get("liquidation_price") is not None)
        fair_o_cnt = sum(1 for r in spac_results if r.get("is_fair_buy") == "O")
        print(
            f"[SUMMARY] KRX기준일={trade_date or '-'}, 유효SPAC종목={len(spac_results)}개, "
            f"종가확보={valid_prices}개, 청산가확보={resolved_liq_cnt}개, "
            f"매수적정(O)={fair_o_cnt}개, 청산내역={len(liq_results)}건"
        )

        spacs_meta = {
            "date": now_kst.strftime("%Y-%m-%d"),
            "deposit_rate_gross": round(deposit_rate_gross, 2),
            "deposit_rate_after_tax": round(deposit_rate_after_tax, 2),
            "deposit_rates": deposit_rates,
        }
        save_json(spac_results, OUTPUT_PATH, updated_at, extra_meta=spacs_meta)
        save_json(liq_results, LIQUIDATIONS_PATH, updated_at)

    except Exception as exc:
        print(f"[ERROR] 메인 실행 예외: {exc}")
        traceback.print_exc()
        save_json(existing_spacs, OUTPUT_PATH, updated_at)
        save_json(existing_liqs, LIQUIDATIONS_PATH, updated_at)


if __name__ == "__main__":
    main()
```[cite: 7]

---

### 2. `run-update.yml` 전체 코드

```yaml
name: Update SPAC Data

on:
  schedule:
    - cron: "0 9 * * 1-5"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  update-spacs:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install --upgrade pip && pip install requests

      - name: Run SPAC updater script
        env:
          DART_API_KEY: ${{ secrets.DART_API_KEY }}
          KRX_OPENAPI_KEY: ${{ secrets.KRX_OPENAPI_KEY }}
          ECOS_API_KEY: ${{ secrets.ECOS_API_KEY }}
          EXCLUDE_DELISTING_NOTICE: "true"
        run: python -u main_run.py

      - name: Commit and push updated data
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add -f data/spacs.json data/liquidations.json data/deposit_rates.json || true
          if git diff --staged --quiet; then
            echo "변경된 데이터가 없습니다."
          else
            git commit -m "chore: update spacs, liquidations, and deposit rates data"
            git push
          fi
```[cite: 6]