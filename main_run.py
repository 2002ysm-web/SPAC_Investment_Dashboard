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
EXCLUDE_DELISTING_NOTICE = os.environ.get("EXCLUDE_DELISTING_NOTICE", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
}

EXCLUDED_SPAC_CODES = {
    "206400",  # 아인스팩씨엔에스 (만기/상장폐지일 경과)
    "468760",  # 유진스팩10호 (상장폐지일/수익률 누락)
    "474930",  # 신한제13호스팩 (상장폐지 사유발생 / 청산 안내)
    "475250",  # 하나33호스팩 (상장폐지 사유발생 / 청산 안내)
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


def to_after_tax_rate(gross_rate: float) -> float:
    return round(float(gross_rate) * DEPOSIT_AFTER_TAX_FACTOR, 4)


def get_base_deposit_rate() -> tuple[float, float]:
    """(세후 기준 예금금리, 세전 기준 예금금리) 반환"""
    fallback_gross = 3.21
    gross_rate = fallback_gross
    if DEPOSIT_RATE_PATH.exists():
        try:
            with DEPOSIT_RATE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            rates = data.get("rates", {}) if isinstance(data, dict) else {}
            if rates:
                latest_ym = sorted(rates.keys())[-1]
                parsed = parse_krw_float(rates[latest_ym])
                if parsed is not None and parsed > 0:
                    gross_rate = parsed
        except Exception as exc:
            print(f"[WARN] 기준 예금금리 읽기 실패: {exc}")
    after_tax_rate = to_after_tax_rate(gross_rate)
    return after_tax_rate, round(gross_rate, 4)


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
    digits = re.sub(r"[^0-9A-Za-z]", "", raw)
    if len(digits) > 6:
        digits = digits[-6:]
    return digits.zfill(6) if digits else ""


def parse_krw_int(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        return int(round(val))
    s = str(val).replace(",", "").strip()
    if not s or s == "-":
        return 0
    try:
        return int(round(float(s)))
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
        return float(val)
    s = str(val).replace(",", "").replace("%", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def is_spac_affiliation(dept_val: Any) -> bool:
    dept = str(dept_val or "").strip()
    if not dept:
        return False
    upper = dept.upper()
    return "SPAC" in upper or "스팩" in dept or "기업인수목적" in dept


def is_spac_name(name_val: Any) -> bool:
    name = str(name_val or "").strip()
    if not name:
        return False
    return bool(re.search(r"(스팩|SPAC|기업인수목적)", name, flags=re.IGNORECASE))


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


def _fetch_krx_web_market_quotes(session: requests.Session, bas_dd: str, mkt_id: str = "KSQ") -> list[dict[str, Any]]:
    endpoints = (
        "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
        "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
    )
    payload = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
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
            "trade_date": fallback_date,
        }


def get_krx_spac_market_data(ref_dt: datetime) -> tuple[list[dict], dict[str, dict[str, Any]], str]:
    candidate_dates = [
        (ref_dt - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(0, 12)
    ]

    info_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    web_rows: list[dict[str, Any]] = []
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
        ksq_rows = _fetch_krx_web_market_quotes(session, d_str, "KSQ")
        stk_rows = _fetch_krx_web_market_quotes(session, d_str, "STK")
        merged_web = ksq_rows + stk_rows
        if merged_web:
            web_rows = merged_web
            if not used_date:
                used_date = d_str
            break

    info_by_code: dict[str, dict[str, Any]] = {}
    for r in info_rows:
        code = normalize_code(r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("ISU_CD") or r.get("code"))
        if re.fullmatch(r"[0-9A-Za-z]{6}", code):
            info_by_code[code] = r

    quote_by_code: dict[str, dict[str, Any]] = {}
    _merge_quote_rows(trade_rows, info_by_code, quote_by_code, used_date, overwrite=True)
    _merge_quote_rows(web_rows, info_by_code, quote_by_code, used_date, overwrite=False)

    all_codes: list[str] = []
    seen_all_codes: set[str] = set()
    for src in (info_rows, trade_rows, web_rows):
        for r in src:
            c = normalize_code(
                r.get("ISU_SRT_CD") or r.get("종목코드") or r.get("ISU_CD") or r.get("code")
            )
            if re.fullmatch(r"[0-9A-Za-z]{6}", c) and c not in seen_all_codes:
                seen_all_codes.add(c)
                all_codes.append(c)

    spacs: list[dict] = []
    seen_codes: set[str] = set()
    dept_matched_cnt = 0
    name_fallback_cnt = 0

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

        matched_by_dept = is_spac_affiliation(dept)
        matched_by_name = (not matched_by_dept) and is_spac_name(name)

        if not (matched_by_dept or matched_by_name):
            continue

        listing_date = str(
            info.get("LIST_DD") or q_info.get("listing_date") or ""
        ).strip()
        if not parse_date_yyyymmdd(listing_date):
            continue

        seen_codes.add(code)
        if matched_by_dept:
            dept_matched_cnt += 1
        else:
            name_fallback_cnt += 1

        spacs.append({
            "code": code,
            "name": name,
            "listing_date": listing_date,
            "krx_dept": dept or ("SPAC(명칭추정)" if matched_by_name else ""),
        })

    print(
        f"[INFO] KRX 기준일={used_date or '미확인'}, info={len(info_rows)}건, "
        f"trade={len(trade_rows)}건, web={len(web_rows)}건, SPAC종목={len(spacs)}개 "
        f"(소속일치={dept_matched_cnt}, 명칭보완={name_fallback_cnt})"
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
    ][:1]
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


def is_valid_spac_record(record: dict[str, Any]) -> bool:
    code = normalize_code(record.get("code"))
    if not code or code in EXCLUDED_SPAC_CODES:
        return False

    delisting_date = str(record.get("delisting_date") or "").strip()
    rem_days = record.get("remaining_days")
    if not delisting_date or not isinstance(rem_days, int) or rem_days <= 0:
        return False

    if record.get("yield_isa") is None or record.get("yield_general") is None:
        return False

    dept = str(record.get("krx_dept") or "")
    disclosures = record.get("recent_disclosures") or []
    has_delist_reason = any(
        "상장폐지 사유발생" in (d.get("report_nm") or "")
        for d in disclosures
    )

    if EXCLUDE_DELISTING_NOTICE and (
        has_delist_reason or bool(record.get("has_liquidation_notice")) or "관리종목" in dept
    ):
        return False

    return True


def build_spac_record(
    spac: dict,
    corp_codes: dict[str, str],
    quote_by_code: dict[str, dict[str, Any]],
    deposit_rate_after_tax: float,
    deposit_rate_gross: float,
    ref_dt: datetime,
) -> tuple[dict | None, dict | None]:
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
        spac, disclosures, current_price, par_base_price, deposit_rate_after_tax
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
        "deposit_rate": deposit_rate_after_tax,
        "deposit_rate_after_tax": deposit_rate_after_tax,
        "deposit_rate_gross": deposit_rate_gross,
        "is_suspended": is_suspended,
        "has_liquidation_notice": has_liq,
        "status": status,
        "recent_disclosures": disclosures[:15],
    }

    if not is_valid_spac_record(record):
        return None, liq_record

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
        deposit_rate_after_tax, deposit_rate_gross = get_base_deposit_rate()
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
                rec, liq = build_spac_record(
                    spac,
                    corp_codes,
                    quote_by_code,
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
```[cite: 36]

---

### 2. `gemini-code-1795182516702_2.py` 전체 코드

```python
import csv
import json
import math
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))
DEPOSIT_NET_FACTOR = 0.845
GENERAL_TAX_RATE = 0.154
MERGER_THRESHOLD_DAYS = 365
DEFAULT_PRE_TAX_DEPOSIT_RATE = 2.35
EXCLUDE_DELISTING_NOTICE = os.environ.get("EXCLUDE_DELISTING_NOTICE", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
}

EXCLUDED_SPAC_CODES = {
    "206400",  # 아인스팩씨엔에스
    "468760",  # 유진스팩10호
    "474930",  # 신한제13호스팩
    "475250",  # 하나33호스팩
}


def get_today_kst_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def parse_date_only(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in {"-", "미확인", "N/A", "null", "None"}:
        return None
    m = re.match(r"^(\d{4})[./-]?(\d{2})[./-]?(\d{2})", s)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=KST)
    except ValueError:
        return None


def to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    cleaned = re.sub(r"[,%\s원억만]|%p", "", str(value)).strip()
    if not cleaned or cleaned in {"-", "미확인", "N/A", "null", "None"}:
        return None
    try:
        num = float(cleaned)
        return num if math.isfinite(num) else None
    except ValueError:
        return None


def to_int(value: Any) -> Optional[int]:
    num = to_float(value)
    if num is None:
        return None
    return int(round(num))


def normalize_name(name: Any) -> str:
    s = re.sub(r"\s+", "", str(name or ""))
    s = re.sub(r"^KB", "케이비", s, flags=re.IGNORECASE)
    s = re.sub(r"^IBKS", "아이비케이에스", s, flags=re.IGNORECASE)
    return s.lower()


def extract_delisting_date(raw: Dict[str, Any]) -> str:
    for key in ("delisting_date", "liquidation_date", "maturity_date", "expiry_date", "end_date"):
        val = raw.get(key)
        dt = parse_date_only(val)
        if dt:
            return dt.strftime("%Y-%m-%d")
    listing_dt = parse_date_only(raw.get("listing_date") or raw.get("ipo_date"))
    if listing_dt:
        # 스팩 기본 존속기간(3년 = 1095일) 기준 fallback
        return (listing_dt + timedelta(days=1095)).strftime("%Y-%m-%d")
    return ""


def get_remaining_days(delisting_date_str: str, today_dt: datetime) -> Optional[int]:
    target_dt = parse_date_only(delisting_date_str)
    if not target_dt:
        return None
    return (target_dt.date() - today_dt.date()).days


def normalize_status(raw: Dict[str, Any], remaining_days: Optional[int]) -> str:
    status = str(raw.get("status") or raw.get("trade_status") or "").strip()
    if remaining_days is not None and remaining_days <= 0:
        return "상장폐지"
    if "상장폐지" in status or "폐지" in status or "해산" in status:
        return "상장폐지"
    if "정지" in status or "거래정지" in status:
        return "정지"
    if "합병" in status:
        return "합병"
    if not status or status == "정상":
        return "일반"
    return status


def has_delisting_or_liquidation_flag(raw: Dict[str, Any]) -> bool:
    dept = str(raw.get("krx_dept") or "")
    if "관리종목" in dept or bool(raw.get("has_liquidation_notice")):
        return True
    disclosures = raw.get("recent_disclosures") or []
    if isinstance(disclosures, list):
        for d in disclosures:
            if isinstance(d, dict) and "상장폐지 사유발생" in str(d.get("report_nm") or ""):
                return True
    return False


def is_valid_active_spac(
    raw: Dict[str, Any],
    delisting_date_str: str,
    remaining_days: Optional[int],
    status: str,
    yield_isa: Optional[float],
    yield_general: Optional[float],
) -> bool:
    code = str(raw.get("code") or raw.get("ticker") or raw.get("short_code") or "").strip().zfill(6)
    if code in EXCLUDED_SPAC_CODES:
        return False

    name = str(raw.get("name") or raw.get("stock_name") or "").strip()
    if not name:
        return False

    # 스팩 종목 여부 확인(종목명에 스팩/기업인수목적 포함 또는 명시적 is_spac=True)
    is_spac_flag = raw.get("is_spac")
    if is_spac_flag is False:
        return False
    if is_spac_flag is not True:
        if ("스팩" not in name) and ("기업인수목적" not in name):
            return False

    # 상장폐지일 누락 또는 만기경과/당일만료/합병완료 등 종료된 항목 제외
    if not delisting_date_str:
        return False
    if status in {"상장폐지", "합병"}:
        return False
    if remaining_days is None or remaining_days <= 0:
        return False

    raw_rem = to_float(raw.get("remaining_days"))
    if raw_rem is not None and raw_rem <= 0:
        return False

    # 수익률 누락 항목 제외
    if yield_isa is None or yield_general is None:
        return False

    # 상장폐지 사유발생 / 청산 공시 / 관리종목 제외 옵션
    if EXCLUDE_DELISTING_NOTICE and has_delisting_or_liquidation_flag(raw):
        return False

    return True


def compute_annualized_yield(
    current_price: Optional[float],
    liquidation_price: Optional[float],
    remaining_days: Optional[int],
    account_type: str,
) -> Optional[float]:
    if not current_price or current_price <= 0 or not liquidation_price or liquidation_price <= 0:
        return None
    if remaining_days is None or remaining_days <= 0:
        return None

    if account_type == "general" and liquidation_price > current_price:
        net_liquidation_price = current_price + (liquidation_price - current_price) * (1.0 - GENERAL_TAX_RATE)
    else:
        net_liquidation_price = liquidation_price

    ratio = net_liquidation_price / current_price
    if ratio <= 0:
        return -100.0

    annualized = (math.pow(ratio, 365.0 / float(remaining_days)) - 1.0) * 100.0
    return round(annualized, 2)


def determine_invest_type(raw: Dict[str, Any], remaining_days: Optional[int]) -> str:
    if remaining_days is not None:
        return "합병" if remaining_days >= MERGER_THRESHOLD_DAYS else "청산"
    raw_type = str(raw.get("invest_type") or raw.get("type") or "").strip()
    if "합병" in raw_type:
        return "합병"
    return "청산"


def determine_fair_buy(
    raw: Dict[str, Any],
    yield_isa: Optional[float],
    yield_general: Optional[float],
    after_tax_deposit_rate: float,
) -> str:
    explicit = str(raw.get("is_fair_buy") or "").strip().upper()
    if explicit in {"O", "X"}:
        pass

    candidates = [v for v in (yield_isa, yield_general) if v is not None]
    if candidates:
        return "O" if max(candidates) >= 0.0 else "X"

    if explicit in {"O", "X"}:
        return explicit
    return "미확인"


def transform_spac_item(
    raw: Dict[str, Any],
    today_dt: datetime,
    deposit_rate_pre_tax: float,
) -> Optional[Dict[str, Any]]:
    name = str(raw.get("name") or raw.get("stock_name") or "").strip()
    code = str(raw.get("code") or raw.get("ticker") or raw.get("short_code") or "").strip()
    delisting_date = extract_delisting_date(raw)
    remaining_days = get_remaining_days(delisting_date, today_dt)
    status = normalize_status(raw, remaining_days)

    current_price = to_int(raw.get("current_price") or raw.get("price") or raw.get("close"))
    liquidation_price = to_int(
        raw.get("liquidation_price")
        or raw.get("expected_liquidation_price")
        or raw.get("refund_price")
    )

    yield_isa = to_float(raw.get("yield_isa"))
    if yield_isa is None:
        yield_isa = compute_annualized_yield(
            to_float(current_price),
            to_float(liquidation_price),
            remaining_days,
            "isa",
        )

    yield_general = to_float(raw.get("yield_general"))
    if yield_general is None:
        yield_general = compute_annualized_yield(
            to_float(current_price),
            to_float(liquidation_price),
            remaining_days,
            "general",
        )

    if not is_valid_active_spac(raw, delisting_date, remaining_days, status, yield_isa, yield_general):
        return None

    invest_type = determine_invest_type(raw, remaining_days)
    after_tax_deposit = round(deposit_rate_pre_tax * DEPOSIT_NET_FACTOR, 2)
    is_fair_buy = determine_fair_buy(raw, yield_isa, yield_general, after_tax_deposit)

    item: Dict[str, Any] = {
        "code": code,
        "name": name,
        "current_price": current_price,
        "liquidation_price": liquidation_price,
        "delisting_date": delisting_date,
        "remaining_days": remaining_days,
        "invest_type": invest_type,
        "is_fair_buy": is_fair_buy,
        "status": status,
        "yield_isa": yield_isa,
        "yield_general": yield_general,
        "deposit_rate": round(deposit_rate_pre_tax, 2),
    }

    for extra_key in ("listing_date", "sponsor", "trust_amount", "report_nm", "rcept_no"):
        if raw.get(extra_key) not in (None, ""):
            item[extra_key] = raw[extra_key]

    return item


def deduplicate_spacs(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_keys = set()
    unique_items: List[Dict[str, Any]] = []

    for item in items:
        code = str(item.get("code") or "").strip()
        norm = normalize_name(item.get("name"))
        dedup_key = code if code else norm
        if not dedup_key:
            continue
        if dedup_key in seen_keys or norm in seen_keys:
            continue
        seen_keys.add(dedup_key)
        if norm:
            seen_keys.add(norm)
        unique_items.append(item)

    return unique_items


def fetch_json_if_exists(url_or_path: str) -> Any:
    if url_or_path.startswith(("http://", "https://")):
        req = Request(url_or_path, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    path = Path(url_or_path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def load_raw_records(source_path: Optional[str] = None) -> List[Dict[str, Any]]:
    candidates = [
        source_path,
        "data/spacs_raw.json",
        "data/spacs.json",
        "data/spacs_raw.csv",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        p = Path(candidate)
        if not p.exists():
            continue
        if p.suffix.lower() == ".csv":
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
    return []


def get_latest_deposit_rate(data_dir: Path) -> float:
    rate_file = data_dir / "deposit_rates.json"
    if rate_file.exists():
        try:
            payload = json.loads(rate_file.read_text(encoding="utf-8"))
            rates = payload.get("rates") if isinstance(payload, dict) else None
            if isinstance(rates, dict) and rates:
                latest_ym = sorted(rates.keys())[-1]
                val = to_float(rates[latest_ym])
                if val and val > 0:
                    return val
        except Exception:
            pass
    return DEFAULT_PRE_TAX_DEPOSIT_RATE


def generate_spacs_json(
    output_path: str = "data/spacs.json",
    source_path: Optional[str] = None,
    max_items: Optional[int] = None,
) -> Dict[str, Any]:
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    today_dt = datetime.now(KST)
    deposit_rate_pre_tax = get_latest_deposit_rate(out_file.parent)
    raw_records = load_raw_records(source_path)

    transformed: List[Dict[str, Any]] = []
    for raw in raw_records:
        item = transform_spac_item(raw, today_dt, deposit_rate_pre_tax)
        if item is not None:
            transformed.append(item)

    unique_items = deduplicate_spacs(transformed)
    unique_items.sort(
        key=lambda x: (
            x["current_price"] if x["current_price"] is not None else 10**9,
            x["name"],
        )
    )

    if max_items is not None and max_items > 0:
        unique_items = unique_items[:max_items]

    payload = {
        "updated_at": today_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "deposit_rate_pre_tax": round(deposit_rate_pre_tax, 2),
        "deposit_rate_after_tax": round(deposit_rate_pre_tax * DEPOSIT_NET_FACTOR, 2),
        "count": len(unique_items),
        "items": unique_items,
    }

    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


if __name__ == "__main__":
    result = generate_spacs_json(
        output_path="data/spacs.json",
        source_path=os.environ.get("SPAC_SOURCE_PATH"),
    )
    print(f"Saved {result['count']} active SPACs to data/spacs.json (updated_at: {result['updated_at']})")
```[cite: 37]

---

### 3. `index.html 8_2.txt` 전체 코드

```html
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SPAC Investment Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        input[type="date"] {
            display: block;
            width: 100%;
            min-width: 0;
            max-width: 100%;
            height: 42px;
            line-height: 42px;
            padding: 0 12px;
            box-sizing: border-box;
            text-align: left;
            -webkit-appearance: none;
            appearance: none;
        }
        input[type="date"]::-webkit-date-and-time-value {
            text-align: left;
            min-height: 0;
            height: 42px;
            line-height: 42px;
            margin: 0;
        }
        input[type="date"]::-webkit-datetime-edit,
        input[type="date"]::-webkit-datetime-edit-fields-wrapper {
            padding: 0;
            line-height: 42px;
        }
        input[type="date"]::-webkit-calendar-picker-indicator { margin: 0; padding: 0; }
        #toast-auto { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 60; }
    </style>
</head>
<body class="bg-slate-50 text-slate-900 font-sans">
    <div class="max-w-7xl mx-auto px-4 py-10">
        <!-- Header & Main Tab Navigation -->
        <div class="flex flex-col md:flex-row justify-between items-start md:items-center mb-6 gap-4 border-b border-slate-200 pb-6">
            <div>
                <div class="flex items-center gap-3 flex-wrap">
                    <h1 class="text-3xl font-extrabold text-slate-800 tracking-tight">🚀 SPAC Investment Dashboard</h1>
                    <span id="deposit-rate-badge" class="bg-indigo-50 text-indigo-700 border border-indigo-200 text-xs font-bold px-3 py-1 rounded-full shadow-sm">
                        기준 예금금리(세후): 연 --%
                    </span>
                </div>
            </div>
            <div class="flex items-center gap-3 flex-wrap">
                <!-- 탭 전환 버튼 -->
                <div class="bg-slate-200/80 p-1 rounded-xl flex gap-1">
                    <button id="tab-btn-dashboard" onclick="switchTab('dashboard')" class="px-3.5 py-2 rounded-lg text-xs font-extrabold transition-all bg-white text-indigo-600 shadow-sm">
                        📊 종목 분석
                    </button>
                    <button id="tab-btn-holdings" onclick="switchTab('holdings')" class="px-3.5 py-2 rounded-lg text-xs font-extrabold transition-all text-slate-600 hover:text-slate-900">
                        📂 보유 종목 🔒
                    </button>
                    <button id="tab-btn-performance" onclick="switchTab('performance')" class="px-3.5 py-2 rounded-lg text-xs font-extrabold transition-all text-slate-600 hover:text-slate-900">
                        💰 성과 관리 🔒
                    </button>
                </div>
                <div id="update-time" class="bg-white px-4 py-2 rounded-xl shadow-sm border border-slate-200 text-xs font-semibold text-slate-600">
                    데이터 로딩 중...
                </div>
            </div>
        </div>

        <!-- ========================================================= -->
        <!-- TAB 1: 종목 분석 뷰                                        -->
        <!-- ========================================================= -->
        <div id="view-dashboard">
            <!-- Summary Stats Bar -->
            <div class="grid grid-cols-3 gap-3 mb-8">
                <div class="bg-white p-4 rounded-2xl shadow-sm border border-slate-200">
                    <span class="text-[11px] font-bold text-slate-400 uppercase tracking-wider block mb-1">전체 스팩 수</span>
                    <div id="stat-total-count" class="text-xl font-extrabold text-slate-800">- 개</div>
                </div>
                <div class="bg-white p-4 rounded-2xl shadow-sm border border-slate-200">
                    <span class="text-[11px] font-bold text-indigo-600 uppercase tracking-wider block mb-1">합병형 <span class="block text-slate-400 font-normal">(매수적정, 0%↑)</span></span>
                    <div id="stat-merger-count" class="text-xl font-extrabold text-indigo-600 mt-1">- 개</div>
                </div>
                <div class="bg-white p-4 rounded-2xl shadow-sm border border-slate-200">
                    <span class="text-[11px] font-bold text-blue-600 uppercase tracking-wider block mb-1">청산형 <span class="block text-slate-400 font-normal">(매수적정, 예금↑)</span></span>
                    <div id="stat-liq-count" class="text-xl font-extrabold text-blue-600 mt-1">- 개</div>
                </div>
            </div>

            <!-- Recommendations Section -->
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-8 mb-10">
                <!-- 1. 합병형 추천 -->
                <div class="bg-gradient-to-br from-indigo-950 via-slate-900 to-slate-900 text-white p-6 rounded-2xl shadow-lg border border-indigo-500/25">
                    <div class="flex justify-between items-center mb-4">
                        <h2 class="text-base font-bold tracking-tight text-indigo-200">🤝 합병형 추천</h2>
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                        <div class="bg-white/5 p-3.5 rounded-xl border border-white/10 flex flex-col justify-between">
                            <div>
                                <span class="text-[10px] font-bold text-indigo-300 bg-indigo-500/20 px-2 py-0.5 rounded">예금 이상 (ISA)</span>
                                <h3 id="rec-merger-high-isa-name" class="text-sm font-bold mt-2 mb-2 truncate">-</h3>
                            </div>
                            <div class="space-y-1 text-xs text-slate-300">
                                <div class="flex justify-between"><span>현재가</span><span id="rec-merger-high-isa-price" class="font-bold text-white">-</span></div>
                                <div class="flex justify-between items-start"><span>수익률</span><span id="rec-merger-high-isa-yield" class="font-bold text-emerald-400 text-right">-</span></div>
                                <div class="flex justify-between"><span>청산가</span><span id="rec-merger-high-isa-liq" class="font-bold text-white">-</span></div>
                            </div>
                        </div>
                        <div class="bg-white/5 p-3.5 rounded-xl border border-white/10 flex flex-col justify-between">
                            <div>
                                <span class="text-[10px] font-bold text-indigo-300 bg-indigo-500/20 px-2 py-0.5 rounded">예금 이상 (일반)</span>
                                <h3 id="rec-merger-high-gen-name" class="text-sm font-bold mt-2 mb-2 truncate">-</h3>
                            </div>
                            <div class="space-y-1 text-xs text-slate-300">
                                <div class="flex justify-between"><span>현재가</span><span id="rec-merger-high-gen-price" class="font-bold text-white">-</span></div>
                                <div class="flex justify-between items-start"><span>수익률</span><span id="rec-merger-high-gen-yield" class="font-bold text-emerald-400 text-right">-</span></div>
                                <div class="flex justify-between"><span>청산가</span><span id="rec-merger-high-gen-liq" class="font-bold text-white">-</span></div>
                            </div>
                        </div>
                        <div class="bg-white/5 p-3.5 rounded-xl border border-white/10 flex flex-col justify-between">
                            <div>
                                <span class="text-[10px] font-bold text-indigo-300 bg-indigo-500/20 px-2 py-0.5 rounded">0%~예금미만 (ISA)</span>
                                <h3 id="rec-merger-mid-isa-name" class="text-sm font-bold mt-2 mb-2 truncate">-</h3>
                            </div>
                            <div class="space-y-1 text-xs text-slate-300">
                                <div class="flex justify-between"><span>현재가</span><span id="rec-merger-mid-isa-price" class="font-bold text-white">-</span></div>
                                <div class="flex justify-between items-start"><span>수익률</span><span id="rec-merger-mid-isa-yield" class="font-bold text-emerald-400 text-right">-</span></div>
                                <div class="flex justify-between"><span>청산가</span><span id="rec-merger-mid-isa-liq" class="font-bold text-white">-</span></div>
                            </div>
                        </div>
                        <div class="bg-white/5 p-3.5 rounded-xl border border-white/10 flex flex-col justify-between">
                            <div>
                                <span class="text-[10px] font-bold text-indigo-300 bg-indigo-500/20 px-2 py-0.5 rounded">0%~예금미만 (일반)</span>
                                <h3 id="rec-merger-mid-gen-name" class="text-sm font-bold mt-2 mb-2 truncate">-</h3>
                            </div>
                            <div class="space-y-1 text-xs text-slate-300">
                                <div class="flex justify-between"><span>현재가</span><span id="rec-merger-mid-gen-price" class="font-bold text-white">-</span></div>
                                <div class="flex justify-between items-start"><span>수익률</span><span id="rec-merger-mid-gen-yield" class="font-bold text-emerald-400 text-right">-</span></div>
                                <div class="flex justify-between"><span>청산가</span><span id="rec-merger-mid-gen-liq" class="font-bold text-white">-</span></div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 2. 청산형 추천 -->
                <div class="bg-gradient-to-br from-blue-950 via-slate-900 to-slate-900 text-white p-6 rounded-2xl shadow-lg border border-blue-500/25">
                    <div class="flex justify-between items-center mb-4">
                        <h2 class="text-base font-bold tracking-tight text-blue-200">🛡️ 청산형 추천</h2>
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                        <div class="bg-white/5 p-3.5 rounded-xl border border-white/10 flex flex-col justify-between">
                            <div>
                                <span class="text-[10px] font-bold text-blue-300 bg-blue-500/20 px-2 py-0.5 rounded">예금 이상 (ISA)</span>
                                <h3 id="rec-liq-high-isa-name" class="text-sm font-bold mt-2 mb-2 truncate">-</h3>
                            </div>
                            <div class="space-y-1 text-xs text-slate-300">
                                <div class="flex justify-between"><span>현재가</span><span id="rec-liq-high-isa-price" class="font-bold text-white">-</span></div>
                                <div class="flex justify-between items-start"><span>수익률</span><span id="rec-liq-high-isa-yield" class="font-bold text-emerald-400 text-right">-</span></div>
                                <div class="flex justify-between"><span>청산가</span><span id="rec-liq-high-isa-liq" class="font-bold text-white">-</span></div>
                            </div>
                        </div>
                        <div class="bg-white/5 p-3.5 rounded-xl border border-white/10 flex flex-col justify-between">
                            <div>
                                <span class="text-[10px] font-bold text-blue-300 bg-blue-500/20 px-2 py-0.5 rounded">예금 이상 (일반)</span>
                                <h3 id="rec-liq-high-gen-name" class="text-sm font-bold mt-2 mb-2 truncate">-</h3>
                            </div>
                            <div class="space-y-1 text-xs text-slate-300">
                                <div class="flex justify-between"><span>현재가</span><span id="rec-liq-high-gen-price" class="font-bold text-white">-</span></div>
                                <div class="flex justify-between items-start"><span>수익률</span><span id="rec-liq-high-gen-yield" class="font-bold text-emerald-400 text-right">-</span></div>
                                <div class="flex justify-between"><span>청산가</span><span id="rec-liq-high-gen-liq" class="font-bold text-white">-</span></div>
                            </div>
                        </div>
                        <div class="bg-white/5 p-3.5 rounded-xl border border-white/10 flex flex-col justify-between">
                            <div>
                                <span class="text-[10px] font-bold text-blue-300 bg-blue-500/20 px-2 py-0.5 rounded">0%~예금미만 (ISA)</span>
                                <h3 id="rec-liq-mid-isa-name" class="text-sm font-bold mt-2 mb-2 truncate">-</h3>
                            </div>
                            <div class="space-y-1 text-xs text-slate-300">
                                <div class="flex justify-between"><span>현재가</span><span id="rec-liq-mid-isa-price" class="font-bold text-white">-</span></div>
                                <div class="flex justify-between items-start"><span>수익률</span><span id="rec-liq-mid-isa-yield" class="font-bold text-emerald-400 text-right">-</span></div>
                                <div class="flex justify-between"><span>청산가</span><span id="rec-liq-mid-isa-liq" class="font-bold text-white">-</span></div>
                            </div>
                        </div>
                        <div class="bg-white/5 p-3.5 rounded-xl border border-white/10 flex flex-col justify-between">
                            <div>
                                <span class="text-[10px] font-bold text-blue-300 bg-blue-500/20 px-2 py-0.5 rounded">0%~예금미만 (일반)</span>
                                <h3 id="rec-liq-mid-gen-name" class="text-sm font-bold mt-2 mb-2 truncate">-</h3>
                            </div>
                            <div class="space-y-1 text-xs text-slate-300">
                                <div class="flex justify-between"><span>현재가</span><span id="rec-liq-mid-gen-price" class="font-bold text-white">-</span></div>
                                <div class="flex justify-between items-start"><span>수익률</span><span id="rec-liq-mid-gen-yield" class="font-bold text-emerald-400 text-right">-</span></div>
                                <div class="flex justify-between"><span>청산가</span><span id="rec-liq-mid-gen-liq" class="font-bold text-white">-</span></div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Filters & Search Bar -->
            <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-200 mb-6 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-7 gap-4 items-center">
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">투자 유형</label>
                    <select id="filter-type" onchange="applyFilters()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                        <option value="all">전체</option>
                        <option value="합병">합병</option>
                        <option value="청산">청산</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">매수 적정</label>
                    <select id="filter-fair" onchange="applyFilters()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                        <option value="all">전체</option>
                        <option value="O">O</option>
                        <option value="X">X</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">거래 상태</label>
                    <select id="filter-status" onchange="applyFilters()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                        <option value="all">전체</option>
                        <option value="일반" selected>일반</option>
                        <option value="정지">정지</option>
                        <option value="합병">합병</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">투자 계좌</label>
                    <select id="filter-account" onchange="applyFilters()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                        <option value="isa" selected>ISA</option>
                        <option value="general">일반</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">청산 연도</label>
                    <select id="filter-delisting-year" onchange="applyFilters()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                        <option value="all" selected>전체</option>
                        <option value="2027">2027</option>
                        <option value="2028">2028</option>
                        <option value="2029">2029</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">예상 수익률</label>
                    <select id="filter-yield-range" onchange="applyFilters()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                        <option value="all" selected>전체</option>
                        <option value="above_deposit">예금 이상(세후)</option>
                        <option value="low_positive">0% 이상 ~ 예금 미만(세후)</option>
                        <option value="negative">0% 미만</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">종목 검색</label>
                    <input type="text" id="search-input" onkeyup="applyFilters()" placeholder="종목명 입력..." class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700">
                </div>
            </div>

            <!-- Data Table -->
            <div class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
                <div class="overflow-x-auto">
                    <table class="w-full text-left border-collapse">
                        <thead>
                            <tr class="bg-slate-100/70 text-slate-600 text-xs uppercase tracking-wider border-b border-slate-200 select-none">
                                <th onclick="sortData('name')" class="py-4 px-6 font-bold cursor-pointer hover:bg-slate-200 transition-colors">종목명 <span id="sort-icon-name" class="text-slate-400">↕</span></th>
                                <th onclick="sortData('current_price')" class="py-4 px-6 font-bold cursor-pointer hover:bg-slate-200 transition-colors">현재가 <span id="sort-icon-current_price" class="text-slate-400">↕</span></th>
                                <th onclick="sortData('liquidation_price')" class="py-4 px-6 font-bold cursor-pointer hover:bg-slate-200 transition-colors">청산가 <span id="sort-icon-liquidation_price" class="text-slate-400">↕</span></th>
                                <th onclick="sortData('is_fair_buy')" class="py-4 px-6 font-bold cursor-pointer hover:bg-slate-200 transition-colors">매수 <span id="sort-icon-is_fair_buy" class="text-slate-400">↕</span></th>
                                <th onclick="sortData('invest_type')" class="py-4 px-6 font-bold cursor-pointer hover:bg-slate-200 transition-colors">유형 <span id="sort-icon-invest_type" class="text-slate-400">↕</span></th>
                                <th onclick="sortData('yield')" class="py-4 px-6 font-bold cursor-pointer hover:bg-slate-200 transition-colors"><span id="yield-header-col">예상수익률</span> <span id="sort-icon-yield" class="text-slate-400">↕</span></th>
                                <th onclick="sortData('delisting_date')" class="py-4 px-6 font-bold cursor-pointer hover:bg-slate-200 transition-colors">청산일 <span id="sort-icon-delisting_date" class="text-slate-400">↕</span></th>
                                <th onclick="sortData('status')" class="py-4 px-6 font-bold cursor-pointer hover:bg-slate-200 transition-colors">상태 <span id="sort-icon-status" class="text-slate-400">↕</span></th>
                            </tr>
                        </thead>
                        <tbody id="spac-table-body" class="divide-y divide-slate-100 text-sm font-medium text-slate-700">
                            <!-- 동적 삽입 -->
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- ========================================================= -->
        <!-- TAB 2: 보유 종목 뷰                                        -->
        <!-- ========================================================= -->
        <div id="view-holdings" class="hidden">
            <!-- 비밀번호 잠금 모달 영역 -->
            <div id="holdings-lock-screen" class="bg-white p-10 rounded-2xl shadow-sm border border-slate-200 text-center max-w-md mx-auto my-20">
                <div class="text-4xl mb-4">🔒</div>
                <h2 class="text-xl font-extrabold text-slate-800 mb-2">보유 종목 탭 보호</h2>
                <p class="text-xs text-slate-500 mb-6">보유 종목에 접근하려면 비밀번호를 입력해주세요.</p>
                <form onsubmit="unlockPrivateTabs(event, 'holdings')" class="space-y-4">
                    <input type="password" id="holdings-password-input" placeholder="비밀번호 4자리 입력" maxlength="4" class="w-full px-4 py-3 text-center text-lg tracking-widest border border-slate-300 rounded-xl font-bold focus:outline-none focus:ring-2 focus:ring-indigo-500" required>
                    <button type="submit" class="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white font-bold rounded-xl transition-colors shadow-sm">잠금 해제</button>
                </form>
            </div>

            <!-- 실제 보유 종목 콘텐츠 -->
            <div id="holdings-content" class="hidden">
                <!-- 1. 보유 종목 요약 카드 -->
                <div class="space-y-3 mb-6">
                    <div class="grid grid-cols-2 gap-3 sm:gap-4">
                        <div class="bg-white p-4 sm:p-6 rounded-2xl shadow-sm border border-slate-200">
                            <span class="text-[11px] sm:text-xs font-bold text-slate-400 uppercase tracking-wider block mb-1">매수금</span>
                            <div id="hold-total-buy" class="text-2xl sm:text-4xl font-black text-slate-800 truncate">- 원</div>
                        </div>
                        <div class="bg-white p-4 sm:p-6 rounded-2xl shadow-sm border border-slate-200">
                            <span class="text-[11px] sm:text-xs font-bold text-indigo-600 uppercase tracking-wider block mb-1">평가금액</span>
                            <div id="hold-total-eval" class="text-2xl sm:text-4xl font-black text-indigo-600 truncate">- 원</div>
                        </div>
                    </div>
                    <div class="grid grid-cols-2 gap-3 sm:gap-4">
                        <div class="bg-white p-4 sm:p-6 rounded-2xl shadow-sm border border-slate-200">
                            <span class="text-[11px] sm:text-xs font-bold text-emerald-600 uppercase tracking-wider block mb-1">단순 수익률</span>
                            <div id="hold-total-simple" class="text-2xl sm:text-4xl font-black text-emerald-600 truncate">- %</div>
                        </div>
                        <div class="bg-white p-4 sm:p-6 rounded-2xl shadow-sm border border-slate-200">
                            <span class="text-[11px] sm:text-xs font-bold text-blue-600 uppercase tracking-wider block mb-1">연환산 수익률</span>
                            <div id="hold-total-annual" class="text-2xl sm:text-4xl font-black text-blue-600 truncate">- %</div>
                        </div>
                    </div>
                </div>

                <!-- 2. 보유 종목 추가 폼 -->
                <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 mb-6">
                    <div class="flex items-center justify-between mb-4">
                        <div>
                            <h2 class="text-base font-bold text-slate-800">📥 등록하기</h2>
                            <p class="text-xs text-slate-400 mt-0.5">수수료 등이 모두 포함된 총 매수금액과 매수량을 입력하면 평단가 기준으로 평가손익이 자동 계산됩니다.</p>
                        </div>
                        <button onclick="lockPrivateTabs()" class="text-xs text-rose-500 hover:underline font-semibold">🔒 잠금</button>
                    </div>
                    <form onsubmit="addHoldingItem(event)" class="space-y-4">
                        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-4">
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">계좌 명의</label>
                                <select id="hold-input-holder" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                                    <option value="남편" selected>남편</option>
                                    <option value="아내">아내</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">계좌 구분</label>
                                <select id="hold-input-account" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                                    <option value="ISA" selected>ISA</option>
                                    <option value="일반">일반</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">종목명</label>
                                <input type="text" id="hold-input-name" list="spac-name-list" placeholder="종목명 입력 또는 선택" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                                <datalist id="spac-name-list"></datalist>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">매수금</label>
                                <input type="number" id="hold-input-amount" placeholder="예: 2005000" min="1" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">매수량</label>
                                <input type="number" id="hold-input-quantity" placeholder="예: 1000" min="1" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">매수일</label>
                                <input type="date" id="hold-input-date" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                            </div>
                        </div>
                        <div>
                            <button type="submit" class="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-bold rounded-xl transition-colors shadow-sm">➕ 보유 종목</button>
                        </div>
                    </form>
                </div>

                <!-- 3. 보유 종목 필터 바 -->
                <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-200 mb-6 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-4 items-center">
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">계좌 명의</label>
                        <select id="hold-filter-holder" onchange="renderHoldings()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                            <option value="all" selected>전체</option>
                            <option value="남편">남편</option>
                            <option value="아내">아내</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">계좌 구분</label>
                        <select id="hold-filter-account" onchange="renderHoldings()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                            <option value="all" selected>전체</option>
                            <option value="ISA">ISA</option>
                            <option value="일반">일반</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">청산 연도</label>
                        <select id="hold-filter-delisting-year" onchange="renderHoldings()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                            <option value="all" selected>전체</option>
                            <option value="2027">2027</option>
                            <option value="2028">2028</option>
                            <option value="2029">2029</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">예금 이상(세후)</label>
                        <select id="hold-filter-benchmark" onchange="renderHoldings()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                            <option value="all" selected>전체</option>
                            <option value="beat">달성</option>
                            <option value="miss">미달</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">거래 상태</label>
                        <select id="hold-filter-status" onchange="renderHoldings()" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                            <option value="all" selected>전체</option>
                            <option value="일반">일반</option>
                            <option value="정지">정지</option>
                            <option value="합병">합병</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">종목 검색</label>
                        <input type="text" id="hold-search-input" onkeyup="renderHoldings()" placeholder="종목명 검색..." class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700">
                    </div>
                </div>

                <!-- 4. 보유 종목 테이블 -->
                <div class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="bg-slate-100/70 text-slate-600 text-xs uppercase tracking-wider border-b border-slate-200 select-none">
                                    <th onclick="sortHoldingData('holder')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors text-center">명의 <span id="hold-sort-icon-holder" class="text-slate-400">↕</span></th>
                                    <th onclick="sortHoldingData('acct')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors text-center">계좌 <span id="hold-sort-icon-acct" class="text-slate-400">↕</span></th>
                                    <th onclick="sortHoldingData('name')" class="py-4 px-5 font-bold cursor-pointer hover:bg-slate-200 transition-colors">종목명 <span id="hold-sort-icon-name" class="text-slate-400">↕</span></th>
                                    <th onclick="sortHoldingData('buy_amount')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors">매수금 <span id="hold-sort-icon-buy_amount" class="text-slate-400">↕</span></th>
                                    <th onclick="sortHoldingData('eval_amount')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors">평가금(손익) <span id="hold-sort-icon-eval_amount" class="text-slate-400">↕</span></th>
                                    <th onclick="sortHoldingData('annualYield')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors">수익률(연환산/단순) <span id="hold-sort-icon-annualYield" class="text-slate-400">↕</span></th>
                                    <th onclick="sortHoldingData('spacStatus')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors text-center">상태 <span id="hold-sort-icon-spacStatus" class="text-slate-400">↕</span></th>
                                    <th class="py-4 px-4 font-bold text-center">관리</th>
                                </tr>
                            </thead>
                            <tbody id="holdings-table-body" class="divide-y divide-slate-100 text-sm font-medium text-slate-700">
                                <!-- 동적 삽입 -->
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

        <!-- ========================================================= -->
        <!-- TAB 3: 성과 관리 뷰                                        -->
        <!-- ========================================================= -->
        <div id="view-performance" class="hidden">
            <!-- 비밀번호 잠금 모달 영역 -->
            <div id="perf-lock-screen" class="bg-white p-10 rounded-2xl shadow-sm border border-slate-200 text-center max-w-md mx-auto my-20">
                <div class="text-4xl mb-4">🔒</div>
                <h2 class="text-xl font-extrabold text-slate-800 mb-2">성과 관리 탭 보호</h2>
                <p class="text-xs text-slate-500 mb-6">성과 관리 탭에 접근하려면 비밀번호를 입력해주세요.</p>
                <form onsubmit="unlockPrivateTabs(event, 'performance')" class="space-y-4">
                    <input type="password" id="perf-password-input" placeholder="비밀번호 4자리 입력" maxlength="4" class="w-full px-4 py-3 text-center text-lg tracking-widest border border-slate-300 rounded-xl font-bold focus:outline-none focus:ring-2 focus:ring-indigo-500" required>
                    <button type="submit" class="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white font-bold rounded-xl transition-colors shadow-sm">잠금 해제</button>
                </form>
            </div>

            <!-- 실제 성과 콘텐츠 -->
            <div id="perf-content" class="hidden">
                <!-- 1. 최상단 요약 카드 바 -->
                <div class="space-y-3 mb-6">
                    <div class="bg-white p-5 sm:p-6 rounded-2xl shadow-sm border border-slate-200">
                        <span class="text-xs font-bold text-slate-400 uppercase tracking-wider block mb-1">손익금액</span>
                        <div id="perf-total-pnl" class="text-3xl sm:text-4xl font-black text-slate-800 truncate">- 원</div>
                    </div>
                    <div class="grid grid-cols-2 gap-3 sm:gap-4">
                        <div class="bg-white p-4 sm:p-6 rounded-2xl shadow-sm border border-slate-200">
                            <span class="text-[11px] sm:text-xs font-bold text-emerald-600 uppercase tracking-wider block mb-1">단순 수익률</span>
                            <div id="perf-avg-simple" class="text-2xl sm:text-4xl font-black text-emerald-600 truncate">- %</div>
                        </div>
                        <div class="bg-white p-4 sm:p-6 rounded-2xl shadow-sm border border-slate-200">
                            <span class="text-[11px] sm:text-xs font-bold text-blue-600 uppercase tracking-wider block mb-1">연환산 수익률</span>
                            <div id="perf-avg-annual" class="text-2xl sm:text-4xl font-black text-blue-600 truncate">- %</div>
                        </div>
                    </div>
                </div>

                <!-- 2. 성과 입력 폼 카드 -->
                <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 mb-6">
                    <div class="flex items-center justify-between mb-4">
                        <div>
                            <h2 class="text-base font-bold text-slate-800">📝 추가하기</h2>
                            <p class="text-xs text-slate-400 mt-0.5">매수일을 입력하면 해당 시점의 한국은행 정기예금 금리(세후 기준)가 자동 조회되어 비교됩니다.</p>
                        </div>
                        <button onclick="lockPrivateTabs()" class="text-xs text-rose-500 hover:underline font-semibold">🔒 잠금</button>
                    </div>
                    <form onsubmit="addPerformanceItem(event)" class="space-y-4">
                        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-4">
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">종목명</label>
                                <input type="text" id="input-name" placeholder="예: 엔에이치스팩31호" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">유형</label>
                                <select id="input-type" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                                    <option value="청산">청산</option>
                                    <option value="매매">매매</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">매수금</label>
                                <input type="number" id="input-buy-amount" placeholder="2000000" min="1" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">매수일</label>
                                <input type="date" id="input-buy-date" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">매도금</label>
                                <input type="number" id="input-sell-amount" placeholder="2050000" min="0" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                            </div>
                            <div>
                                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">매도일</label>
                                <input type="date" id="input-sell-date" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                            </div>
                        </div>
                        <div>
                            <button type="submit" class="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-bold rounded-xl transition-colors shadow-sm">➕ 성과 내역</button>
                        </div>
                    </form>
                </div>

                <!-- 3. 성과 필터 바 -->
                <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-200 mb-6 flex flex-wrap gap-4 items-center justify-between">
                    <div class="flex gap-4 items-center flex-wrap">
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">수익 연도</label>
                            <select id="perf-filter-year" onchange="renderPerformance()" class="h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                                <option value="all" selected>전체</option>
                                <option value="2026">2026</option>
                                <option value="2025">2025</option>
                                <option value="2024">2024</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">투자 유형</label>
                            <select id="perf-filter-type" onchange="renderPerformance()" class="h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                                <option value="all" selected>전체</option>
                                <option value="청산">청산</option>
                                <option value="매매">매매</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">예금 이상(세후)</label>
                            <select id="perf-filter-benchmark" onchange="renderPerformance()" class="h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                                <option value="all" selected>전체</option>
                                <option value="beat">달성</option>
                                <option value="miss">미달</option>
                            </select>
                        </div>
                    </div>
                    <div class="w-full md:w-72">
                        <label class="block text-xs font-bold text-slate-500 mb-1.5 uppercase tracking-wider">종목 검색</label>
                        <input type="text" id="perf-search-input" onkeyup="renderPerformance()" placeholder="종목명 검색..." class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700">
                    </div>
                </div>

                <!-- 4. 성과 내역 테이블 -->
                <div class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="bg-slate-100/70 text-slate-600 text-xs uppercase tracking-wider border-b border-slate-200 select-none">
                                    <th onclick="sortPerformanceData('name')" class="py-4 px-5 font-bold cursor-pointer hover:bg-slate-200 transition-colors">종목명 <span id="perf-sort-icon-name" class="text-slate-400">↕</span></th>
                                    <th onclick="sortPerformanceData('type')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors">유형 <span id="perf-sort-icon-type" class="text-slate-400">↕</span></th>
                                    <th onclick="sortPerformanceData('buy_amount')" class="py-4 px-5 font-bold cursor-pointer hover:bg-slate-200 transition-colors">매수금 <span id="perf-sort-icon-buy_amount" class="text-slate-400">↕</span></th>
                                    <th onclick="sortPerformanceData('buy_date')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors">매수일 <span id="perf-sort-icon-buy_date" class="text-slate-400">↕</span></th>
                                    <th onclick="sortPerformanceData('sell_amount')" class="py-4 px-5 font-bold cursor-pointer hover:bg-slate-200 transition-colors">매도금 <span id="perf-sort-icon-sell_amount" class="text-slate-400">↕</span></th>
                                    <th onclick="sortPerformanceData('sell_date')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors">매도일 <span id="perf-sort-icon-sell_date" class="text-slate-400">↕</span></th>
                                    <th onclick="sortPerformanceData('pnl')" class="py-4 px-5 font-bold cursor-pointer hover:bg-slate-200 transition-colors">손익 <span id="perf-sort-icon-pnl" class="text-slate-400">↕</span></th>
                                    <th onclick="sortPerformanceData('annualYield')" class="py-4 px-4 font-bold cursor-pointer hover:bg-slate-200 transition-colors">수익률(연환산/단순) <span id="perf-sort-icon-annualYield" class="text-slate-400">↕</span></th>
                                    <th class="py-4 px-4 font-bold text-center">관리</th>
                                </tr>
                            </thead>
                            <tbody id="perf-table-body" class="divide-y divide-slate-100 text-sm font-medium text-slate-700">
                                <!-- 동적 삽입 -->
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- ========================================================= -->
    <!-- 매도/청산 완료 처리 모달 -->
    <!-- ========================================================= -->
    <div id="sell-modal" class="fixed inset-0 bg-slate-900/50 backdrop-blur-sm hidden flex items-center justify-center z-50 px-4">
        <div class="bg-white rounded-2xl shadow-xl border border-slate-200 max-w-md w-full p-6">
            <h3 class="text-lg font-extrabold text-slate-800 mb-1">💰 매도/청산 및 성과 기록</h3>
            <p id="sell-modal-subtitle" class="text-xs text-slate-500 mb-5">실제 매도/청산된 총 금액을 입력하면 성과 관리 탭으로 자동 이관됩니다.</p>
            <form onsubmit="confirmSellHolding(event)" class="space-y-4">
                <input type="hidden" id="sell-modal-index">
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">구분</label>
                    <select id="sell-modal-type" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                        <option value="매매">매매 (중도 매도)</option>
                        <option value="청산">청산 (예치금 반환)</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">실제 매도/청산 총금액 (원)</label>
                    <input type="number" id="sell-modal-amount" min="0" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-bold text-slate-800" required>
                </div>
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">매도/청산일 (오늘 날짜 자동)</label>
                    <input type="date" id="sell-modal-date" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl font-medium text-slate-700" required>
                </div>
                <div class="flex justify-end gap-2 pt-2">
                    <button type="button" onclick="closeSellModal()" class="px-4 py-2.5 text-xs font-bold text-slate-600 bg-slate-100 hover:bg-slate-200 rounded-xl transition-colors">취소</button>
                    <button type="submit" class="px-5 py-2.5 text-xs font-bold text-white bg-emerald-600 hover:bg-emerald-700 rounded-xl transition-colors shadow-sm">성과 관리로 이관</button>
                </div>
            </form>
        </div>
    </div>

    <div id="toast-auto" class="hidden bg-slate-900 text-white text-sm font-semibold px-4 py-3 rounded-xl shadow-lg"></div>
    <script>
        let allItems = [];
        let holdingItems = [];
        let performanceItems = [];
        let isUnlocked = false;

        let holdingSortColumn = 'name';
        let holdingSortDirection = 'asc';

        let perfSortColumn = 'sell_date';
        let perfSortDirection = 'desc';

        const DEPOSIT_NET_FACTOR = 0.845;
        const GENERAL_TAX_RATE = 0.154;
        const MERGER_THRESHOLD_DAYS = 365;
        const SEED_KEY = 'spac_seed_applied_v2';
        const EXCLUDED_SPAC_CODES = new Set(['206400', '468760', '474930', '475250']);

        let bokDepositRates = {
            "2024-01": 3.67, "2024-02": 3.63, "2024-03": 3.58, "2024-04": 3.53,
            "2024-05": 3.55, "2024-06": 3.51, "2024-07": 3.41, "2024-08": 3.35,
            "2024-09": 3.40, "2024-10": 3.37, "2024-11": 3.35, "2024-12": 3.22,
            "2025-01": 3.07, "2025-02": 2.97, "2025-03": 2.84, "2025-04": 2.71,
            "2025-05": 2.63, "2025-06": 2.58, "2025-07": 2.55, "2025-08": 2.52,
            "2025-09": 2.50, "2025-10": 2.48, "2025-11": 2.45, "2025-12": 2.45,
            "2026-01": 2.42, "2026-02": 2.40, "2026-03": 2.40, "2026-04": 2.38,
            "2026-05": 2.38, "2026-06": 2.35, "2026-07": 2.35, "2026-08": 2.35,
            "2026-09": 2.35, "2026-10": 2.35, "2026-11": 2.35
        };

        const SEED_HOLDINGS = [
            {"account": "남편", "acct": "일반", "name": "유진스팩11호", "buy_amount": 2119750, "quantity": 1024, "buy_date": "2026-02-24"},
            {"account": "남편", "acct": "일반", "name": "유진스팩10호", "buy_amount": 33840, "quantity": 16, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "유진스팩11호", "buy_amount": 7878700, "quantity": 3806, "buy_date": "2026-02-25"},
            {"account": "남편", "acct": "일반", "name": "에스케이증권제12호스팩", "buy_amount": 125965, "quantity": 61, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "에스케이증권제13호스팩", "buy_amount": 9999600, "quantity": 4854, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "하나33호스팩", "buy_amount": 5973990, "quantity": 2819, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "에스케이증권제12호스팩", "buy_amount": 9879310, "quantity": 4784, "buy_date": "2026-02-24"},
            {"account": "남편", "acct": "일반", "name": "미래에셋비전스팩4호", "buy_amount": 9998460, "quantity": 4761, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "신한제14호스팩", "buy_amount": 9994920, "quantity": 4728, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "디비금융제13호스팩", "buy_amount": 1014010, "quantity": 484, "buy_date": "2026-02-24"},
            {"account": "남편", "acct": "일반", "name": "미래에셋비전스팩7호", "buy_amount": 6552030, "quantity": 3196, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "유진스팩10호", "buy_amount": 9968760, "quantity": 4680, "buy_date": "2026-02-24"},
            {"account": "남편", "acct": "일반", "name": "한화플러스제5호스팩", "buy_amount": 10000260, "quantity": 4878, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "하나34호스팩", "buy_amount": 9532980, "quantity": 4583, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "디비금융제13호스팩", "buy_amount": 8916640, "quantity": 4256, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "키움제11호스팩", "buy_amount": 9670480, "quantity": 4583, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "신한제15호스팩", "buy_amount": 9998460, "quantity": 4830, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "이베스트스팩6호", "buy_amount": 10000350, "quantity": 4914, "buy_date": "2026-02-24"},
            {"account": "남편", "acct": "일반", "name": "키움제10호스팩", "buy_amount": 9998230, "quantity": 4750, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "미래에셋비전스팩6호", "buy_amount": 9999600, "quantity": 4854, "buy_date": "2026-02-23"},
            {"account": "남편", "acct": "일반", "name": "KB제31호스팩", "buy_amount": 707595, "quantity": 341, "buy_date": "2026-02-24"},
            {"account": "남편", "acct": "일반", "name": "하나33호스팩", "buy_amount": 3999390, "quantity": 1882, "buy_date": "2026-02-25"},
            {"account": "남편", "acct": "일반", "name": "미래에셋비전스팩7호", "buy_amount": 436660, "quantity": 213, "buy_date": "2026-02-25"},
            {"account": "남편", "acct": "일반", "name": "하나34호스팩", "buy_amount": 437860, "quantity": 210, "buy_date": "2026-02-25"},
            {"account": "남편", "acct": "일반", "name": "KB제31호스팩", "buy_amount": 9298405, "quantity": 4481, "buy_date": "2026-02-25"},
            {"account": "남편", "acct": "일반", "name": "유안타제17호스팩", "buy_amount": 10000350, "quantity": 4914, "buy_date": "2026-02-25"},
            {"account": "남편", "acct": "일반", "name": "키움제11호스팩", "buy_amount": 10500, "quantity": 5, "buy_date": "2026-02-26"},
            {"account": "남편", "acct": "일반", "name": "미래에셋비전스팩7호", "buy_amount": 41000, "quantity": 20, "buy_date": "2026-03-03"},
            {"account": "남편", "acct": "일반", "name": "하나33호스팩", "buy_amount": 189520, "quantity": 92, "buy_date": "2026-07-22"},
            {"account": "남편", "acct": "일반", "name": "신한제16호스팩", "buy_amount": 3160110, "quantity": 1580, "buy_date": "2026-09-29"},
            {"account": "남편", "acct": "ISA", "name": "한국제15호스팩", "buy_amount": 801010, "quantity": 390, "buy_date": "2026-07-20"},
            {"account": "남편", "acct": "ISA", "name": "유안타제15호스팩", "buy_amount": 2119610, "quantity": 1027, "buy_date": "2026-02-12"},
            {"account": "남편", "acct": "ISA", "name": "신한제13호스팩", "buy_amount": 292360, "quantity": 137, "buy_date": "2026-03-24"},
            {"account": "남편", "acct": "ISA", "name": "유안타제16호스팩", "buy_amount": 2253100, "quantity": 1097, "buy_date": "2026-03-26"},
            {"account": "남편", "acct": "ISA", "name": "에이치엠씨제7호스팩", "buy_amount": 1963405, "quantity": 963, "buy_date": "2026-03-26"},
            {"account": "남편", "acct": "ISA", "name": "신한제14호스팩", "buy_amount": 2518055, "quantity": 1229, "buy_date": "2026-04-17"},
            {"account": "남편", "acct": "ISA", "name": "유안타제17호스팩", "buy_amount": 1073350, "quantity": 533, "buy_date": "2026-05-04"},
            {"account": "남편", "acct": "ISA", "name": "유안타제17호스팩", "buy_amount": 32220, "quantity": 16, "buy_date": "2026-05-07"},
            {"account": "남편", "acct": "ISA", "name": "유안타제17호스팩", "buy_amount": 978700, "quantity": 486, "buy_date": "2026-05-12"},
            {"account": "남편", "acct": "ISA", "name": "유진스팩11호", "buy_amount": 2030, "quantity": 1, "buy_date": "2026-05-15"},
            {"account": "남편", "acct": "ISA", "name": "키움제11호스팩", "buy_amount": 2302715, "quantity": 1135, "buy_date": "2026-05-18"},
            {"account": "남편", "acct": "ISA", "name": "키움제10호스팩", "buy_amount": 3189720, "quantity": 1580, "buy_date": "2026-05-19"},
            {"account": "남편", "acct": "ISA", "name": "신한제15호스팩", "buy_amount": 2503710, "quantity": 1222, "buy_date": "2026-05-19"},
            {"account": "남편", "acct": "ISA", "name": "디비금융제13호스팩", "buy_amount": 2503720, "quantity": 1225, "buy_date": "2026-05-19"},
            {"account": "남편", "acct": "ISA", "name": "하나34호스팩", "buy_amount": 2503700, "quantity": 1228, "buy_date": "2026-05-27"},
            {"account": "남편", "acct": "ISA", "name": "KB제30호스팩", "buy_amount": 2202525, "quantity": 1075, "buy_date": "2026-05-29"},
            {"account": "남편", "acct": "ISA", "name": "유진스팩11호", "buy_amount": 20380, "quantity": 10, "buy_date": "2026-05-29"},
            {"account": "남편", "acct": "ISA", "name": "교보17호스팩", "buy_amount": 1112215, "quantity": 535, "buy_date": "2026-05-29"},
            {"account": "남편", "acct": "ISA", "name": "유진스팩11호", "buy_amount": 2095940, "quantity": 1018, "buy_date": "2026-06-01"},
            {"account": "남편", "acct": "ISA", "name": "교보17호스팩", "buy_amount": 998835, "quantity": 473, "buy_date": "2026-06-01"},
            {"account": "남편", "acct": "ISA", "name": "한국제15호스팩", "buy_amount": 1201510, "quantity": 585, "buy_date": "2026-06-26"},
            {"account": "남편", "acct": "ISA", "name": "KB제31호스팩", "buy_amount": 2002660, "quantity": 992, "buy_date": "2026-07-14"},
            {"account": "남편", "acct": "ISA", "name": "교보16호스팩", "buy_amount": 1201060, "quantity": 592, "buy_date": "2026-06-26"},
            {"account": "남편", "acct": "ISA", "name": "KB제30호스팩", "buy_amount": 1254870, "quantity": 617, "buy_date": "2026-06-26"},
            {"account": "남편", "acct": "ISA", "name": "엔에이치스팩31호", "buy_amount": 1209930, "quantity": 582, "buy_date": "2026-07-16"},
            {"account": "남편", "acct": "ISA", "name": "대신밸런스제19호스팩", "buy_amount": 2003295, "quantity": 973, "buy_date": "2026-07-16"},
            {"account": "남편", "acct": "ISA", "name": "신한제13호스팩", "buy_amount": 1583790, "quantity": 760, "buy_date": "2026-07-20"},
            {"account": "남편", "acct": "ISA", "name": "엔에이치스팩31호", "buy_amount": 800665, "quantity": 387, "buy_date": "2026-07-20"},
            {"account": "남편", "acct": "ISA", "name": "에스케이증권제13호스팩", "buy_amount": 1017385, "quantity": 499, "buy_date": "2026-07-22"},
            {"account": "남편", "acct": "ISA", "name": "에스케이증권제13호스팩", "buy_amount": 1122670, "quantity": 552, "buy_date": "2026-08-05"},
            {"account": "남편", "acct": "ISA", "name": "교보16호스팩", "buy_amount": 801010, "quantity": 390, "buy_date": "2026-08-05"},
            {"account": "남편", "acct": "ISA", "name": "유진스팩11호", "buy_amount": 2003580, "quantity": 990, "buy_date": "2026-08-11"},
            {"account": "남편", "acct": "ISA", "name": "교보17호스팩", "buy_amount": 2002455, "quantity": 987, "buy_date": "2026-08-11"},
            {"account": "남편", "acct": "ISA", "name": "디비금융제13호스팩", "buy_amount": 419965, "quantity": 207, "buy_date": "2026-08-11"},
            {"account": "남편", "acct": "ISA", "name": "신한제14호스팩", "buy_amount": 1651380, "quantity": 818, "buy_date": "2026-08-12"},
            {"account": "남편", "acct": "ISA", "name": "디비금융제13호스팩", "buy_amount": 1100950, "quantity": 544, "buy_date": "2026-08-12"},
            {"account": "남편", "acct": "ISA", "name": "신한제15호스팩", "buy_amount": 1542150, "quantity": 762, "buy_date": "2026-08-13"},
            {"account": "남편", "acct": "ISA", "name": "KB제30호스팩", "buy_amount": 107790, "quantity": 53, "buy_date": "2026-08-21"},
            {"account": "남편", "acct": "ISA", "name": "유안타제17호스팩", "buy_amount": 1027030, "quantity": 510, "buy_date": "2026-08-21"},
            {"account": "남편", "acct": "ISA", "name": "유안타제17호스팩", "buy_amount": 1742500, "quantity": 861, "buy_date": "2026-08-24"},
            {"account": "남편", "acct": "ISA", "name": "대신밸런스제19호스팩", "buy_amount": 1677790, "quantity": 809, "buy_date": "2026-09-30"},
            {"account": "남편", "acct": "ISA", "name": "하나34호스팩", "buy_amount": 186650, "quantity": 92, "buy_date": "2026-09-30"},
            {"account": "남편", "acct": "ISA", "name": "키움제10호스팩", "buy_amount": 1803000, "quantity": 880, "buy_date": "2026-09-30"},
            {"account": "남편", "acct": "ISA", "name": "신한제15호스팩", "buy_amount": 1000965, "quantity": 491, "buy_date": "2026-09-30"},
            {"account": "남편", "acct": "ISA", "name": "신한제14호스팩", "buy_amount": 850140, "quantity": 418, "buy_date": "2026-10-06"},
            {"account": "남편", "acct": "ISA", "name": "교보17호스팩", "buy_amount": 901790, "quantity": 438, "buy_date": "2026-10-06"},
            {"account": "남편", "acct": "ISA", "name": "하나34호스팩", "buy_amount": 294310, "quantity": 144, "buy_date": "2026-10-07"},
            {"account": "남편", "acct": "ISA", "name": "KB제31호스팩", "buy_amount": 2072480, "quantity": 1019, "buy_date": "2026-10-13"}
        ];

        function toAfterTaxDepositRate(preTaxRate) {
            const r = toFiniteNumber(preTaxRate);
            if (r === null) return null;
            return r * DEPOSIT_NET_FACTOR;
        }

        function normName(n) {
            return String(n || '').replace(/\s+/g, '').replace(/^KB/i, '케이비').replace(/^IBKS/i, '아이비케이에스').toLowerCase();
        }
        function findSpacByName(name) {
            const k = normName(name);
            return allItems.find(s => s.name === name) || allItems.find(s => normName(s.name) === k);
        }
        function getHoldingHolder(item) {
            const h = String(item?.account || item?.accountHolder || item?.holder || '남편').trim();
            return h === '아내' ? '아내' : '남편';
        }
        function getHoldingUniqueKey(item) {
            return [
                getHoldingHolder(item),
                item.acct || '일반',
                normName(item.name),
                Number(item.buy_amount || 0),
                Number(item.quantity || 0),
                String(item.buy_date || '')
            ].join('|');
        }
        function deduplicateHoldings(items) {
            const seen = new Set();
            const out = [];
            for (const item of items) {
                if (!item) continue;
                const normalized = { ...item, account: getHoldingHolder(item) };
                const key = getHoldingUniqueKey(normalized);
                if (!seen.has(key)) {
                    seen.add(key);
                    out.push(normalized);
                }
            }
            return out;
        }
        function saveHoldings() {
            holdingItems = deduplicateHoldings(holdingItems);
            try {
                localStorage.setItem('spac_holding_items_v5', JSON.stringify(holdingItems));
            } catch (e) {}
        }
        function savePerformance() {
            try {
                localStorage.setItem('spac_performance_items_v3', JSON.stringify(performanceItems));
            } catch (e) {}
        }

        function applySeedHoldings() {
            holdingItems = deduplicateHoldings(holdingItems);
            try {
                if (localStorage.getItem(SEED_KEY)) {
                    saveHoldings();
                    return;
                }
            } catch (e) { return; }
            holdingItems = deduplicateHoldings(holdingItems.concat(SEED_HOLDINGS.map(r => ({ ...r, account: getHoldingHolder(r) }))));
            saveHoldings();
            try {
                localStorage.setItem(SEED_KEY, new Date().toISOString());
            } catch (e) {}
        }

        function showToast(msg) {
            const el = document.getElementById('toast-auto');
            if (!el) return;
            el.innerText = msg;
            el.classList.remove('hidden');
            clearTimeout(window.__toastTimer);
            window.__toastTimer = setTimeout(() => el.classList.add('hidden'), 6000);
        }

        function hasConfirmedLiquidationNotice(liqItem) {
            if (!liqItem || typeof liqItem !== 'object') return false;
            const reportNm = String(liqItem.report_nm || '').trim();
            if (!reportNm) return false;
            return ['청산', '해산', '분배금', '예치금 반환'].some(k => reportNm.includes(k));
        }

        function toFiniteNumber(val) {
            if (val === null || val === undefined || val === '') return null;
            if (typeof val === 'number') return Number.isFinite(val) ? val : null;
            const cleaned = String(val).replace(/,/g, '').replace(/%/g, '').replace(/%p/g, '').trim();
            if (!cleaned || cleaned === '-' || cleaned === '미확인' || cleaned === 'N/A' || cleaned === 'null') return null;
            const num = Number(cleaned);
            return Number.isFinite(num) ? num : null;
        }

        function parseDateOnly(dateStr) {
            if (!dateStr) return null;
            const cleaned = String(dateStr).trim();
            const match = cleaned.match(/^(\d{4})-?(\d{2})-?(\d{2})/);
            if (match) {
                const dt = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
                return Number.isNaN(dt.getTime()) ? null : dt;
            }
            const parsed = new Date(cleaned);
            if (Number.isNaN(parsed.getTime())) return null;
            return new Date(Date.UTC(parsed.getUTCFullYear(), parsed.getUTCMonth(), parsed.getUTCDate()));
        }

        function getDelistingDateValue(item) {
            if (!item || typeof item !== 'object') return '';
            return item.delisting_date || item.liquidation_date || item.maturity_date || '';
        }

        function getDaysUntilDelisting(dateStr) {
            const target = parseDateOnly(dateStr);
            if (!target) return null;
            const today = parseDateOnly(getTodayStr());
            if (!today) return null;
            const diffMs = target.getTime() - today.getTime();
            return Math.floor(diffMs / (1000 * 60 * 60 * 24));
        }

        // 상장폐지·만기경과·수익률누락·청산/상폐사유 종목 제외 판별
        function isExpiredOrDelistedSpac(item) {
            if (!item || typeof item !== 'object') return true;
            const code = String(item.code || '').trim().padStart(6, '0');
            if (EXCLUDED_SPAC_CODES.has(code)) return true;

            const rawStatus = String(item.status || '').trim();
            if (rawStatus === '상장폐지') return true;

            const delistingDate = getDelistingDateValue(item);
            if (!delistingDate) return true;

            const remDays = toFiniteNumber(item.remaining_days);
            if (remDays === null || remDays <= 0) return true;

            const daysLeft = getDaysUntilDelisting(delistingDate);
            if (daysLeft === null || daysLeft <= 0) return true;

            const yIsa = toFiniteNumber(item.yield_isa);
            const yGen = toFiniteNumber(item.yield_general);
            if (yIsa === null && yGen === null) return true;

            const dept = String(item.krx_dept || '');
            const disclosures = Array.isArray(item.recent_disclosures) ? item.recent_disclosures : [];
            const hasDelistReason = disclosures.some(d => String(d?.report_nm || '').includes('상장폐지 사유발생'));
            if (hasDelistReason || Boolean(item.has_liquidation_notice) || dept.includes('관리종목')) {
                return true;
            }

            return false;
        }

        function normalizeStatus(status) {
            const s = String(status || '').trim();
            if (!s || s === '정상') return '일반';
            return s;
        }

        function normalizeInvestType(item) {
            const delistingDate = getDelistingDateValue(item);
            const daysLeft = getDaysUntilDelisting(delistingDate);
            if (daysLeft !== null) {
                return daysLeft >= MERGER_THRESHOLD_DAYS ? '합병' : '청산';
            }
            const raw = String(item?.invest_type ?? item?.type ?? item?.category ?? '').trim();
            if (raw.includes('합병')) return '합병';
            return '청산';
        }

        function normalizeFairBuy(item) {
            const raw = item?.is_fair_buy ?? item?.fair_buy ?? item?.isFairBuy;
            if (raw === true || String(raw).trim().toUpperCase() === 'O' || String(raw).trim().toUpperCase() === 'Y') return 'O';
            if (raw === false || String(raw).trim().toUpperCase() === 'X' || String(raw).trim().toUpperCase() === 'N') return 'X';
            return raw ? String(raw).trim() : null;
        }

        function getCurrentBaseDepositRate() {
            const fromItem = allItems.length > 0 ? toFiniteNumber(allItems[0].deposit_rate) : null;
            if (fromItem !== null && fromItem > 0) return toAfterTaxDepositRate(fromItem);
            const latestKey = Object.keys(bokDepositRates).sort().pop();
            const fromMap = latestKey ? toFiniteNumber(bokDepositRates[latestKey]) : null;
            return toAfterTaxDepositRate(fromMap !== null ? fromMap : 2.35);
        }

        function calculateSingleAnnualYield(buyAmount, sellAmount, buyDate, sellDate) {
            if (!buyAmount || buyAmount <= 0 || sellAmount === null || sellAmount === undefined || sellAmount < 0) return -100;
            const bDt = buyDate instanceof Date ? buyDate : parseDateOnly(buyDate);
            const sDt = sellDate instanceof Date ? sellDate : parseDateOnly(sellDate);
            if (!bDt || !sDt) return 0;
            const diffDays = (sDt.getTime() - bDt.getTime()) / (1000 * 60 * 60 * 24);
            if (diffDays <= 0) {
                return ((sellAmount - buyAmount) / buyAmount) * 100;
            }
            const days = Math.max(1, diffDays);
            const ratio = sellAmount / buyAmount;
            return (Math.pow(ratio, 365.0 / days) - 1) * 100;
        }

        function computeFallbackYieldFromSpac(item, accountType) {
            const curPrice = toFiniteNumber(item?.current_price);
            const liqPrice = toFiniteNumber(item?.liquidation_price);
            const targetDateStr = getDelistingDateValue(item) || item?.expected_date;
            if (curPrice && curPrice > 0 && liqPrice && liqPrice > 0) {
                const grossRatio = liqPrice / curPrice;
                const afterTaxPrice = accountType === 'general' && liqPrice > curPrice
                    ? curPrice + (liqPrice - curPrice) * (1 - GENERAL_TAX_RATE)
                    : liqPrice;
                const netRatio = afterTaxPrice / curPrice;
                if (targetDateStr) {
                    const diffDays = getDaysUntilDelisting(targetDateStr);
                    if (diffDays !== null && diffDays >= 1) {
                        return (Math.pow(netRatio, 365.0 / diffDays) - 1) * 100;
                    }
                }
                return (grossRatio - 1) * 100;
            }
            return null;
        }

        function resolveAccountYield(item, accountType) {
            if (!item) return null;
            const isIsa = accountType === 'isa';
            const directCandidates = isIsa
                ? [item.yield_isa, item.isa_yield, item.expected_yield_isa]
                : [item.yield_general, item.general_yield, item.expected_yield_general];

            for (const c of directCandidates) {
                const n = toFiniteNumber(c);
                if (n !== null) return n;
            }

            const genericCandidates = [item.expected_yield, item.annual_yield, item.yield, item.return_rate];
            for (const g of genericCandidates) {
                const n = toFiniteNumber(g);
                if (n !== null) {
                    if (!isIsa && n > 0 && (item.yield_general === undefined || item.yield_general === null)) {
                        return n * (1 - GENERAL_TAX_RATE);
                    }
                    return n;
                }
            }

            return computeFallbackYieldFromSpac(item, accountType);
        }

        function formatSpreadPp(yieldVal, baseAfterTaxRate) {
            const y = toFiniteNumber(yieldVal);
            const b = toFiniteNumber(baseAfterTaxRate);
            if (y === null || b === null) return null;
            const diff = y - b;
            const sign = diff > 0.05 ? '+' : (diff < -0.05 ? '-' : '');
            return `(${sign}${Math.abs(diff).toFixed(1)}%p)`;
        }

        async function processLiquidations() {
            let list = [];
            try {
                const r = await fetch('data/liquidations.json?t=' + Date.now());
                if (!r.ok) return;
                const j = await r.json();
                list = Array.isArray(j) ? j : (j.items || []);
            } catch (e) { return; }
            if (!list.length) return;

            const today = getTodayStr();
            const liqMap = {};
            list.forEach(l => {
                if (!hasConfirmedLiquidationNotice(l)) return;
                const unitPrice = toFiniteNumber(l && l.unit_price);
                const payDate = l && typeof l.pay_date === 'string' ? l.pay_date.trim() : '';
                if (l && l.name && payDate && unitPrice !== null && unitPrice > 0) {
                    const key = normName(l.name);
                    if (!liqMap[key]) {
                        liqMap[key] = { ...l, unit_price: unitPrice, pay_date: payDate };
                    }
                }
            });

            let moved = 0;
            const remain = [];
            holdingItems.forEach(item => {
                const l = liqMap[normName(item.name)];
                if (!l || l.pay_date > today || !(item.quantity > 0)) { remain.push(item); return; }
                const acct = item.acct || '일반';
                const gross = Math.round(item.quantity * l.unit_price);
                const gain = gross - item.buy_amount;
                const tax = acct === '일반' ? Math.floor(Math.max(0, gain) * GENERAL_TAX_RATE) : 0;
                const rec = {
                    name: item.name, type: '청산', buy_amount: item.buy_amount, buy_date: item.buy_date,
                    sell_amount: gross - tax, sell_date: l.pay_date, acct, account: getHoldingHolder(item),
                    report_nm: l.report_nm, auto: true
                };
                if (tax > 0) rec.tax = tax;
                performanceItems.unshift(rec);
                moved++;
            });
            if (moved > 0) {
                holdingItems = remain;
                saveHoldings();
                savePerformance();
                showToast(`청산 공시 확인 ${moved}건이 성과 관리로 자동 이관되었습니다.`);
                renderHoldings();
                renderPerformance();
            }
        }

        function getTodayStr() {
            const now = new Date();
            const yyyy = now.getFullYear();
            const mm = String(now.getMonth() + 1).padStart(2, '0');
            const dd = String(now.getDate()).padStart(2, '0');
            return `${yyyy}-${mm}-${dd}`;
        }

        function calculateXIRR(cashFlows) {
            if (!cashFlows || cashFlows.length < 2) return 0;
            const validFlows = cashFlows
                .map(cf => ({
                    date: cf.date instanceof Date ? cf.date : parseDateOnly(cf.date),
                    amount: Number(cf.amount)
                }))
                .filter(cf => cf.date && !Number.isNaN(cf.date.getTime()) && Number.isFinite(cf.amount));

            if (validFlows.length < 2) return 0;
            validFlows.sort((a, b) => a.date.getTime() - b.date.getTime());

            if (validFlows.length === 2) {
                return calculateSingleAnnualYield(Math.abs(validFlows[0].amount), validFlows[1].amount, validFlows[0].date, validFlows[1].date);
            }

            const t0 = validFlows[0].date;
            const hasPos = validFlows.some(cf => cf.amount > 0);
            const hasNeg = validFlows.some(cf => cf.amount < 0);
            if (!hasPos || !hasNeg) return 0;

            const evalNpv = (r) => {
                let npv = 0;
                for (const cf of validFlows) {
                    const years = (cf.date.getTime() - t0.getTime()) / (1000 * 60 * 60 * 24 * 365.0);
                    npv += cf.amount / Math.pow(1 + r, years);
                }
                return npv;
            };

            let rate = 0.05;
            const maxIter = 100;
            const tol = 1e-7;

            for (let i = 0; i < maxIter; i++) {
                let npv = 0;
                let dnpv = 0;
                for (const cf of validFlows) {
                    const years = (cf.date.getTime() - t0.getTime()) / (1000 * 60 * 60 * 24 * 365.0);
                    const denom = Math.pow(1 + rate, years);
                    npv += cf.amount / denom;
                    dnpv -= years * cf.amount / (denom * (1 + rate));
                }
                if (!isFinite(npv) || !isFinite(dnpv)) break;
                if (Math.abs(npv) < tol) return rate * 100;
                if (Math.abs(dnpv) < 1e-12) break;

                let newRate = rate - npv / dnpv;
                if (newRate <= -0.999999) newRate = -0.999999;
                if (Math.abs(newRate - rate) < tol) return newRate * 100;
                rate = newRate;
            }

            let low = -0.9999;
            let high = 100.0;
            for (let i = 0; i < 100; i++) {
                const mid = (low + high) / 2;
                const val = evalNpv(mid);
                if (Math.abs(val) < tol) return mid * 100;
                if (val > 0) low = mid;
                else high = mid;
            }
            return ((low + high) / 2) * 100;
        }

        function getBokRateForDate(dateStr) {
            let rawRate = 3.2;
            if (dateStr && dateStr.length >= 7) {
                const ym = dateStr.substring(0, 7);
                if (bokDepositRates[ym] !== undefined) {
                    rawRate = bokDepositRates[ym];
                } else {
                    const sortedKeys = Object.keys(bokDepositRates).sort();
                    let fallbackRate = sortedKeys.length > 0 ? bokDepositRates[sortedKeys[sortedKeys.length - 1]] : 3.2;
                    for (const k of sortedKeys) {
                        if (k <= ym) fallbackRate = bokDepositRates[k];
                    }
                    rawRate = fallbackRate;
                }
            }
            return toAfterTaxDepositRate(rawRate) ?? (rawRate * DEPOSIT_NET_FACTOR);
        }

        function switchTab(tabName) {
            const tabs = ['dashboard', 'holdings', 'performance'];
            tabs.forEach(t => {
                const btn = document.getElementById(`tab-btn-${t}`);
                const view = document.getElementById(`view-${t}`);
                if (t === tabName) {
                    btn.className = "px-3.5 py-2 rounded-lg text-xs font-extrabold transition-all bg-white text-indigo-600 shadow-sm";
                    view.classList.remove('hidden');
                } else {
                    btn.className = "px-3.5 py-2 rounded-lg text-xs font-extrabold transition-all text-slate-600 hover:text-slate-900";
                    view.classList.add('hidden');
                }
            });

            if (tabName === 'holdings' && isUnlocked) renderHoldings();
            if (tabName === 'performance' && isUnlocked) renderPerformance();
        }

        function unlockPrivateTabs(event, sourceTab) {
            event.preventDefault();
            const inputId = sourceTab === 'holdings' ? 'holdings-password-input' : 'perf-password-input';
            const pw = document.getElementById(inputId).value;
            if (pw === '0913') {
                isUnlocked = true;
                document.getElementById('holdings-lock-screen').classList.add('hidden');
                document.getElementById('holdings-content').classList.remove('hidden');
                document.getElementById('perf-lock-screen').classList.add('hidden');
                document.getElementById('perf-content').classList.remove('hidden');
                renderHoldings();
                renderPerformance();
            } else {
                alert('비밀번호가 올바르지 않습니다.');
                document.getElementById(inputId).value = '';
            }
        }

        function lockPrivateTabs() {
            isUnlocked = false;
            document.getElementById('holdings-password-input').value = '';
            document.getElementById('perf-password-input').value = '';
            document.getElementById('holdings-content').classList.add('hidden');
            document.getElementById('holdings-lock-screen').classList.remove('hidden');
            document.getElementById('perf-content').classList.add('hidden');
            document.getElementById('perf-lock-screen').classList.remove('hidden');
        }

        function addHoldingItem(event) {
            event.preventDefault();
            const holderEl = document.getElementById('hold-input-holder');
            const newItem = {
                account: holderEl ? holderEl.value : '남편',
                acct: document.getElementById('hold-input-account').value,
                name: document.getElementById('hold-input-name').value.trim(),
                buy_amount: Number(document.getElementById('hold-input-amount').value),
                quantity: Number(document.getElementById('hold-input-quantity').value),
                buy_date: document.getElementById('hold-input-date').value
            };

            const dupKey = getHoldingUniqueKey(newItem);
            if (holdingItems.some(existing => getHoldingUniqueKey(existing) === dupKey)) {
                alert('동일한 명의·계좌·종목·매수금·매수량·매수일의 보유 내역이 이미 등록되어 있습니다.');
                return;
            }

            holdingItems.unshift(newItem);
            saveHoldings();

            document.getElementById('hold-input-name').value = '';
            document.getElementById('hold-input-amount').value = '';
            document.getElementById('hold-input-quantity').value = '';
            document.getElementById('hold-input-date').value = '';

            renderHoldings();
            alert('보유 종목이 등록되었습니다.');
        }

        function deleteHoldingItem(index) {
            if (confirm('해당 보유 종목을 삭제하시겠습니까?')) {
                holdingItems.splice(index, 1);
                saveHoldings();
                renderHoldings();
            }
        }

        function openSellModal(index, estimatedEval) {
            const item = holdingItems[index];
            if (!item) return;
            const holder = getHoldingHolder(item);
            document.getElementById('sell-modal-index').value = index;
            document.getElementById('sell-modal-subtitle').innerText = `[${holder} - ${item.acct || '일반'} - ${item.name}] 매수금 ${item.buy_amount.toLocaleString()}원 (${item.buy_date})`;
            document.getElementById('sell-modal-amount').value = estimatedEval > 0 ? Math.round(estimatedEval) : item.buy_amount;
            document.getElementById('sell-modal-date').value = getTodayStr();
            document.getElementById('sell-modal').classList.remove('hidden');
        }

        function closeSellModal() {
            document.getElementById('sell-modal').classList.add('hidden');
        }

        function confirmSellHolding(event) {
            event.preventDefault();
            const index = Number(document.getElementById('sell-modal-index').value);
            const item = holdingItems[index];
            if (!item) return;

            const perfRecord = {
                name: item.name,
                type: document.getElementById('sell-modal-type').value,
                buy_amount: item.buy_amount,
                buy_date: item.buy_date,
                sell_amount: Number(document.getElementById('sell-modal-amount').value),
                sell_date: document.getElementById('sell-modal-date').value,
                acct: item.acct || '일반',
                account: getHoldingHolder(item)
            };

            performanceItems.unshift(perfRecord);
            savePerformance();

            holdingItems.splice(index, 1);
            saveHoldings();

            closeSellModal();
            renderHoldings();
            renderPerformance();
            alert('매도/청산 완료 처리되어 성과 관리 탭에 자동으로 기록되었습니다.');
        }

        function sortHoldingData(column) {
            if (holdingSortColumn === column) {
                holdingSortDirection = holdingSortDirection === 'asc' ? 'desc' : 'asc';
            } else {
                holdingSortColumn = column;
                holdingSortDirection = (column === 'holder' || column === 'acct' || column === 'name' || column === 'spacStatus') ? 'asc' : 'desc';
            }
            renderHoldings();
        }

        function updateHoldingSortIcons() {
            const columns = ['holder', 'acct', 'name', 'buy_amount', 'eval_amount', 'annualYield', 'spacStatus'];
            columns.forEach(col => {
                const iconEl = document.getElementById(`hold-sort-icon-${col}`);
                if (iconEl) {
                    if (holdingSortColumn === col) {
                        iconEl.innerText = holdingSortDirection === 'asc' ? ' ▲' : ' ▼';
                        iconEl.className = 'text-indigo-600 font-bold';
                    } else {
                        iconEl.innerText = ' ↕';
                        iconEl.className = 'text-slate-400';
                    }
                }
            });
        }

        function renderHoldings() {
            if (!isUnlocked) return;

            const tbody = document.getElementById('holdings-table-body');
            tbody.innerHTML = '';

            const filterHolderEl = document.getElementById('hold-filter-holder');
            const filterHolder = filterHolderEl ? filterHolderEl.value : 'all';
            const filterAccount = document.getElementById('hold-filter-account').value;
            const filterDelYear = document.getElementById('hold-filter-delisting-year').value;
            const filterBenchmark = document.getElementById('hold-filter-benchmark').value;
            const filterStatus = document.getElementById('hold-filter-status').value;
            const searchKeyword = document.getElementById('hold-search-input').value.trim().toLowerCase();

            let totalBuy = 0;
            let totalEval = 0;
            const todayStr = getTodayStr();
            const todayDate = parseDateOnly(todayStr);
            const totalCashFlows = [];

            if (holdingItems.length === 0) {
                tbody.innerHTML = `<tr><td colspan="8" class="py-12 text-center text-slate-400 font-normal">등록된 보유 종목이 없습니다. 위 폼에서 보유 중인 스팩을 추가해보세요.</td></tr>`;
                document.getElementById('hold-total-buy').innerText = '0 원';
                document.getElementById('hold-total-eval').innerText = '0 원';
                document.getElementById('hold-total-simple').innerText = '0.00 %';
                document.getElementById('hold-total-annual').innerText = '0.00 %';
                return;
            }

            const enrichedHoldings = holdingItems.map((item, idx) => {
                const matchedSpac = findSpacByName(item.name);
                const currentPrice = matchedSpac ? (toFiniteNumber(matchedSpac.current_price) ?? 0) : 0;
                const delistingDate = matchedSpac ? getDelistingDateValue(matchedSpac) : '';
                const delistingYear = delistingDate ? delistingDate.substring(0, 4) : '';
                const spacStatus = matchedSpac ? normalizeStatus(matchedSpac.status) : '일반';
                const accountOwner = getHoldingHolder(item);
                const acct = item.acct || '일반';

                let estimatedEval = item.buy_amount;
                if (item.quantity && item.quantity > 0 && currentPrice > 0) {
                    estimatedEval = Math.round(item.quantity * currentPrice);
                }

                const buyDateObj = parseDateOnly(item.buy_date) || todayDate;
                const currentAnnual = calculateSingleAnnualYield(item.buy_amount, estimatedEval, buyDateObj, todayDate);
                const evalDiff = estimatedEval - item.buy_amount;
                const simpleYield = item.buy_amount > 0 ? (evalDiff / item.buy_amount) * 100 : 0;
                const afterTaxDepositRate = getBokRateForDate(item.buy_date);
                const isBeat = currentAnnual >= afterTaxDepositRate;

                return { ...item, account: accountOwner, holder: accountOwner, acct, origIndex: idx, estimatedEval, evalDiff, simpleYield, currentAnnual, afterTaxDepositRate, isBeat, delistingYear, spacStatus };
            });

            const filteredHoldings = enrichedHoldings.filter(item => {
                if (filterHolder !== 'all' && item.holder !== filterHolder) return false;
                if (filterAccount !== 'all' && item.acct !== filterAccount) return false;
                if (filterDelYear !== 'all' && item.delistingYear !== filterDelYear) return false;
                if (filterBenchmark === 'beat' && !item.isBeat) return false;
                if (filterBenchmark === 'miss' && item.isBeat) return false;
                if (filterStatus !== 'all' && item.spacStatus !== filterStatus) return false;
                if (searchKeyword && !String(item.name || '').toLowerCase().includes(searchKeyword)) return false;
                return true;
            });

            filteredHoldings.sort((a, b) => {
                let valA, valB;
                if (holdingSortColumn === 'eval_amount') {
                    valA = a.estimatedEval;
                    valB = b.estimatedEval;
                } else if (holdingSortColumn === 'annualYield') {
                    valA = a.currentAnnual;
                    valB = b.currentAnnual;
                } else {
                    valA = a[holdingSortColumn];
                    valB = b[holdingSortColumn];
                }

                if (typeof valA === 'string' && typeof valB === 'string') {
                    return holdingSortDirection === 'asc' ? valA.localeCompare(valB) : valB.localeCompare(valA);
                }
                return holdingSortDirection === 'asc' ? Number(valA) - Number(valB) : Number(valB) - Number(valA);
            });

            updateHoldingSortIcons();

            if (filteredHoldings.length === 0) {
                tbody.innerHTML = `<tr><td colspan="8" class="py-12 text-center text-slate-400 font-normal">조건에 일치하는 보유 종목이 없습니다.</td></tr>`;
            }

            filteredHoldings.forEach(item => {
                totalBuy += item.buy_amount;
                totalEval += item.estimatedEval;

                totalCashFlows.push({ date: parseDateOnly(item.buy_date) || todayDate, amount: -item.buy_amount });
                totalCashFlows.push({ date: todayDate, amount: item.estimatedEval });

                const diffRate = (item.currentAnnual - item.afterTaxDepositRate).toFixed(2);
                const evalColor = item.evalDiff >= 0 ? 'text-emerald-600 font-bold' : 'text-rose-500 font-bold';
                const simpleColor = item.simpleYield >= 0 ? 'text-slate-600' : 'text-rose-500';
                const annualColor = item.isBeat ? 'text-emerald-600 font-extrabold' : 'text-rose-500 font-extrabold';
                const diffBadge = item.isBeat
                    ? `<span class="ml-1 text-[10px] bg-emerald-50 text-emerald-700 px-1.5 py-0.5 rounded font-semibold">+${diffRate}%p</span>`
                    : `<span class="ml-1 text-[10px] bg-rose-50 text-rose-600 px-1.5 py-0.5 rounded font-semibold">${diffRate}%p</span>`;

                const statusBadge = item.spacStatus === '일반'
                    ? `<span class="text-slate-700 font-normal">일반</span>`
                    : `<span class="bg-rose-50 text-rose-600 text-xs px-2 py-0.5 rounded font-bold">${item.spacStatus}</span>`;

                const holderBadge = item.holder === '아내'
                    ? `<span class="bg-amber-50 text-amber-700 text-xs px-2 py-0.5 rounded font-semibold">아내</span>`
                    : `<span class="bg-sky-50 text-sky-700 text-xs px-2 py-0.5 rounded font-semibold">남편</span>`;

                const accountBadge = item.acct === 'ISA'
                    ? `<span class="bg-indigo-50 text-indigo-700 text-xs px-2 py-0.5 rounded font-semibold">ISA</span>`
                    : `<span class="bg-slate-100 text-slate-700 text-xs px-2 py-0.5 rounded font-semibold">일반</span>`;

                const tr = document.createElement('tr');
                tr.className = "hover:bg-slate-50/80 transition-colors";
                tr.innerHTML = `
                    <td class="py-4 px-4 text-center">${holderBadge}</td>
                    <td class="py-4 px-4 text-center">${accountBadge}</td>
                    <td class="py-4 px-5 font-bold text-slate-900">${item.name}</td>
                    <td class="py-4 px-4 text-slate-700 font-semibold">${item.buy_amount.toLocaleString()}원</td>
                    <td class="py-4 px-4 ${evalColor}">${item.estimatedEval.toLocaleString()}원<br><span class="text-xs font-normal">(${item.evalDiff >= 0 ? '+' : ''}${item.evalDiff.toLocaleString()}원)</span></td>
                    <td class="py-4 px-4"><div class="${annualColor}">${item.currentAnnual.toFixed(2)}% ${diffBadge}</div><div class="${simpleColor} text-xs mt-0.5">${item.simpleYield.toFixed(2)}%</div></td>
                    <td class="py-4 px-4 text-center">${statusBadge}</td>
                    <td class="py-4 px-4 text-center space-x-1">
                        <button onclick="openSellModal(${item.origIndex}, ${item.estimatedEval})" class="text-xs bg-emerald-600 hover:bg-emerald-700 text-white font-bold px-2.5 py-1.5 rounded-lg transition-colors shadow-xs">매도/청산</button>
                        <button onclick="deleteHoldingItem(${item.origIndex})" class="text-xs bg-rose-50 hover:bg-rose-100 text-rose-600 font-bold px-2 py-1.5 rounded-lg transition-colors">삭제</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });

            const totalEvalDiff = totalEval - totalBuy;
            const totalSimpleYield = totalBuy > 0 ? (totalEvalDiff / totalBuy) * 100 : 0;
            const totalAnnualYield = calculateXIRR(totalCashFlows);

            document.getElementById('hold-total-buy').innerText = totalBuy.toLocaleString() + ' 원';
            document.getElementById('hold-total-eval').innerText = totalEval.toLocaleString() + ' 원';
            document.getElementById('hold-total-simple').innerText = totalSimpleYield.toFixed(2) + ' %';
            document.getElementById('hold-total-annual').innerText = totalAnnualYield.toFixed(2) + ' %';
        }

        function addPerformanceItem(event) {
            event.preventDefault();
            const newItem = {
                name: document.getElementById('input-name').value.trim(),
                type: document.getElementById('input-type').value,
                buy_amount: Number(document.getElementById('input-buy-amount').value),
                buy_date: document.getElementById('input-buy-date').value,
                sell_amount: Number(document.getElementById('input-sell-amount').value),
                sell_date: document.getElementById('input-sell-date').value
            };

            performanceItems.unshift(newItem);
            savePerformance();

            document.getElementById('input-name').value = '';
            document.getElementById('input-buy-amount').value = '';
            document.getElementById('input-buy-date').value = '';
            document.getElementById('input-sell-amount').value = '';
            document.getElementById('input-sell-date').value = '';

            renderPerformance();
            alert('성과 내역이 추가되었습니다.');
        }

        function deletePerformanceItem(index) {
            if (confirm('해당 성과 기록을 삭제하시겠습니까?')) {
                performanceItems.splice(index, 1);
                savePerformance();
                renderPerformance();
            }
        }

        function sortPerformanceData(column) {
            if (perfSortColumn === column) {
                perfSortDirection = perfSortDirection === 'asc' ? 'desc' : 'asc';
            } else {
                perfSortColumn = column;
                perfSortDirection = (column === 'name' || column === 'type' || column === 'buy_date' || column === 'buy_amount') ? 'asc' : 'desc';
            }
            renderPerformance();
        }

        function updatePerfSortIcons() {
            const columns = ['name', 'type', 'buy_amount', 'buy_date', 'sell_amount', 'sell_date', 'pnl', 'annualYield'];
            columns.forEach(col => {
                const iconEl = document.getElementById(`perf-sort-icon-${col}`);
                if (iconEl) {
                    if (perfSortColumn === col) {
                        iconEl.innerText = perfSortDirection === 'asc' ? ' ▲' : ' ▼';
                        iconEl.className = 'text-indigo-600 font-bold';
                    } else {
                        iconEl.innerText = ' ↕';
                        iconEl.className = 'text-slate-400';
                    }
                }
            });
        }

        function renderPerformance() {
            if (!isUnlocked) return;

            const tbody = document.getElementById('perf-table-body');
            tbody.innerHTML = '';

            const filterYear = document.getElementById('perf-filter-year').value;
            const filterType = document.getElementById('perf-filter-type').value;
            const filterBenchmark = document.getElementById('perf-filter-benchmark').value;
            const searchKeyword = document.getElementById('perf-search-input').value.trim().toLowerCase();

            let totalPnl = 0;
            let totalBuyForAvg = 0;
            let totalSellForAvg = 0;
            const perfCashFlows = [];

            const processedItems = performanceItems.map((item, idx) => {
                const pnl = item.sell_amount - item.buy_amount;
                const simpleYield = item.buy_amount > 0 ? (pnl / item.buy_amount) * 100 : 0;
                const buyDateObj = parseDateOnly(item.buy_date);
                const sellDateObj = parseDateOnly(item.sell_date);
                const annualYield = calculateSingleAnnualYield(item.buy_amount, item.sell_amount, buyDateObj, sellDateObj);
                const afterTaxDepositRate = getBokRateForDate(item.buy_date);
                const isBeat = annualYield >= afterTaxDepositRate;
                const sellYear = item.sell_date ? item.sell_date.substring(0, 4) : '';

                return { ...item, origIndex: idx, pnl, simpleYield, annualYield, afterTaxDepositRate, isBeat, sellYear, buyDateObj, sellDateObj };
            });

            const filteredItems = processedItems.filter(item => {
                if (filterYear !== 'all' && item.sellYear !== filterYear) return false;
                if (filterType !== 'all' && item.type !== filterType) return false;
                if (filterBenchmark === 'beat' && !item.isBeat) return false;
                if (filterBenchmark === 'miss' && item.isBeat) return false;
                if (searchKeyword && !String(item.name || '').toLowerCase().includes(searchKeyword)) return false;
                return true;
            });

            filteredItems.sort((a, b) => {
                let valA, valB;
                if (perfSortColumn === 'pnl') {
                    valA = a.pnl;
                    valB = b.pnl;
                } else if (perfSortColumn === 'annualYield') {
                    valA = a.annualYield;
                    valB = b.annualYield;
                } else {
                    valA = a[perfSortColumn];
                    valB = b[perfSortColumn];
                }

                if (typeof valA === 'string' && typeof valB === 'string') {
                    return perfSortDirection === 'asc' ? valA.localeCompare(valB) : valB.localeCompare(valA);
                }
                return perfSortDirection === 'asc' ? Number(valA) - Number(valB) : Number(valB) - Number(valA);
            });

            updatePerfSortIcons();

            if (filteredItems.length === 0) {
                tbody.innerHTML = `<tr><td colspan="9" class="py-12 text-center text-slate-400 font-normal">조건에 일치하는 성과 내역이 없습니다.</td></tr>`;
            }

            filteredItems.forEach(item => {
                totalPnl += item.pnl;
                totalBuyForAvg += item.buy_amount;
                totalSellForAvg += item.sell_amount;

                if (item.buyDateObj && item.sellDateObj) {
                    perfCashFlows.push({ date: item.buyDateObj, amount: -item.buy_amount });
                    perfCashFlows.push({ date: item.sellDateObj, amount: item.sell_amount });
                }

                const diffRate = (item.annualYield - item.afterTaxDepositRate).toFixed(2);
                const pnlColor = item.pnl >= 0 ? 'text-emerald-600 font-bold' : 'text-rose-500 font-bold';
                const simpleColor = item.simpleYield >= 0 ? 'text-slate-600' : 'text-rose-500';
                const annualColor = item.isBeat ? 'text-emerald-600 font-extrabold' : 'text-rose-500 font-extrabold';
                const diffBadge = item.isBeat
                    ? `<span class="ml-1 text-[10px] bg-emerald-50 text-emerald-700 px-1.5 py-0.5 rounded font-semibold">+${diffRate}%p</span>`
                    : `<span class="ml-1 text-[10px] bg-rose-50 text-rose-600 px-1.5 py-0.5 rounded font-semibold">${diffRate}%p</span>`;

                const typeBadge = item.type === '청산'
                    ? `<span class="bg-blue-50 text-blue-700 text-xs px-2 py-0.5 rounded-md font-semibold">청산</span>`
                    : `<span class="bg-purple-50 text-purple-700 text-xs px-2 py-0.5 rounded-md font-semibold">매매</span>`;

                const tr = document.createElement('tr');
                tr.className = "hover:bg-slate-50/80 transition-colors";
                tr.innerHTML = `
                    <td class="py-4 px-5 font-bold text-slate-900">${item.name}${item.account ? ` <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded ${item.account === '아내' ? 'bg-amber-50 text-amber-700' : 'bg-sky-50 text-sky-700'}">${item.account}</span>` : ''}${item.acct ? ` <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded ${item.acct === 'ISA' ? 'bg-indigo-50 text-indigo-700' : 'bg-slate-100 text-slate-600'}">${item.acct}</span>` : ''}${item.tax ? `<div class="text-[10px] font-normal text-slate-400">세금 ${item.tax.toLocaleString()}원 차감</div>` : ''}</td>
                    <td class="py-4 px-4">${typeBadge}</td>
                    <td class="py-4 px-5 text-slate-700 font-semibold">${item.buy_amount.toLocaleString()}원</td>
                    <td class="py-4 px-4 text-slate-600">${item.buy_date}</td>
                    <td class="py-4 px-5 text-slate-700 font-semibold">${item.sell_amount.toLocaleString()}원</td>
                    <td class="py-4 px-4 text-slate-600">${item.sell_date}</td>
                    <td class="py-4 px-5 ${pnlColor}">${item.pnl >= 0 ? '+' : ''}${item.pnl.toLocaleString()}원</td>
                    <td class="py-4 px-4"><div class="${annualColor}">${item.annualYield.toFixed(2)}% ${diffBadge}</div><div class="${simpleColor} text-xs mt-0.5">${item.simpleYield.toFixed(2)}%</div></td>
                    <td class="py-4 px-4 text-center">
                        <button onclick="deletePerformanceItem(${item.origIndex})" class="text-xs bg-rose-50 hover:bg-rose-100 text-rose-600 font-bold px-2.5 py-1.5 rounded-lg transition-colors">삭제</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });

            const overallSimpleYield = totalBuyForAvg > 0 ? (totalPnl / totalBuyForAvg) * 100 : 0;
            const overallAnnualYield = calculateXIRR(perfCashFlows);

            document.getElementById('perf-total-pnl').innerText = (totalPnl >= 0 ? '+' : '') + totalPnl.toLocaleString() + ' 원';
            document.getElementById('perf-avg-simple').innerText = overallSimpleYield.toFixed(2) + ' %';
            document.getElementById('perf-avg-annual').innerText = overallAnnualYield.toFixed(2) + ' %';
        }

        async function loadSpacData() {
            const cacheBuster = '?t=' + new Date().getTime();

            try {
                const rateResp = await fetch('data/deposit_rates.json' + cacheBuster);
                if (rateResp.ok) {
                    const rateData = await rateResp.json();
                    if (rateData && rateData.rates) bokDepositRates = { ...bokDepositRates, ...rateData.rates };
                }
            } catch (e) {}

            try {
                const response = await fetch('data/spacs.json' + cacheBuster);
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                document.getElementById('update-time').innerText = `최종 갱신: ${data.updated_at || '-'}`;
                const rawItems = Array.isArray(data) ? data : (data.items || []);

                const seenKeys = new Set();
                const activeItems = rawItems.filter(item => {
                    if (isExpiredOrDelistedSpac(item)) return false;
                    const key = String(item.code || item.corp_code || normName(item.name)).trim();
                    if (!key || seenKeys.has(key)) return false;
                    seenKeys.add(key);
                    return true;
                });

                allItems = activeItems.map(item => {
                    const resolvedDelistingDate = getDelistingDateValue(item);
                    const normalizedItem = {
                        ...item,
                        delisting_date: resolvedDelistingDate,
                        is_fair_buy: normalizeFairBuy(item),
                        status: normalizeStatus(item.status)
                    };
                    return {
                        ...normalizedItem,
                        invest_type: normalizeInvestType(normalizedItem)
                    };
                });

                const datalist = document.getElementById('spac-name-list');
                if (datalist) {
                    datalist.innerHTML = allItems.map(i => `<option value="${i.name}">`).join('');
                }

                const baseAfterTaxRate = getCurrentBaseDepositRate();
                if (baseAfterTaxRate !== null && baseAfterTaxRate !== undefined) {
                    document.getElementById('deposit-rate-badge').innerText = `기준 예금금리(세후): 연 ${Number(baseAfterTaxRate).toFixed(2)}%`;
                }

                updateSummaryStats(allItems);
                applyFilters();
            } catch (error) {
                console.error('SPAC 데이터 로드 실패:', error);
                document.getElementById('update-time').innerText = '데이터 로드 실패';
            }

            let savedHoldings = null;
            let savedPerf = null;
            try {
                savedHoldings = localStorage.getItem('spac_holding_items_v5');
                savedPerf = localStorage.getItem('spac_performance_items_v3');
            } catch (e) {}

            if (savedHoldings) {
                try {
                    holdingItems = JSON.parse(savedHoldings);
                } catch (e) {
                    holdingItems = [];
                }
            } else {
                holdingItems = [];
            }

            if (savedPerf) {
                try {
                    performanceItems = JSON.parse(savedPerf);
                } catch (e) {
                    performanceItems = [];
                }
            } else {
                performanceItems = [
                    { name: "유진스팩7호", type: "청산", buy_amount: 2001995, buy_date: "2024-05-22", sell_amount: 2060215, sell_date: "2024-10-15" },
                    { name: "하이제7호스팩", type: "청산", buy_amount: 2002980, buy_date: "2024-05-07", sell_amount: 2053809, sell_date: "2024-10-16" },
                    { name: "하나스팩21호", type: "매매", buy_amount: 2001995, buy_date: "2024-10-16", sell_amount: 1712612, sell_date: "2024-11-12" },
                    { name: "신영스팩9호", type: "매매", buy_amount: 259690, buy_date: "2025-01-03", sell_amount: 265138, sell_date: "2025-03-10" },
                    { name: "케이비제21호스팩", type: "청산", buy_amount: 2003910, buy_date: "2025-01-03", sell_amount: 2055251, sell_date: "2025-04-25" },
                    { name: "엔에이치스팩23호", type: "청산", buy_amount: 2002540, buy_date: "2024-05-08", sell_amount: 2033815, sell_date: "2025-05-20" },
                    { name: "삼성스팩6호", type: "청산", buy_amount: 2002410, buy_date: "2025-01-03", sell_amount: 2056815, sell_date: "2025-05-20" },
                    { name: "신영스팩8호", type: "매매", buy_amount: 2003530, buy_date: "2025-01-03", sell_amount: 2158661, sell_date: "2025-05-22" },
                    { name: "유안타제9호스팩", type: "청산", buy_amount: 2003295, buy_date: "2025-01-03", sell_amount: 2076519, sell_date: "2025-07-03" },
                    { name: "유안타제10호스팩", type: "청산", buy_amount: 2002660, buy_date: "2024-10-16", sell_amount: 2096807, sell_date: "2025-09-02" },
                    { name: "에스케이증권제8호스팩", type: "청산", buy_amount: 2003295, buy_date: "2025-01-03", sell_amount: 2080123, sell_date: "2025-09-02" },
                    { name: "엔에이치스팩25호", type: "청산", buy_amount: 2002410, buy_date: "2025-01-03", sell_amount: 2089260, sell_date: "2025-09-03" },
                    { name: "엔에이치스팩24호", type: "청산", buy_amount: 2001810, buy_date: "2025-01-03", sell_amount: 2087501, sell_date: "2025-09-05" },
                    { name: "IBKS제20호스팩", type: "청산", buy_amount: 2002140, buy_date: "2025-05-23", sell_amount: 2036184, sell_date: "2025-09-12" },
                    { name: "유진스팩9호", type: "청산", buy_amount: 2003405, buy_date: "2025-01-03", sell_amount: 2066011, sell_date: "2025-10-29" },
                    { name: "대신밸런스제14호스팩", type: "청산", buy_amount: 2002540, buy_date: "2025-05-23", sell_amount: 2044697, sell_date: "2025-10-30" },
                    { name: "엔에이치스팩26호", type: "청산", buy_amount: 2002660, buy_date: "2024-05-07", sell_amount: 2119749, sell_date: "2025-11-06" },
                    { name: "신한제11호스팩", type: "매매", buy_amount: 1955790, buy_date: "2025-07-04", sell_amount: 1993520, sell_date: "2025-11-07" },
                    { name: "엔에이치스팩27호", type: "청산", buy_amount: 2004970, buy_date: "2024-10-16", sell_amount: 2111317, sell_date: "2025-11-13" },
                    { name: "엔에이치스팩27호", type: "청산", buy_amount: 62360, buy_date: "2024-10-24", sell_amount: 65910, sell_date: "2025-11-13" },
                    { name: "엔에이치스팩27호", type: "청산", buy_amount: 240225, buy_date: "2024-11-12", sell_amount: 252655, sell_date: "2025-11-13" },
                    { name: "비엔케이제1호스팩", type: "청산", buy_amount: 1472040, buy_date: "2024-11-12", sell_amount: 1540389, sell_date: "2025-11-14" },
                    { name: "비엔케이제1호스팩", type: "청산", buy_amount: 2244990, buy_date: "2025-05-23", sell_amount: 2305106, sell_date: "2025-11-14" },
                    { name: "신영스팩9호", type: "청산", buy_amount: 1715655, buy_date: "2025-01-03", sell_amount: 1762024, sell_date: "2025-11-20" },
                    { name: "유안타제13호스팩", type: "청산", buy_amount: 218155, buy_date: "2024-04-23", sell_amount: 231291, sell_date: "2026-02-03" },
                    { name: "유안타제13호스팩", type: "청산", buy_amount: 1781950, buy_date: "2024-04-25", sell_amount: 1889241, sell_date: "2026-02-03" },
                    { name: "하나스팩27호", type: "청산", buy_amount: 1985280, buy_date: "2025-10-22", sell_amount: 2030708, sell_date: "2026-02-20" },
                    { name: "엔에이치스팩29호", type: "매매", buy_amount: 2003730, buy_date: "2024-05-10", sell_amount: 2517466, sell_date: "2026-04-17" },
                    { name: "엔에이치스팩30호", type: "매매", buy_amount: 2003730, buy_date: "2024-04-23", sell_amount: 3965536, sell_date: "2026-03-24" },
                    { name: "엔에이치스팩30호", type: "매매", buy_amount: 270095, buy_date: "2025-04-03", sell_amount: 538038, sell_date: "2026-03-24" },
                    { name: "하나29호스팩", type: "청산", buy_amount: 3001505, buy_date: "2026-01-07", sell_amount: 3081002, sell_date: "2026-05-15" },
                    { name: "하나29호스팩", type: "청산", buy_amount: 1889360, buy_date: "2026-01-06", sell_amount: 1933431, sell_date: "2026-05-15" },
                    { name: "하나29호스팩", type: "청산", buy_amount: 5007430, buy_date: "2026-01-05", sell_amount: 5124239, sell_date: "2026-05-15" },
                    { name: "교보14호스팩", type: "청산", buy_amount: 3002360, buy_date: "2026-01-07", sell_amount: 3084152, sell_date: "2026-05-18" },
                    { name: "교보14호스팩", type: "청산", buy_amount: 4007050, buy_date: "2026-01-06", sell_amount: 4103564, sell_date: "2026-05-18" },
                    { name: "교보14호스팩", type: "청산", buy_amount: 1796780, buy_date: "2026-01-05", sell_amount: 1844444, sell_date: "2026-05-18" },
                    { name: "케이비제25호스팩", type: "청산", buy_amount: 4170605, buy_date: "2026-01-05", sell_amount: 4282230, sell_date: "2026-05-29" },
                    { name: "케이비제25호스팩", type: "청산", buy_amount: 3001505, buy_date: "2026-01-07", sell_amount: 3083981, sell_date: "2026-05-29" },
                    { name: "에스케이증권제9호스팩", type: "청산", buy_amount: 1433745, buy_date: "2025-09-05", sell_amount: 1492326, sell_date: "2026-06-24" },
                    { name: "에스케이증권제9호스팩", type: "청산", buy_amount: 2000430, buy_date: "2025-10-27", sell_amount: 2058677, sell_date: "2026-06-24" }
                ];
            }

            applySeedHoldings();
            await processLiquidations();
        }

        let currentSortColumn = 'current_price';
        let currentSortDirection = 'asc';

        function updateSummaryStats(items) {
            document.getElementById('stat-total-count').innerText = items.length + ' 개';
            const mergerFairCount = items.filter(i => {
                if (i.invest_type !== '합병' || i.is_fair_buy !== 'O') return false;
                const yIsa = resolveAccountYield(i, 'isa');
                const yGen = resolveAccountYield(i, 'general');
                return (yIsa !== null && yIsa > 0) || (yGen !== null && yGen > 0);
            }).length;
            const liqFairCount = items.filter(i => i.invest_type === '청산' && i.is_fair_buy === 'O').length;
            document.getElementById('stat-merger-count').innerText = mergerFairCount + ' 개';
            document.getElementById('stat-liq-count').innerText = liqFairCount + ' 개';
            updateRecommendations(items);
        }

        function pickBucketItems(pool, accountType, baseAfterTaxRate, bucket) {
            return pool
                .map(item => ({ item, y: resolveAccountYield(item, accountType) }))
                .filter(entry => {
                    if (entry.y === null || !Number.isFinite(entry.y)) return false;
                    if (bucket === 'high') return entry.y >= baseAfterTaxRate;
                    if (bucket === 'mid') return entry.y >= 0 && entry.y < baseAfterTaxRate;
                    return false;
                })
                .sort((a, b) => b.y - a.y)
                .map(entry => entry.item);
        }

        function updateRecommendations(items) {
            const baseAfterTaxRate = getCurrentBaseDepositRate();

            const fairNormalItems = items.filter(i => i.is_fair_buy === 'O' && i.status === '일반');
            const fallbackNormalItems = fairNormalItems.length > 0 ? fairNormalItems : items.filter(i => i.status === '일반');

            const mergerPool = fallbackNormalItems.filter(i => i.invest_type === '합병');
            const liqPool = fallbackNormalItems.filter(i => i.invest_type === '청산');

            const mergerIsaHigh = pickBucketItems(mergerPool, 'isa', baseAfterTaxRate, 'high');
            const mergerIsaMid = pickBucketItems(mergerPool, 'isa', baseAfterTaxRate, 'mid');
            const mergerGenHigh = pickBucketItems(mergerPool, 'general', baseAfterTaxRate, 'high');
            const mergerGenMid = pickBucketItems(mergerPool, 'general', baseAfterTaxRate, 'mid');

            const liqIsaHigh = pickBucketItems(liqPool, 'isa', baseAfterTaxRate, 'high');
            const liqIsaMid = pickBucketItems(liqPool, 'isa', baseAfterTaxRate, 'mid');
            const liqGenHigh = pickBucketItems(liqPool, 'general', baseAfterTaxRate, 'high');
            const liqGenMid = pickBucketItems(liqPool, 'general', baseAfterTaxRate, 'mid');

            fillCard('merger-high-isa', mergerIsaHigh[0], 'isa', baseAfterTaxRate);
            fillCard('merger-high-gen', mergerGenHigh[0], 'general', baseAfterTaxRate);
            fillCard('merger-mid-isa', mergerIsaMid[0], 'isa', baseAfterTaxRate);
            fillCard('merger-mid-gen', mergerGenMid[0], 'general', baseAfterTaxRate);

            fillCard('liq-high-isa', liqIsaHigh[0], 'isa', baseAfterTaxRate);
            fillCard('liq-high-gen', liqGenHigh[0], 'general', baseAfterTaxRate);
            fillCard('liq-mid-isa', liqIsaMid[0], 'isa', baseAfterTaxRate);
            fillCard('liq-mid-gen', liqGenMid[0], 'general', baseAfterTaxRate);
        }

        function fillCard(cardId, item, accountType, baseAfterTaxRate) {
            const nameEl = document.getElementById(`rec-${cardId}-name`);
            const priceEl = document.getElementById(`rec-${cardId}-price`);
            const yieldEl = document.getElementById(`rec-${cardId}-yield`);
            const liqEl = document.getElementById(`rec-${cardId}-liq`);

            if (!item) {
                if (nameEl) nameEl.innerText = '-';
                if (priceEl) priceEl.innerText = '-';
                if (yieldEl) yieldEl.innerHTML = '-';
                if (liqEl) liqEl.innerText = '-';
                return;
            }

            const yVal = resolveAccountYield(item, accountType);
            const curPrice = toFiniteNumber(item.current_price);
            const liqPrice = toFiniteNumber(item.liquidation_price);
            const ppText = formatSpreadPp(yVal, baseAfterTaxRate);

            if (nameEl) nameEl.innerText = item.name || '-';
            if (priceEl) priceEl.innerText = curPrice !== null ? curPrice.toLocaleString() + '원' : '-';
            if (yieldEl) {
                if (yVal !== null) {
                    const yColor = yVal >= baseAfterTaxRate ? 'text-emerald-400' : (yVal >= 0 ? 'text-amber-300' : 'text-rose-400');
                    const ppColor = ppText && ppText.startsWith('(+') ? 'text-emerald-300' : 'text-rose-300';
                    yieldEl.innerHTML = `<div class="${yColor}">${yVal.toFixed(2)}%</div>${ppText ? `<div class="text-[10px] font-semibold ${ppColor}">${ppText}</div>` : ''}`;
                } else {
                    yieldEl.innerHTML = '-';
                }
            }
            if (liqEl) liqEl.innerText = liqPrice !== null ? liqPrice.toLocaleString() + '원' : '미확인';
        }

        function sortData(column) {
            if (currentSortColumn === column) {
                currentSortDirection = currentSortDirection === 'asc' ? 'desc' : 'asc';
            } else {
                currentSortColumn = column;
                currentSortDirection = (column === 'name' || column === 'invest_type' || column === 'delisting_date' || column === 'status') ? 'asc' : 'desc';
            }
            applyFilters();
        }

        function updateSortIcons() {
            const columns = ['name', 'current_price', 'liquidation_price', 'is_fair_buy', 'invest_type', 'yield', 'delisting_date', 'status'];
            columns.forEach(col => {
                const iconEl = document.getElementById(`sort-icon-${col}`);
                if (iconEl) {
                    if (currentSortColumn === col) {
                        iconEl.innerText = currentSortDirection === 'asc' ? ' ▲' : ' ▼';
                        iconEl.className = 'text-indigo-600 font-bold';
                    } else {
                        iconEl.innerText = ' ↕';
                        iconEl.className = 'text-slate-400';
                    }
                }
            });
        }

        function applyFilters() {
            const typeVal = document.getElementById('filter-type').value;
            const fairVal = document.getElementById('filter-fair').value;
            const statusVal = document.getElementById('filter-status').value;
            const accountVal = document.getElementById('filter-account').value;
            const delYearVal = document.getElementById('filter-delisting-year').value;
            const yieldRangeVal = document.getElementById('filter-yield-range').value;
            const searchVal = document.getElementById('search-input').value.trim().toLowerCase();

            const yieldHeader = document.getElementById('yield-header-col');
            if (yieldHeader) {
                yieldHeader.innerText = '예상수익률';
            }

            const baseAfterTaxRate = getCurrentBaseDepositRate();

            updateRecommendations(allItems);

            const filtered = allItems.filter(item => {
                if (typeVal !== 'all' && item.invest_type !== typeVal) return false;
                if (fairVal !== 'all' && item.is_fair_buy !== fairVal) return false;
                if (statusVal !== 'all' && item.status !== statusVal) return false;
                if (delYearVal !== 'all') {
                    const dStr = String(item.delisting_date || '').trim();
                    if (!dStr || !dStr.startsWith(delYearVal)) return false;
                }

                const resolvedYield = resolveAccountYield(item, accountVal);
                if (yieldRangeVal !== 'all' && resolvedYield === null) return false;
                const yVal = resolvedYield !== null ? Number(resolvedYield) : null;
                if (yieldRangeVal === 'above_deposit' && (yVal === null || yVal < baseAfterTaxRate)) return false;
                if (yieldRangeVal === 'low_positive' && (yVal === null || yVal < 0 || yVal >= baseAfterTaxRate)) return false;
                if (yieldRangeVal === 'negative' && (yVal === null || yVal >= 0)) return false;

                if (searchVal && !String(item.name || '').toLowerCase().includes(searchVal)) return false;
                return true;
            });

            filtered.sort((a, b) => {
                let valA, valB;
                if (currentSortColumn === 'yield') {
                    valA = resolveAccountYield(a, accountVal) ?? -Infinity;
                    valB = resolveAccountYield(b, accountVal) ?? -Infinity;
                } else if (currentSortColumn === 'current_price' || currentSortColumn === 'liquidation_price') {
                    valA = toFiniteNumber(a[currentSortColumn]) ?? -Infinity;
                    valB = toFiniteNumber(b[currentSortColumn]) ?? -Infinity;
                } else {
                    valA = a[currentSortColumn] ?? (typeof b[currentSortColumn] === 'string' ? '' : -Infinity);
                    valB = b[currentSortColumn] ?? (typeof a[currentSortColumn] === 'string' ? '' : -Infinity);
                }

                if (typeof valA === 'string' && typeof valB === 'string') {
                    return currentSortDirection === 'asc' ? valA.localeCompare(valB) : valB.localeCompare(valA);
                }
                return currentSortDirection === 'asc' ? Number(valA) - Number(valB) : Number(valB) - Number(valA);
            });

            updateSortIcons();
            renderTable(filtered, accountVal, baseAfterTaxRate);
        }

        function renderTable(items, accountVal, baseAfterTaxRate) {
            const tbody = document.getElementById('spac-table-body');
            tbody.innerHTML = '';

            if (items.length === 0) {
                tbody.innerHTML = `<tr><td colspan="8" class="py-12 text-center text-slate-400 font-normal">조건에 일치하는 스팩 종목이 없습니다.</td></tr>`;
                return;
            }

            items.forEach(item => {
                const resolvedYield = resolveAccountYield(item, accountVal);
                const hasYield = resolvedYield !== null && Number.isFinite(resolvedYield);
                const yVal = hasYield ? Number(resolvedYield) : null;
                const ppText = hasYield ? formatSpreadPp(yVal, baseAfterTaxRate) : null;

                const fairBadge = item.is_fair_buy === 'O'
                    ? `<span class="bg-emerald-50 text-emerald-700 font-bold px-2 py-0.5 rounded text-xs">O</span>`
                    : item.is_fair_buy === 'X'
                        ? `<span class="bg-slate-100 text-slate-500 px-2 py-0.5 rounded text-xs">X</span>`
                        : `<span class="bg-amber-50 text-amber-700 px-2 py-0.5 rounded text-xs">미확인</span>`;

                const typeBadge = item.invest_type === '청산'
                    ? `<span class="bg-blue-50 text-blue-700 text-xs px-2 py-0.5 rounded font-semibold">청산</span>`
                    : `<span class="bg-indigo-50 text-indigo-700 text-xs px-2 py-0.5 rounded font-semibold">합병</span>`;

                const statusBadge = item.status === '일반'
                    ? `<span class="text-slate-700 font-normal">일반</span>`
                    : `<span class="bg-rose-50 text-rose-600 text-xs px-2 py-0.5 rounded font-bold">${item.status}</span>`;

                const yieldColor = !hasYield ? 'text-slate-400' : (yVal >= 0 ? 'text-emerald-600 font-bold' : 'text-rose-500 font-bold');
                const ppColor = !ppText ? 'text-slate-400' : (ppText.startsWith('(+') ? 'text-emerald-700 font-semibold' : 'text-rose-600 font-semibold');

                const curPrice = toFiniteNumber(item.current_price);
                const liqPrice = toFiniteNumber(item.liquidation_price);
                const liqText = liqPrice !== null ? liqPrice.toLocaleString() + '원' : '미확인';

                const tr = document.createElement('tr');
                tr.className = "hover:bg-slate-50/80 transition-colors";
                tr.innerHTML = `
                    <td class="py-4 px-6 font-bold text-slate-900">${item.name || '-'}</td>
                    <td class="py-4 px-6 text-slate-700 font-semibold">${curPrice !== null ? curPrice.toLocaleString() + '원' : '-'}</td>
                    <td class="py-4 px-6 text-slate-600">${liqText}</td>
                    <td class="py-4 px-6 text-center">${fairBadge}</td>
                    <td class="py-4 px-6">${typeBadge}</td>
                    <td class="py-4 px-6">
                        <div class="${yieldColor}">${hasYield ? yVal.toFixed(2) + '%' : '-'}</div>
                        ${ppText ? `<div class="text-[11px] mt-0.5 ${ppColor}">${ppText}</div>` : `<div class="text-[11px] mt-0.5 text-slate-400">-</div>`}
                    </td>
                    <td class="py-4 px-6 text-slate-500 text-xs">${item.delisting_date || '-'}</td>
                    <td class="py-4 px-6">${statusBadge}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        window.onload = loadSpacData;
    </script>
</body>
</html>
```[cite: 38]
