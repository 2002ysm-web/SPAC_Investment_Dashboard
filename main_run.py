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

DEFAULT_PRE_TAX_DEPOSIT_RATE = 2.50
FALLBACK_BOk_RATES = {
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

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# =========================
# 공용 파싱 및 유틸리티
# =========================
def normalize_code(code: Any) -> str:
    digits = re.sub(r"[^0-9]", "", str(code or "").strip())
    if not digits:
        return ""
    return digits.zfill(6)[-6:]


def normalize_name(name: Any) -> str:
    s = str(name or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    s = s.replace("주식회사", "").replace("(주)", "").replace("㈜", "")
    s = re.sub(r"^kb", "케이비", s)
    s = re.sub(r"^ibks", "아이비케이에스", s)
    return s


def is_spac_entity(name: Any, dept: Any = "") -> bool:
    n = str(name or "").strip()
    d = str(dept or "").strip()
    if not n:
        return False
    if "스팩" in n or "SPAC" in n.upper():
        return True
    if "SPAC" in d.upper():
        return True
    return False


def parse_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    s = str(val).strip()
    if not s or s in {"-", "N/A", "NA", "null", "None", "미확인", "미정"}:
        return None
    s = s.replace(",", "").replace("%", "").replace("원", "").replace("주", "").strip()
    try:
        f = float(s)
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def parse_int(val: Any) -> int | None:
    f = parse_float(val)
    if f is None:
        return None
    return int(round(f))


def parse_date_yyyymmdd(val: Any) -> datetime | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    m = re.search(r"(\d{4})[-./]?(\d{2})[-./]?(\d{2})", s)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def safe_get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 20) -> Any:
    try:
        resp = SESSION.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[WARN] GET JSON 실패 ({url}): {exc}")
        return None


def safe_post_json(
    url: str,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> Any:
    try:
        merged_headers = dict(HEADERS)
        if headers:
            merged_headers.update(headers)
        resp = SESSION.post(url, data=data, headers=merged_headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[WARN] POST JSON 실패 ({url}): {exc}")
        return None


def load_existing_items(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            return [x for x in raw["items"] if isinstance(x, dict)]
    except Exception as exc:
        print(f"[WARN] 기존 JSON 로드 실패 ({path}): {exc}")
    return []


def load_existing_deposit_rates(path: Path) -> dict[str, float]:
    rates = dict(FALLBACK_BOk_RATES)
    if not path.exists():
        return rates
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("rates"), dict):
            for k, v in raw["rates"].items():
                fv = parse_float(v)
                if fv is not None and fv > 0:
                    rates[str(k)] = round(fv, 4)
    except Exception as exc:
        print(f"[WARN] 기존 예금금리 JSON 로드 실패 ({path}): {exc}")
    return rates


# =========================
# 1. 한국은행 ECOS 정기예금 금리 조회
# =========================
def fetch_ecos_deposit_rates(start_ym: str = "202401", end_ym: str | None = None) -> dict[str, float]:
    if not ECOS_API_KEY:
        return {}
    if not end_ym:
        end_ym = datetime.now(KST).strftime("%Y%m")
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{ECOS_API_KEY}/json/kr/1/200/"
        f"121Y002/M/{start_ym}/{end_ym}/010200000"
    )
    data = safe_get_json(url, timeout=15)
    if not isinstance(data, dict):
        return {}
    rows = (((data.get("StatisticSearch") or {}).get("row")) or [])
    if not isinstance(rows, list):
        return {}
    parsed: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        time_str = str(row.get("TIME") or "").strip()
        val = parse_float(row.get("DATA_VALUE"))
        if len(time_str) == 6 and val is not None and val > 0:
            ym = f"{time_str[:4]}-{time_str[4:6]}"
            parsed[ym] = round(val, 4)
    return parsed


def get_base_deposit_rate(now_kst: datetime) -> tuple[float, float, dict[str, float]]:
    rates = load_existing_deposit_rates(DEPOSIT_RATE_PATH)
    current_ym = now_kst.strftime("%Y%m")
    ecos_rates = fetch_ecos_deposit_rates(start_ym="202401", end_ym=current_ym)
    if ecos_rates:
        rates.update(ecos_rates)
        print(f"[INFO] ECOS 기준예금금리 {len(ecos_rates)}개월치 갱신 완료")
    else:
        print("[INFO] ECOS API 미사용 또는 응답 없음 — 내장/기존 예금금리 시계열 사용")

    sorted_keys = sorted(rates.keys())
    latest_ym = sorted_keys[-1] if sorted_keys else now_kst.strftime("%Y-%m")
    pre_tax_rate = rates.get(latest_ym, DEFAULT_PRE_TAX_DEPOSIT_RATE)
    after_tax_rate = round(pre_tax_rate * DEPOSIT_AFTER_TAX_FACTOR, 4)

    DEPOSIT_RATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": now_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
        "source": "BOK ECOS 121Y002 (010200000)" if ecos_rates else "Fallback/Cached BOK Series",
        "latest_ym": latest_ym,
        "pre_tax_rate": round(pre_tax_rate, 4),
        "after_tax_rate": after_tax_rate,
        "tax_rate": DEPOSIT_INTEREST_TAX_RATE,
        "rates": rates,
    }
    DEPOSIT_RATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] {DEPOSIT_RATE_PATH} 저장 완료 (기준월={latest_ym}, 세전={pre_tax_rate:.2f}%, 세후={after_tax_rate:.2f}%)")
    return after_tax_rate, round(pre_tax_rate, 4), rates


# =========================
# 2. OpenDART 기업코드 및 공시 조회
# =========================
def get_dart_corp_codes() -> dict[str, str]:
    corp_map: dict[str, str] = {}
    if not DART_API_KEY:
        print("[WARN] DART_API_KEY가 설정되지 않았습니다.")
        return corp_map

    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    try:
        resp = SESSION.get(url, params={"crtfc_key": DART_API_KEY}, timeout=30)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_name = next((n for n in zf.namelist() if n.endswith(".xml")), None)
            if not xml_name:
                return corp_map
            xml_bytes = zf.read(xml_name)
        root = ET.fromstring(xml_bytes)
        for item in root.findall(".//list"):
            stock_code = normalize_code(item.findtext("stock_code"))
            corp_code = str(item.findtext("corp_code") or "").strip()
            if stock_code and corp_code:
                corp_map[stock_code] = corp_code
        print(f"[INFO] DART 상장사 corp_code {len(corp_map)}개 로드 완료")
    except Exception as exc:
        print(f"[WARN] DART corpCode 다운로드/파싱 실패: {exc}")
    return corp_map


def get_recent_dart_disclosures(corp_code: str, days: int = 730) -> list[dict[str, Any]]:
    if not DART_API_KEY or not corp_code:
        return []
    end_dt = datetime.now(KST)
    start_dt = end_dt - timedelta(days=days)
    url = "https://opendart.fss.or.kr/api/list.json"
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bgn_de": start_dt.strftime("%Y%m%d"),
        "end_de": end_dt.strftime("%Y%m%d"),
        "page_no": 1,
        "page_count": 50,
    }
    data = safe_get_json(url, params=params, timeout=15)
    if not isinstance(data, dict) or data.get("status") != "000":
        return []
    rows = data.get("list") or []
    if not isinstance(rows, list):
        return []

    disclosures: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        disclosures.append(
            {
                "rcept_no": str(r.get("rcept_no") or "").strip(),
                "rcept_dt": str(r.get("rcept_dt") or "").strip(),
                "report_nm": str(r.get("report_nm") or "").strip(),
                "corp_name": str(r.get("corp_name") or "").strip(),
                "flr_nm": str(r.get("flr_nm") or "").strip(),
                "rm": str(r.get("rm") or "").strip(),
            }
        )
    return disclosures


# =========================
# 3. KRX SPAC 종목 및 시세 조회
# =========================
def fetch_krx_openapi_spac_quotes(trade_date_str: str) -> list[dict[str, Any]]:
    if not KRX_OPENAPI_KEY:
        return []
    endpoints = [
        "https://data-dbg.krx.co.kr/svc/apis/sto/kosdaq_bydd_trd",
        "https://data-dbg.krx.co.kr/svc/apis/idx/kosdaq_dd_trd",
    ]
    headers = {"AUTH_KEY": KRX_OPENAPI_KEY}
    for ep in endpoints:
        data = safe_get_json(ep, params={"basDd": trade_date_str}, headers=headers, timeout=15)
        rows = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("OutBlock_1", "output", "data", "list"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
        if rows:
            return [r for r in rows if isinstance(r, dict)]
    return []


def fetch_krx_web_spac_quotes(trade_date_str: str) -> list[dict[str, Any]]:
    url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
    payload = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
        "locale": "ko_KR",
        "mktId": "KSQ",
        "trdDd": trade_date_str,
        "share": "1",
        "money": "1",
        "csvxls_isNo": "false",
    }
    data = safe_post_json(url, data=payload, timeout=20)
    if not isinstance(data, dict):
        return []
    rows = data.get("OutBlock_1") or []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def parse_krx_quote_row(row: dict[str, Any], trade_date_str: str) -> dict[str, Any] | None:
    code = normalize_code(
        row.get("ISU_SRT_CD") or row.get("ISU_CD") or row.get("code") or row.get("종목코드")
    )
    name = str(
        row.get("ISU_ABBRV") or row.get("ISU_NM") or row.get("name") or row.get("종목명") or ""
    ).strip()
    dept = str(row.get("MKT_NM") or row.get("SECT_TP_NM") or row.get("소속부") or "KOSDAQ").strip()

    if not code or not name:
        return None
    if not is_spac_entity(name, dept):
        return None

    close_price = parse_float(
        row.get("TDD_CLSPRC") or row.get("CLSPRC") or row.get("close") or row.get("종가")
    )
    open_price = parse_float(row.get("TDD_OPNPRC") or row.get("OPNPRC") or row.get("시가"))
    high_price = parse_float(row.get("TDD_HGPRC") or row.get("HGPRC") or row.get("고가"))
    low_price = parse_float(row.get("TDD_LWPRC") or row.get("LWPRC") or row.get("저가"))
    prev_diff = parse_float(row.get("CMPPREVDD_PRC") or row.get("전일대비"))
    chg_rate = parse_float(row.get("FLUC_RT") or row.get("등락률"))
    volume = parse_int(row.get("ACC_TRDVOL") or row.get("TRDVOL") or row.get("거래량"))
    trading_value = parse_int(row.get("ACC_TRDVAL") or row.get("TRDVAL") or row.get("거래대금"))
    market_cap = parse_int(row.get("MKTCAP") or row.get("시가총액"))

    listing_date_raw = (
        row.get("LIST_DD") or row.get("ISU_LIST_DD") or row.get("상장일") or ""
    )
    listing_dt = parse_date_yyyymmdd(listing_date_raw)
    listing_date_str = listing_dt.strftime("%Y-%m-%d") if listing_dt else ""

    is_suspended = (close_price is None or close_price <= 0) and ((volume or 0) == 0)

    formatted_trade_date = (
        f"{trade_date_str[:4]}-{trade_date_str[4:6]}-{trade_date_str[6:8]}"
        if len(trade_date_str) == 8
        else ""
    )

    return {
        "code": code,
        "name": name,
        "market": "KOSDAQ",
        "krx_dept": dept,
        "listing_date": listing_date_str,
        "current_price": int(round(close_price)) if close_price and close_price > 0 else 0,
        "open_price": int(round(open_price)) if open_price and open_price > 0 else None,
        "high_price": int(round(high_price)) if high_price and high_price > 0 else None,
        "low_price": int(round(low_price)) if low_price and low_price > 0 else None,
        "prev_diff": int(round(prev_diff)) if prev_diff is not None else None,
        "chg_rate": round(chg_rate, 2) if chg_rate is not None else None,
        "volume": volume,
        "trading_value": trading_value,
        "market_cap": market_cap,
        "trade_date": formatted_trade_date,
        "is_suspended": is_suspended,
    }


def get_krx_spac_market_data(
    ref_dt: datetime, prev_by_code: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    quote_by_code: dict[str, dict[str, Any]] = {}
    used_trade_date = ""

    for offset in range(0, 10):
        cand_dt = ref_dt - timedelta(days=offset)
        if cand_dt.weekday() >= 5:
            continue
        ymd = cand_dt.strftime("%Y%m%d")
        raw_rows = fetch_krx_openapi_spac_quotes(ymd)
        if not raw_rows:
            raw_rows = fetch_krx_web_spac_quotes(ymd)

        parsed_map: dict[str, dict[str, Any]] = {}
        for r in raw_rows:
            q = parse_krx_quote_row(r, ymd)
            if q:
                parsed_map[q["code"]] = q

        valid_cnt = sum(1 for v in parsed_map.values() if (v.get("current_price") or 0) > 0)
        if parsed_map and valid_cnt > 0:
            quote_by_code = parsed_map
            used_trade_date = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
            print(f"[INFO] KRX 시세 확보 완료 (기준일={used_trade_date}, SPAC={len(quote_by_code)}개, 거래종가={valid_cnt}개)")
            break

    spac_list: list[dict[str, Any]] = []
    seen_codes: set[str] = set()

    for code, q in quote_by_code.items():
        seen_codes.add(code)
        prev = prev_by_code.get(code, {})
        listing_date = q.get("listing_date") or str(prev.get("listing_date") or "")
        spac_list.append(
            {
                "code": code,
                "name": q["name"],
                "market": q.get("market") or "KOSDAQ",
                "krx_dept": q.get("krx_dept") or str(prev.get("krx_dept") or ""),
                "listing_date": listing_date,
            }
        )

    for code, prev in prev_by_code.items():
        if code in seen_codes:
            continue
        name = str(prev.get("name") or "").strip()
        dept = str(prev.get("krx_dept") or "")
        if not is_spac_entity(name, dept):
            continue
        seen_codes.add(code)
        spac_list.append(
            {
                "code": code,
                "name": name,
                "market": str(prev.get("market") or "KOSDAQ"),
                "krx_dept": dept,
                "listing_date": str(prev.get("listing_date") or ""),
            }
        )

    return spac_list, quote_by_code, used_trade_date


def get_krx_quote_and_base(
    code: str, quote_by_code: dict[str, dict[str, Any]]
) -> tuple[int, bool, float, dict[str, Any]]:
    q = quote_by_code.get(code) or {}
    current_price = parse_int(q.get("current_price")) or 0
    is_suspended = bool(q.get("is_suspended")) if q else (current_price <= 0)
    par_base_price = 2000.0
    meta = {
        "open_price": q.get("open_price"),
        "high_price": q.get("high_price"),
        "low_price": q.get("low_price"),
        "prev_diff": q.get("prev_diff"),
        "chg_rate": q.get("chg_rate"),
        "volume": q.get("volume"),
        "trading_value": q.get("trading_value"),
        "market_cap": q.get("market_cap"),
        "trade_date": q.get("trade_date") or "",
    }
    return current_price, is_suspended, par_base_price, meta


# =========================
# 4. 만기/청산가/수익률/매수적정 산출
# =========================
def infer_delisting_date(listing_date_str: str) -> str:
    dt = parse_date_yyyymmdd(listing_date_str)
    if not dt:
        return ""
    try:
        maturity_dt = dt.replace(year=dt.year + 3)
    except ValueError:
        maturity_dt = dt.replace(year=dt.year + 3, month=2, day=28)
    delist_dt = maturity_dt + timedelta(days=7)
    return delist_dt.strftime("%Y-%m-%d")


def calc_remaining_days(delisting_date_str: str, ref_dt: datetime) -> int | None:
    dt = parse_date_yyyymmdd(delisting_date_str)
    if not dt:
        return None
    ref_day = datetime(ref_dt.year, ref_dt.month, ref_dt.day)
    target_day = datetime(dt.year, dt.month, dt.day)
    return (target_day - ref_day).days


def extract_merger_meta(disclosures: list[dict[str, Any]]) -> dict[str, str | None]:
    merger_status: str | None = None
    merger_target: str | None = None
    merger_approved_at: str | None = None

    for d in disclosures:
        title = str(d.get("report_nm") or "").strip()
        rcept_dt = parse_date_yyyymmdd(d.get("rcept_dt"))
        date_str = rcept_dt.strftime("%Y-%m-%d") if rcept_dt else None
        if not title or "합병" not in title:
            continue
        if "철회" in title or "취소" in title or "연기" in title:
            if merger_status is None:
                merger_status = "합병철회/변경"
            continue

        if "합병승인" in title or "주주총회" in title:
            merger_status = "주총승인"
            if not merger_approved_at:
                merger_approved_at = date_str
        elif "합병신고서" in title or "주요사항보고서" in title:
            if not merger_status:
                merger_status = "합병진행중"
        elif "합병" in title and not merger_status:
            merger_status = "합병관련공시"

        m_target = re.search(r"([가-힣A-Za-z0-9·&()_-]{2,30}(?:주식회사)?)\s*(?:와|과의|간)\s*(?:합병|소규모합병|약식합병)", title)
        if m_target and not merger_target:
            cand = m_target.group(1).strip()
            if "스팩" not in cand and "SPAC" not in cand.upper():
                merger_target = cand

    return {
        "merger_status": merger_status,
        "merger_target": merger_target,
        "merger_approved_at": merger_approved_at,
    }


def get_risk_disclosure_titles(disclosures: list[dict[str, Any]]) -> list[str]:
    risk_keywords = (
        "상장폐지",
        "해산사유발생",
        "해산청구",
        "파산",
        "회생절차",
        "기업심사위원회",
        "실질심사",
    )
    titles: list[str] = []
    for d in disclosures:
        title = str(d.get("report_nm") or "").strip()
        if any(k in title for k in risk_keywords):
            titles.append(title)
    return titles


def resolve_liquidation_price(
    disclosures: list[dict[str, Any]],
    current_price: int,
    par_base_price: float,
    deposit_rate_after_tax: float,
) -> tuple[float, float, str]:
    base_price = par_base_price if par_base_price > 0 else 2000.0
    for d in disclosures[:15]:
        title = str(d.get("report_nm") or "")
        m = re.search(r"(?:예상)?청산(?:가액|단가|금액)[^\d]{0,10}(2,\d{3}|1,\d{3}|[12]\d{3})\s*원", title)
        if m:
            val = parse_float(m.group(1))
            if val is not None and 1500.0 <= val <= 3000.0:
                return round(val, 2), base_price, "DART_DISCLOSURE"

    annual_net_rate = max(0.0, deposit_rate_after_tax / 100.0)
    estimated_liq = base_price * (1.0 + annual_net_rate * 2.5)
    if current_price > base_price * 1.15:
        estimated_liq = max(estimated_liq, base_price * 1.03)
    return round(estimated_liq, 2), base_price, "ESTIMATED_DEPOSIT"


def extract_liquidation_record(spac: dict[str, Any], disclosures: list[dict[str, Any]]) -> dict[str, Any] | None:
    code = spac["code"]
    name = spac["name"]
    for d in disclosures:
        title = str(d.get("report_nm") or "").strip()
        rcept_dt = parse_date_yyyymmdd(d.get("rcept_dt"))
        if not any(k in title for k in ("해산", "청산", "분배금", "예치금 반환")):
            continue
        m_price = re.search(r"(?:주당)?(?:청산|분배|반환)(?:가액|단가|금액)[^\d]{0,10}([\d,]+)\s*원", title)
        unit_price = parse_float(m_price.group(1)) if m_price else None
        pay_date = rcept_dt.strftime("%Y-%m-%d") if rcept_dt else ""
        return {
            "code": code,
            "name": name,
            "report_nm": title,
            "rcept_no": d.get("rcept_no") or "",
            "rcept_dt": d.get("rcept_dt") or "",
            "unit_price": unit_price,
            "pay_date": pay_date,
        }
    return None


def calc_annualized_yields(
    s_liq_price: float,
    d_current_price: int,
    listing_date_str: str,
    delisting_date_str: str,
    ref_dt: datetime,
) -> tuple[float | None, float | None, int | None]:
    rem_days = calc_remaining_days(delisting_date_str, ref_dt)
    if d_current_price <= 0 or s_liq_price <= 0 or rem_days is None or rem_days <= 0:
        return None, None, rem_days

    gross_ratio = s_liq_price / float(d_current_price)
    years = max(rem_days / 365.0, 1.0 / 365.0)
    isa_annual = (math.pow(gross_ratio, 1.0 / years) - 1.0) * 100.0

    if s_liq_price > d_current_price:
        after_tax_liq = d_current_price + (s_liq_price - d_current_price) * (1.0 - GENERAL_TAX_RATE)
    else:
        after_tax_liq = s_liq_price
    gen_ratio = max(after_tax_liq / float(d_current_price), 1e-9)
    gen_annual = (math.pow(gen_ratio, 1.0 / years) - 1.0) * 100.0

    return round(isa_annual, 2), round(gen_annual, 2), rem_days


def calc_fair_buy_status(
    c_base_price: float,
    s_liq_price: float,
    d_current_price: int,
    e_listing_date_str: str,
    ref_dt: datetime,
    is_suspended: bool,
) -> tuple[str, int | None]:
    if is_suspended or d_current_price <= 0 or s_liq_price <= 0:
        return "UNKNOWN", None
    list_dt = parse_date_yyyymmdd(e_listing_date_str)
    delist_str = infer_delisting_date(e_listing_date_str)
    delist_dt = parse_date_yyyymmdd(delist_str)
    if not list_dt or not delist_dt:
        return "UNKNOWN", None

    total_days = (delist_dt - list_dt).days
    if total_days <= 0:
        return "UNKNOWN", None

    ref_day = datetime(ref_dt.year, ref_dt.month, ref_dt.day)
    elapsed_days = max(0, min(total_days, (ref_day - list_dt).days))
    fair_price = c_base_price + (s_liq_price - c_base_price) * (elapsed_days / float(total_days))
    fair_int = int(round(fair_price))
    return ("O" if d_current_price <= fair_price else "X"), fair_int


# =========================
# 5. 필터링 및 레코드 생성
# =========================
def evaluate_spac_eligibility(record: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    code = normalize_code(record.get("code"))
    name = str(record.get("name") or "").strip()
    dept = str(record.get("krx_dept") or "").strip()

    if not code or code in EXCLUDED_SPAC_CODES:
        reasons.append("excluded_code")
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


def extract_dart_deposit_rate_and_summary(disclosures: list[dict[str, Any]]) -> tuple[float | None, str]:
    """공시 제목 및 요약/본문에서 예치이자율(%) 및 공시 요약을 추출합니다."""
    if not disclosures:
        return None, ""
    summary_parts: list[str] = []
    extracted_rate: float | None = None
    rate_patterns = [
        r"(?:예치이자율|예치이율|신탁이자율|정기예금\s*금리|이자율)[^\d]{0,12}(\d+(?:\.\d+)?)\s*%",
        r"연\s*(\d+(?:\.\d+)?)\s*%",
    ]
    for d in disclosures[:15]:
        if not isinstance(d, dict):
            continue
        title = str(d.get("report_nm") or "").strip()
        dt_str = str(d.get("rcept_dt") or "").strip()
        note = str(d.get("summary") or d.get("content") or d.get("note") or "").strip()
        combined = f"{title} {note}".strip()
        if title:
            summary_parts.append(f"[{dt_str}] {title}" if dt_str else title)
        if extracted_rate is None and any(k in combined for k in ("예치", "신탁", "청산", "해산", "공모", "이자", "금리")):
            for pat in rate_patterns:
                m = re.search(pat, combined)
                if m:
                    try:
                        val = float(m.group(1))
                        if 0.1 <= val <= 15.0:
                            extracted_rate = val
                            break
                    except ValueError:
                        pass
    return extracted_rate, " / ".join(summary_parts[:8])


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

    dart_deposit_rate, dart_summary = extract_dart_deposit_rate_and_summary(disclosures)
    effective_deposit_rate = dart_deposit_rate if dart_deposit_rate is not None else deposit_rate_gross

    record = {
        "code": code,
        "deposit_interest_rate": effective_deposit_rate,
        "dart_deposit_rate": dart_deposit_rate,
        "dart_summary": dart_summary,
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
            "trade_date": trade_date,
            "deposit_rate": deposit_rate_after_tax,
            "deposit_rate_after_tax": deposit_rate_after_tax,
            "deposit_rate_gross": deposit_rate_gross,
            "exclude_delisting_notice": EXCLUDE_DELISTING_NOTICE,
            "summary": {
                "total": len(spac_results),
                "valid_prices": valid_prices,
                "resolved_liquidation_price": resolved_liq_cnt,
                "fair_buy_o": fair_o_cnt,
            },
        }
        save_json(spac_results, OUTPUT_PATH, updated_at, extra_meta=spacs_meta)

        liq_results.sort(key=lambda x: (x.get("pay_date") or "", x.get("code") or ""), reverse=True)
        save_json(liq_results, LIQUIDATIONS_PATH, updated_at)

    except Exception as exc:
        print(f"[ERROR] 메인 실행 중 치명적 오류: {exc}")
        traceback.print_exc()
        if existing_spacs:
            print("[INFO] 기존 spacs.json 데이터를 보존합니다.")
            save_json(existing_spacs, OUTPUT_PATH, updated_at)
        if existing_liqs:
            save_json(existing_liqs, LIQUIDATIONS_PATH, updated_at)
        raise


if __name__ == "__main__":
    main()