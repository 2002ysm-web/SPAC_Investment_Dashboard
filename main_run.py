import io
import json
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

DART_API_KEY = os.environ.get("DART_API_KEY", "").strip()
KRX_OPENAPI_KEY = os.environ.get("KRX_OPENAPI_KEY", "").strip()
OUTPUT_PATH = Path(os.environ.get("SPACS_OUTPUT_PATH", "data/spacs.json"))
LIQUIDATIONS_PATH = Path(os.environ.get("LIQUIDATIONS_OUTPUT_PATH", "data/liquidations.json"))
DEPOSIT_RATE_PATH = Path(os.environ.get("DEPOSIT_RATE_PATH", "data/deposit_rates.json"))

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
FINAL_YEAR_TAX_FACTOR = 0.845
FINAL_YEAR_MONTH_RATIO = 6.0 / 12.0


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


def get_base_deposit_rate() -> float:
    fallback = 2.35
    if DEPOSIT_RATE_PATH.exists():
        try:
            with DEPOSIT_RATE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            rates = data.get("rates", {}) if isinstance(data, dict) else {}
            if rates:
                latest_ym = sorted(rates.keys())[-1]
                return float(rates[latest_ym])
        except Exception as exc:
            print(f"[WARN] 기준 예금금리 읽기 실패: {exc}")
    return fallback


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


def normalize_code(val: Any) -> str:
    raw = str(val or "").strip()
    if raw.endswith(".0"):
        raw = raw[:-2]
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) > 6:
        digits = digits[-6:]
    return digits.zfill(6) if digits else ""


def parse_krw_int(val: Any) -> int:
    if val is None:
        return 0
    s = re.sub(r"[^0-9\-]", "", str(val).strip())
    if not s or s == "-":
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def parse_krw_float(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).replace(",", "").replace("%", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def is_spac_affiliation(dept_val: Any) -> bool:
    """KRX 종목 정보 소속(소속부 / SECT_TP_NM)이 SPAC 계열인지 판별합니다."""
    dept = str(dept_val or "").strip()
    if not dept:
        return False
    upper = dept.upper()
    return "SPAC" in upper or "스팩" in dept or "기업인수목적" in dept


def is_spac_name(name_val: Any) -> bool:
    """종목명이 SPAC 계열인지 보조 판별합니다."""
    name = str(name_val or "").strip()
    if not name:
        return False
    upper = name.upper()
    return bool(
        re.search(r"(스팩|SPAC|기업인수목적)", name, flags=re.IGNORECASE)
        or "SPAC" in upper
    )


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
                payload = res.json()
                if isinstance(payload, list):
                    return payload
                if isinstance(payload, dict):
                    for key in ("OutBlock_1", "outBlock_1", "output", "data", "items"):
                        rows = payload.get(key)
                        if isinstance(rows, list) and rows:
                            return rows
            except Exception:
                continue
    return []


def _fetch_krx_web_kosdaq_quotes(session: requests.Session, bas_dd: str) -> list[dict[str, Any]]:
    """KRX 정보데이터시스템 전종목 시세(코스닥) 조회"""
    url = "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd"
    payload = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
        "locale": "ko_KR",
        "mktId": "KSQ",
        "trdDd": bas_dd,
        "share": "1",
        "money": "1",
        "csvxls_isNo": "false",
    }
    try:
        res = session.post(url, data=payload, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return []
        data = res.json()
        rows = data.get("OutBlock_1") or data.get("output") or []
        if isinstance(rows, list):
            return rows
    except Exception:
        pass
    return []


def get_krx_spac_market_data(ref_dt: datetime) -> tuple[list[dict], dict[str, dict[str, Any]], str]:
    """
    KRX API(및 KRX 시세 데이터)에서 SPAC 종목 목록과 종가·시세 정보를 가져옵니다.
    반환값: (spac_list, quote_by_code, trade_date_used)
    """
    candidate_dates = [
        (ref_dt - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(0, 10)
    ]

    info_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    used_date = ""

    # 1순위: KRX Open API (코스닥 일별매매정보 + 코스닥 종목기본정보)
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

    # 2순위: KRX 정보데이터시스템 전종목 시세(코스닥)
    if not trade_rows:
        session = requests.Session()
        try:
            session.get(
                "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020101",
                headers={"User-Agent": HEADERS["User-Agent"]},
                timeout=12,
            )
        except Exception:
            pass

        for d_str in candidate_dates:
            t_rows = _fetch_krx_web_kosdaq_quotes(session, d_str)
            if t_rows:
                trade_rows = t_rows
                used_date = d_str
                break

    info_by_code: dict[str, dict[str, Any]] = {}
    for r in info_rows:
        code = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("code"))
        if re.fullmatch(r"\d{6}", code):
            info_by_code[code] = r

    quote_by_code: dict[str, dict[str, Any]] = {}
    for r in trade_rows:
        code = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("ISU_CD") or r.get("code"))
        if not re.fullmatch(r"\d{6}", code):
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

        quote_by_code[code] = {
            "name": name,
            "dept": dept,
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
            "trade_date": used_date,
        }

    # info_rows 와 trade_rows 의 모든 종목 코드를 통합 점검
    all_codes: list[str] = []
    seen_all_codes: set[str] = set()
    for r in info_rows:
        c = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("code"))
        if re.fullmatch(r"\d{6}", c) and c not in seen_all_codes:
            seen_all_codes.add(c)
            all_codes.append(c)
    for r in trade_rows:
        c = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("ISU_CD") or r.get("code"))
        if re.fullmatch(r"\d{6}", c) and c not in seen_all_codes:
            seen_all_codes.add(c)
            all_codes.append(c)

    spacs: list[dict] = []
    seen_codes: set[str] = set()
    dept_matched_cnt = 0
    name_fallback_cnt = 0

    for code in all_codes:
        if code in seen_codes:
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

        matched_by_dept = is_spac_affiliation(dept)
        matched_by_name = (not matched_by_dept) and is_spac_name(name)

        if not (matched_by_dept or matched_by_name):
            continue

        seen_codes.add(code)
        if matched_by_dept:
            dept_matched_cnt += 1
        else:
            name_fallback_cnt += 1

        listing_date = str(
            info.get("LIST_DD") or q_info.get("listing_date") or ""
        ).strip()

        spacs.append({
            "code": code,
            "name": name,
            "listing_date": listing_date,
            "krx_dept": dept or ("SPAC(명칭추정)" if matched_by_name else ""),
        })

    print(
        f"[INFO] KRX 기준일={used_date or '미확인'}, info={len(info_rows)}건, "
        f"trade={len(trade_rows)}건, SPAC종목={len(spacs)}개 "
        f"(소속일치={dept_matched_cnt}, 명칭보완={name_fallback_cnt})"
    )
    return spacs, quote_by_code, used_date


def get_krx_quote_and_base(
    code: str,
    quote_by_code: dict[str, dict[str, Any]],
) -> tuple[int, bool, int | None, dict[str, Any]]:
    """
    KRX 시세 데이터에서 종가, 거래정지 여부, 공모 기준가 추정, 부가 시세를 반환합니다.
    당일 거래량이 0이어도 종가가 존재하면 거래정지로 오인하지 않습니다.
    """
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


def get_recent_dart_disclosures(corp_code: str, days: int = 365) -> list[dict]:
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
                    decoded = raw.decode("utf-8", errors="ignore") or raw.decode("euc-kr", errors="ignore")
                    chunks.append(clean_html_text(decoded))
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

    digits = re.sub(r"[^0-9]", "", fallback_rcept_dt or "")
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def parse_date_yyyymmdd(date_str: str) -> datetime | None:
    digits = re.sub(r"[^0-9]", "", date_str or "")
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d")
        except ValueError:
            return None
    return None


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
    spac: dict,
    disclosures: list[dict],
    current_price: int,
    par_base_price: int | None,
    deposit_rate: float,
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
    ][:1]
    for d in liq_docs:
        doc_text = fetch_document_text(d["rcept_no"])
        price = extract_unit_price(doc_text)
        if price is not None:
            return float(price), base_price, f"공시본문:{d.get('report_nm', '')}"

    liq_unit = calc_sheet_liquidation_unit_price(
        base_price=base_price,
        r1=deposit_rate,
        r2=deposit_rate,
        r3=deposit_rate,
    )
    source = (
        f"공모가({base_price:,}원)+복리+최종6개월산식"
        if par_base_price is not None
        else f"추정공모가({base_price:,}원)+복리+최종6개월산식"
    )
    return liq_unit, base_price, source


def extract_liquidation_record(spac: dict, disclosures: list[dict]) -> dict | None:
    keywords = ("청산", "해산", "분배금", "예치금 반환")
    candidates = [d for d in disclosures if any(k in (d.get("report_nm") or "") for k in keywords)][:1]

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
        try:
            return dt.replace(year=dt.year + 3).strftime("%Y-%m-%d")
        except ValueError:
            pass
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
        try:
            dt_delist = dt_list.replace(year=dt_list.year + 3)
        except ValueError:
            dt_delist = None

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


def build_spac_record(
    spac: dict,
    corp_codes: dict[str, str],
    quote_by_code: dict[str, dict[str, Any]],
    deposit_rate: float,
    ref_dt: datetime,
) -> tuple[dict, dict | None]:
    code = spac["code"]
    corp_code = corp_codes.get(code, "")
    current_price, is_suspended, par_base_price, market_meta = get_krx_quote_and_base(code, quote_by_code)

    disclosures = get_recent_dart_disclosures(corp_code, days=365)

    has_merger = any("합병" in (d.get("report_nm") or "") for d in disclosures[:15])
    has_liq = any(
        any(k in (d.get("report_nm") or "") for k in ("해산", "청산", "상장폐지"))
        for d in disclosures[:15]
    )

    status = "정지" if is_suspended else ("합병" if has_merger else "일반")
    invest_type = "합병" if has_merger else "청산"

    delisting_date = infer_delisting_date(spac.get("listing_date", ""))
    s_liq_price, c_base_price, liq_source = resolve_liquidation_price(
        spac, disclosures, current_price, par_base_price, deposit_rate
    )
    liq_record = extract_liquidation_record(spac, disclosures)

    yield_isa, yield_general, rem_days = calc_annualized_yields(
        s_liq_price=s_liq_price,
        d_current_price=current_price,
        listing_date_str=spac.get("listing_date", ""),
        delisting_date_str=delisting_date,
        ref_dt=ref_dt,
    )
    is_fair_buy, fair_buy_price = calc_fair_buy_status(
        c_base_price=c_base_price,
        s_liq_price=s_liq_price,
        d_current_price=current_price,
        e_listing_date_str=spac.get("listing_date", ""),
        ref_dt=ref_dt,
        is_suspended=is_suspended,
    )

    liq_display = int(round(s_liq_price)) if s_liq_price.is_integer() else s_liq_price

    record = {
        "code": code,
        "name": spac["name"],
        "krx_dept": spac.get("krx_dept", ""),
        "listing_date": spac["listing_date"],
        "delisting_date": delisting_date,
        "remaining_days": rem_days,
        "corp_code": corp_code,
        "base_price": c_base_price,
        "current_price": current_price,
        "open_price": market_meta["open_price"],
        "high_price": market_meta["high_price"],
        "low_price": market_meta["low_price"],
        "prev_diff": market_meta["prev_diff"],
        "chg_rate": market_meta["chg_rate"],
        "volume": market_meta["volume"],
        "trading_value": market_meta["trading_value"],
        "market_cap": market_meta["market_cap"],
        "trade_date": market_meta["trade_date"],
        "liquidation_price": liq_display,
        "liquidation_price_source": liq_source,
        "fair_buy_price": fair_buy_price,
        "is_fair_buy": is_fair_buy,
        "invest_type": invest_type,
        "yield_isa": yield_isa,
        "yield_general": yield_general,
        "deposit_rate": deposit_rate,
        "is_suspended": is_suspended,
        "has_liquidation_notice": has_liq,
        "status": status,
        "recent_disclosures": disclosures[:15],
    }
    return record, liq_record


def save_json(items: list[dict], path: Path, updated_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": updated_at, "count": len(items), "items": items}
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[INFO] {path} 저장 완료 (updated_at={updated_at}, count={len(items)})")


def main() -> None:
    existing_spacs = load_existing_items(OUTPUT_PATH)
    existing_liqs = load_existing_items(LIQUIDATIONS_PATH)

    now_kst = datetime.now(KST)
    ref_dt = datetime(now_kst.year, now_kst.month, now_kst.day)
    updated_at = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

    try:
        deposit_rate = get_base_deposit_rate()
        corp_codes = get_dart_corp_codes()
        spac_list, quote_by_code, trade_date = get_krx_spac_market_data(ref_dt)

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
                rec, liq = build_spac_record(spac, corp_codes, quote_by_code, deposit_rate, ref_dt)
                spac_results.append(rec)
                if liq is not None:
                    liq_results.append(liq)
            except Exception as exc:
                print(f"[WARN] [{code}] 처리 오류: {exc}")
            if idx % 20 == 0:
                print(f"[PROGRESS] {idx}/{len(spac_list)} 완료")
            time.sleep(0.02)

        valid_prices = sum(1 for r in spac_results if (r.get("current_price") or 0) > 0)
        resolved_liq_cnt = sum(1 for r in spac_results if r.get("liquidation_price") is not None)
        fair_o_cnt = sum(1 for r in spac_results if r.get("is_fair_buy") == "O")
        print(
            f"[SUMMARY] KRX기준일={trade_date or '-'}, SPAC종목={len(spac_results)}개, "
            f"종가확보={valid_prices}개, 청산가확보={resolved_liq_cnt}개, "
            f"매수적정(O)={fair_o_cnt}개, 청산내역={len(liq_results)}건"
        )

        save_json(spac_results, OUTPUT_PATH, updated_at)
        save_json(liq_results, LIQUIDATIONS_PATH, updated_at)

    except Exception as exc:
        print(f"[ERROR] 메인 실행 예외: {exc}")
        traceback.print_exc()
        save_json(existing_spacs, OUTPUT_PATH, updated_at)
        save_json(existing_liqs, LIQUIDATIONS_PATH, updated_at)


if __name__ == "__main__":
    main()
