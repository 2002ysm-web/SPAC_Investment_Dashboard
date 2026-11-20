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

import pandas as pd
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
    "Referer": "https://kind.krx.co.kr/",
}
GENERAL_TAX_RATE = 0.154
FINAL_YEAR_TAX_FACTOR = 0.845
FINAL_YEAR_MONTH_RATIO = 6.0 / 12.0


def parse_html_tables(html_text: str) -> list[pd.DataFrame]:
    for flavor in ("lxml", "html5lib", "bs4"):
        try:
            dfs = pd.read_html(io.StringIO(html_text), flavor=flavor)
            if dfs:
                return dfs
        except Exception:
            continue
    try:
        return pd.read_html(io.StringIO(html_text))
    except Exception:
        return []


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
        res = requests.get(url, headers=HEADERS, timeout=25)
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
    return digits.zfill(6) if digits else ""


def extract_dept_value(row: dict[str, Any]) -> str:
    dept_keys = (
        "소속부",
        "종목구분",
        "증권구분",
        "시장구분",
        "dept_nm",
        "sect_tp_nm",
        "secugrp_nm",
        "mkt_nm",
        "krx_dept",
    )
    parts: list[str] = []
    for k in dept_keys:
        v = row.get(k)
        if v is not None and str(v).strip() and str(v).strip().lower() != "nan":
            parts.append(str(v).strip())
    return " / ".join(parts)


def extract_name_value(row: dict[str, Any]) -> str:
    name_keys = ("회사명", "종목명", "itm_nm", "kor_secn_nm", "name")
    for k in name_keys:
        v = row.get(k)
        if v is not None and str(v).strip() and str(v).strip().lower() != "nan":
            return str(v).strip()
    return ""


def is_krx_spac_row(row: dict[str, Any]) -> bool:
    """
    KRX 종목 정보에서 소속부(또는 종목구분·증권구분) 기준으로 SPAC 여부를 우선 판별하고,
    소속부 정보가 없는 경우 종목명('스팩', 'SPAC')으로 보조 판별합니다.
    """
    dept_text = extract_dept_value(row)
    name_text = extract_name_value(row)

    dept_upper = dept_text.upper()
    if "SPAC" in dept_upper or "기업인수목적" in dept_text or "스팩" in dept_text:
        return True

    if re.search(r"(스팩|SPAC)", name_text, re.IGNORECASE):
        return True

    return False


def get_krx_spac_list() -> list[dict]:
    """
    KRX 상장법인/종목 정보에서 소속부(또는 구분/종목명) 기준으로 SPAC 종목만 필터링합니다.
    """
    spacs: list[dict] = []
    seen_codes: set[str] = set()
    url = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"

    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        raw_html = res.content.decode("euc-kr", errors="ignore")
        if "<table" not in raw_html.lower():
            res2 = requests.post(
                "https://kind.krx.co.kr/corpgeneral/corpList.do",
                data={"method": "download", "searchType": "13"},
                headers=HEADERS,
                timeout=20,
            )
            res2.raise_for_status()
            raw_html = res2.content.decode("euc-kr", errors="ignore")

        dfs = parse_html_tables(raw_html)
        if not dfs:
            return spacs

        df = dfs[0].copy()
        df.columns = [str(c).strip() for c in df.columns]

        code_col = next(
            (c for c in ("종목코드", "단축코드", "isu_srt_cd", "code") if c in df.columns),
            None,
        )
        name_col = next(
            (c for c in ("회사명", "종목명", "itm_nm", "kor_secn_nm", "name") if c in df.columns),
            None,
        )
        if not code_col or not name_col:
            return spacs

        for raw_row in df.to_dict(orient="records"):
            if not is_krx_spac_row(raw_row):
                continue

            code = normalize_code(raw_row.get(code_col))
            name = str(raw_row.get(name_col) or "").strip()
            if not code or len(code) != 6 or not name or code in seen_codes:
                continue

            seen_codes.add(code)
            listing_date = str(
                raw_row.get("상장일") or raw_row.get("list_dd") or raw_row.get("listing_date") or ""
            ).strip()
            krx_dept = extract_dept_value(raw_row)

            spacs.append({
                "code": code,
                "name": name,
                "listing_date": listing_date,
                "krx_dept": krx_dept,
            })

        print(f"[INFO] KRX 소속부/종목 기준 SPAC {len(spacs)}개 필터링 완료")
    except Exception as exc:
        print(f"[WARN] KRX SPAC 목록 조회 실패: {exc}")

    return spacs


def get_naver_quote_and_base(code: str) -> tuple[int, bool, int | None]:
    current_price = 0
    is_suspended = False
    par_base_price: int | None = None

    try:
        url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
        res = requests.get(url, headers=HEADERS, timeout=7)
        res.raise_for_status()
        payload = res.json()
        areas = payload.get("result", {}).get("areas", [])
        if areas and areas[0].get("datas"):
            item = areas[0]["datas"][0]
            nv = item.get("nv", 0)
            current_price = int(nv) if nv is not None else 0
            stop_flag = str(item.get("stopReason", "") or item.get("isStop", "") or "").lower()
            if stop_flag in {"true", "1", "y", "yes"} or current_price == 0:
                is_suspended = True
    except Exception:
        pass

    try:
        page_url = f"https://finance.naver.com/item/main.naver?code={code}"
        res = requests.get(page_url, headers=HEADERS, timeout=8)
        res.raise_for_status()
        html = res.text
        if current_price == 0:
            m = re.search(r'id="_nowVal"[^>]*>([0-9,]+)<', html) or re.search(
                r'class="no_today"[^>]*>.*?<span class="blind">([0-9,]+)</span>', html, re.S
            )
            if m:
                current_price = int(m.group(1).replace(",", ""))
        if "거래정지" in html or current_price == 0:
            is_suspended = True

        m_par = re.search(r"액면가.*?([0-9,]+)\s*원", html, re.S)
        if m_par:
            raw_par = int(m_par.group(1).replace(",", ""))
            if raw_par == 100:
                par_base_price = 2000
            elif raw_par == 500:
                par_base_price = 10000
            elif raw_par in (2000, 10000):
                par_base_price = raw_par
    except Exception as exc:
        print(f"[WARN] [{code}] 시세/액면가 조회 실패: {exc}")

    return current_price, is_suspended, par_base_price


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
            headers=HEADERS,
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
            headers=HEADERS,
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
    deposit_rate: float,
    ref_dt: datetime,
    existing_item: dict | None = None,
) -> tuple[dict, dict | None]:
    code = spac["code"]
    corp_code = corp_codes.get(code, "")
    current_price, is_suspended, par_base_price = get_naver_quote_and_base(code)
    if current_price == 0 and existing_item and (existing_item.get("current_price") or 0) > 0:
        current_price = int(existing_item["current_price"])

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
    existing_by_code = {str(x.get("code", "")).zfill(6): x for x in existing_spacs if x.get("code")}

    now_kst = datetime.now(KST)
    ref_dt = datetime(now_kst.year, now_kst.month, now_kst.day)
    updated_at = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

    try:
        deposit_rate = get_base_deposit_rate()
        corp_codes = get_dart_corp_codes()
        spac_list = get_krx_spac_list()

        if not spac_list:
            print("[WARN] KRX SPAC 필터링 결과 비어 있음 — 기존 데이터 유지")
            save_json(existing_spacs, OUTPUT_PATH, updated_at)
            save_json(existing_liqs, LIQUIDATIONS_PATH, updated_at)
            return

        spac_results: list[dict] = []
        liq_results: list[dict] = []

        for idx, spac in enumerate(spac_list, start=1):
            code = spac["code"]
            prev_item = existing_by_code.get(code)
            try:
                rec, liq = build_spac_record(spac, corp_codes, deposit_rate, ref_dt, prev_item)
                spac_results.append(rec)
                if liq is not None:
                    liq_results.append(liq)
            except Exception as exc:
                print(f"[WARN] [{code}] 처리 오류: {exc}")
                if prev_item:
                    spac_results.append(prev_item)
            if idx % 20 == 0:
                print(f"[PROGRESS] {idx}/{len(spac_list)} 완료")
            time.sleep(0.03)

        valid_prices = sum(1 for r in spac_results if (r.get("current_price") or 0) > 0)
        if valid_prices == 0 and existing_spacs:
            print("[WARN] 현재가 유효 건수 0 — 기존 spacs.json 데이터 유지")
            spac_results = existing_spacs

        if not liq_results and existing_liqs:
            liq_results = existing_liqs

        resolved_liq_cnt = sum(1 for r in spac_results if r.get("liquidation_price") is not None)
        fair_o_cnt = sum(1 for r in spac_results if r.get("is_fair_buy") == "O")
        print(
            f"[SUMMARY] 전체={len(spac_results)}개, 현재가유효={valid_prices}개, "
            f"청산가확보={resolved_liq_cnt}개, 매수적정(O)={fair_o_cnt}개, 청산내역={len(liq_results)}건"
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
