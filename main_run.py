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
    fallback = 2.4
    if DEPOSIT_RATE_PATH.exists():
        try:
            with DEPOSIT_RATE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            rates = data.get("rates", {}) if isinstance(data, dict) else {}
            if rates:
                latest_ym = sorted(rates.keys())[-1]
                return float(rates[latest_ym])
        except Exception as exc:
            print(f"[WARN] 기준 예금금리 파일 읽기 실패: {exc}")
    return fallback


def get_dart_corp_codes() -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not DART_API_KEY:
        print("[INFO] DART_API_KEY 미설정 — 공시 조회 없이 진행합니다.")
        return mapping

    url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={DART_API_KEY}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
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
        print(f"[INFO] DART 고유번호 {len(mapping)}개 로드 완료")
    except Exception as exc:
        print(f"[WARN] DART corpCode 조회 실패: {exc}")
    return mapping


def get_krx_ipo_prices() -> dict[str, int]:
    """KRX KIND 신규상장현황에서 스팩 종목별 공모가를 조회합니다."""
    ipo_prices: dict[str, int] = {}
    now_kst = datetime.now(KST)
    start_date = (now_kst - timedelta(days=1200)).strftime("%Y-%m-%d")
    end_date = now_kst.strftime("%Y-%m-%d")

    endpoints = [
        (
            "https://kind.krx.co.kr/listinvstg/pubprcinf.do",
            {
                "method": "searchPubprcInf",
                "forward": "pubprcinf_list",
                "searchType": "13",
                "startDate": start_date,
                "endDate": end_date,
            },
        ),
        (
            "https://kind.krx.co.kr/listinvstg/listingstatus.do",
            {
                "method": "searchListingStatus",
                "forward": "listingstatus_list",
                "searchType": "13",
                "startDate": start_date,
                "endDate": end_date,
            },
        ),
    ]

    for url, params in endpoints:
        try:
            res = requests.post(url, data=params, headers=HEADERS, timeout=20)
            res.raise_for_status()
            html = res.content.decode("euc-kr", errors="ignore")
            if "<table" not in html.lower():
                continue
            dfs = parse_html_tables(html)
            for df in dfs:
                if df.empty:
                    continue
                cols = [str(c).strip() for c in df.columns]
                name_col = next((c for c in cols if "종목명" in c or "회사명" in c), None)
                price_col = next((c for c in cols if "공모가" in c or "발행가" in c or "확정공모가" in c), None)
                code_col = next((c for c in cols if "종목코드" in c or "코드" in c), None)
                if not name_col or not price_col:
                    continue

                for _, row in df.iterrows():
                    name = str(row.get(name_col, "")).strip()
                    if "스팩" not in name:
                        continue
                    raw_price = re.sub(r"[^0-9]", "", str(row.get(price_col, "")))
                    if not raw_price.isdigit():
                        continue
                    price = int(raw_price)
                    if not (1000 <= price <= 50000):
                        continue

                    code = ""
                    if code_col:
                        m_code = re.search(r"([0-9A-Za-z]{6})", str(row.get(code_col, "")).strip())
                        if m_code:
                            code = m_code.group(1).zfill(6)
                    if code:
                        ipo_prices[code] = price
                    norm_key = "name:" + re.sub(r"\s+", "", name).lower()
                    ipo_prices[norm_key] = price

            if ipo_prices:
                print(f"[INFO] KRX 신규상장 공모가 {len(ipo_prices)}건 조회 완료")
                break
        except Exception as exc:
            print(f"[WARN] KRX 신규상장 공모가 조회 시도 실패 ({url}): {exc}")

    return ipo_prices


def get_krx_spac_list() -> list[dict]:
    spacs: list[dict] = []
    url = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
    try:
        res = requests.get(url, headers=HEADERS, timeout=25)
        res.raise_for_status()
        raw_html = res.content.decode("euc-kr", errors="ignore")
        if "<table" not in raw_html.lower():
            res2 = requests.post(
                "https://kind.krx.co.kr/corpgeneral/corpList.do",
                data={"method": "download", "searchType": "13"},
                headers=HEADERS,
                timeout=25,
            )
            res2.raise_for_status()
            raw_html = res2.content.decode("euc-kr", errors="ignore")

        dfs = parse_html_tables(raw_html)
        if not dfs:
            print("[WARN] KRX HTML 테이블 없음")
            return spacs

        df = dfs[0]
        if "회사명" not in df.columns or "종목코드" not in df.columns:
            print(f"[WARN] 예상치 못한 KRX 컬럼: {list(df.columns)}")
            return spacs

        spac_df = df[df["회사명"].astype(str).str.contains("스팩", na=False)].copy()
        for _, row in spac_df.iterrows():
            code = str(row["종목코드"]).strip().replace(".0", "").zfill(6)
            name = str(row["회사명"]).strip()
            listing_date = str(row.get("상장일", "")).strip()
            spacs.append({"code": code, "name": name, "listing_date": listing_date})

        print(f"[INFO] KRX 스팩 종목 {len(spacs)}개 조회 완료")
    except Exception as exc:
        print(f"[WARN] KRX 스팩 목록 조회 실패: {exc}")
    return spacs


def get_naver_quote(code: str) -> tuple[int, bool]:
    current_price = 0
    is_suspended = False

    try:
        url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
        res = requests.get(url, headers=HEADERS, timeout=8)
        res.raise_for_status()
        payload = res.json()
        areas = payload.get("result", {}).get("areas", [])
        if areas and areas[0].get("datas"):
            item = areas[0]["datas"][0]
            raw_price = item.get("nv", 0)
            current_price = int(raw_price) if raw_price is not None else 0
            stop_flag = str(item.get("stopReason", "") or item.get("isStop", "") or "").lower()
            if stop_flag in {"true", "1", "y", "yes"} or current_price == 0:
                is_suspended = True
            if current_price > 0:
                return current_price, is_suspended
    except Exception:
        pass

    try:
        page_url = f"https://finance.naver.com/item/main.naver?code={code}"
        res = requests.get(page_url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        html = res.text
        m = re.search(r'id="_nowVal"[^>]*>([0-9,]+)<', html) or re.search(
            r'class="no_today"[^>]*>.*?<span class="blind">([0-9,]+)</span>', html, re.S
        )
        if m:
            current_price = int(m.group(1).replace(",", ""))
        if "거래정지" in html or current_price == 0:
            is_suspended = True
    except Exception as exc:
        print(f"[WARN] [{code}] 시세 조회 실패: {exc}")

    return current_price, is_suspended


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
        print(f"[WARN] [{corp_code}] DART 공시 조회 오류: {exc}")
    return []


def clean_html_text(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&nbsp;|&#160;", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def extract_unit_price_from_text(text: str) -> int | None:
    clean_text = clean_html_text(text)
    if not clean_text:
        return None

    priority_patterns = [
        r"(?:청산가|청산\s*분배금|해산\s*분배금|1주당\s*청산\w*|주당\s*분배금|예치금\s*반환\w*)[^0-9]{0,25}([1-9][0-9,]{2,4})\s*원",
        r"(?:공모가|발행가|기준가|예치\s*단가)[^0-9]{0,25}([1-9][0-9,]{2,4})\s*원",
    ]
    for pattern in priority_patterns:
        for match in re.finditer(pattern, clean_text):
            val_str = match.group(1).replace(",", "")
            if val_str.isdigit():
                val = int(val_str)
                if 1000 <= val <= 50000:
                    return val
    return None


def extract_pay_date_from_text(text: str) -> str:
    clean_text = clean_html_text(text)
    if not clean_text:
        return ""

    date_patterns = [
        r"(?:지급\s*예정일|지급일|분배금\s*지급일|청산\s*지급일|반환\s*예정일|반환일)[^0-9]{0,20}(20[2-9][0-9])[년.\-/]?\s*([01]?[0-9])[월.\-/]?\s*([0-3]?[0-9])\s*일?",
        r"(20[2-9][0-9])[년.\-/]\s*([01]?[0-9])[월.\-/]\s*([0-3]?[0-9])\s*일?\s*(?:지급|예정|반환)",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, clean_text)
        if match:
            y, m, d = match.group(1), match.group(2).zfill(2), match.group(3).zfill(2)
            try:
                return datetime.strptime(f"{y}-{m}-{d}", "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


def fetch_document_text(rcept_no: str) -> str:
    if not DART_API_KEY or not rcept_no:
        return ""
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
            headers=HEADERS,
            timeout=12,
        )
        res.raise_for_status()
        if not res.content.startswith(b"PK"):
            return ""
        chunks: list[str] = []
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            for member in zf.namelist()[:6]:
                if member.lower().endswith((".xml", ".html", ".htm")):
                    raw_bytes = zf.read(member)
                    decoded = raw_bytes.decode("utf-8", errors="ignore") or raw_bytes.decode("euc-kr", errors="ignore")
                    chunks.append(decoded)
        return "\n".join(chunks)
    except Exception:
        return ""


def resolve_liquidation_price(
    spac: dict, disclosures: list[dict], ipo_prices: dict[str, int]
) -> tuple[int | None, str]:
    for d in disclosures:
        title = d.get("report_nm", "")
        price = extract_unit_price_from_text(title)
        if price is not None:
            return price, f"공시제목:{title}"

    liq_title_docs = [
        d for d in disclosures
        if d.get("rcept_no") and any(k in (d.get("report_nm") or "") for k in ("청산", "해산", "분배금", "예치"))
    ][:2]

    for d in liq_title_docs:
        doc_text = fetch_document_text(d["rcept_no"])
        price = extract_unit_price_from_text(doc_text)
        if price is not None:
            return price, f"공시본문:{d.get('report_nm', '')}"

    code = spac.get("code", "")
    norm_name = "name:" + re.sub(r"\s+", "", spac.get("name", "")).lower()
    ipo_price = ipo_prices.get(code) or ipo_prices.get(norm_name)
    if ipo_price is not None:
        return ipo_price, f"KRX신규상장공모가({ipo_price:,}원)"

    return None, "미확인"


def extract_liquidation_record(spac: dict, disclosures: list[dict]) -> dict | None:
    liq_keywords = ("청산", "해산", "분배금", "예치금 반환")
    candidates = [d for d in disclosures if any(k in (d.get("report_nm") or "") for k in liq_keywords)][:2]

    for d in candidates:
        title = d.get("report_nm", "")
        rcept_no = d.get("rcept_no", "")
        rcept_dt = d.get("rcept_dt", "")

        unit_price = extract_unit_price_from_text(title)
        pay_date = extract_pay_date_from_text(title)

        if (unit_price is None or not pay_date) and rcept_no:
            doc_text = fetch_document_text(rcept_no)
            if unit_price is None:
                unit_price = extract_unit_price_from_text(doc_text)
            if not pay_date:
                pay_date = extract_pay_date_from_text(doc_text)

        if not pay_date and len(re.sub(r"[^0-9]", "", rcept_dt)) >= 8:
            digits = re.sub(r"[^0-9]", "", rcept_dt)[:8]
            try:
                pay_date = datetime.strptime(digits, "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                pay_date = ""

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
    digits = re.sub(r"[^0-9]", "", listing_date or "")
    if len(digits) >= 8:
        try:
            dt_obj = datetime.strptime(digits[:8], "%Y%m%d")
            return dt_obj.replace(year=dt_obj.year + 3).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def build_spac_record(
    spac: dict, corp_codes: dict[str, str], deposit_rate: float, ipo_prices: dict[str, int]
) -> tuple[dict, dict | None]:
    code = spac["code"]
    corp_code = corp_codes.get(code, "")
    current_price, is_suspended = get_naver_quote(code)
    disclosures = get_recent_dart_disclosures(corp_code, days=365)

    has_merger_notice = any("합병" in (d.get("report_nm") or "") for d in disclosures[:15])
    has_liquidation_notice = any(
        any(k in (d.get("report_nm") or "") for k in ("해산", "청산", "상장폐지"))
        for d in disclosures[:15]
    )

    status = "정지" if is_suspended else ("합병" if has_merger_notice else "일반")
    invest_type = "합병" if has_merger_notice else "청산"
    liquidation_price, liq_source = resolve_liquidation_price(spac, disclosures, ipo_prices)
    liquidation_record = extract_liquidation_record(spac, disclosures)

    if liquidation_price is not None and current_price > 0:
        gross_yield = ((liquidation_price - current_price) / current_price) * 100
        net_gain = max(0.0, liquidation_price - current_price) * (1 - GENERAL_TAX_RATE)
        gen_yield = (
            ((liquidation_price - current_price) / current_price) * 100
            if liquidation_price <= current_price
            else (net_gain / current_price) * 100
        )
        yield_isa = round(gross_yield, 2)
        yield_general = round(gen_yield, 2)
        is_fair_buy = "O" if (current_price <= liquidation_price and not is_suspended) else "X"
    else:
        yield_isa = None
        yield_general = None
        is_fair_buy = "미확인"

    spac_record = {
        "code": code,
        "name": spac["name"],
        "listing_date": spac["listing_date"],
        "delisting_date": infer_delisting_date(spac.get("listing_date", "")),
        "corp_code": corp_code,
        "current_price": current_price,
        "liquidation_price": liquidation_price,
        "liquidation_price_source": liq_source,
        "is_fair_buy": is_fair_buy,
        "invest_type": invest_type,
        "yield_isa": yield_isa,
        "yield_general": yield_general,
        "deposit_rate": deposit_rate,
        "is_suspended": is_suspended,
        "has_liquidation_notice": has_liquidation_notice,
        "status": status,
        "recent_disclosures": disclosures[:15],
    }
    return spac_record, liquidation_record


def save_json_payload(items: list[dict], output_path: Path, updated_at: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": updated_at, "count": len(items), "items": items}
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[INFO] {output_path} 저장 완료 (updated_at={updated_at}, count={len(items)})")


def main() -> None:
    existing_spacs = load_existing_items(OUTPUT_PATH)
    existing_liqs = load_existing_items(LIQUIDATIONS_PATH)
    updated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")

    try:
        deposit_rate = get_base_deposit_rate()
        corp_codes = get_dart_corp_codes()
        ipo_prices = get_krx_ipo_prices()
        spac_list = get_krx_spac_list()

        if not spac_list:
            print("[WARN] 스팩 목록이 비어 있어 기존 데이터를 유지하며 갱신시각만 반영합니다.")
            save_json_payload(existing_spacs, OUTPUT_PATH, updated_at)
            save_json_payload(existing_liqs, LIQUIDATIONS_PATH, updated_at)
            return

        spac_results: list[dict] = []
        liquidation_results: list[dict] = []

        for idx, spac in enumerate(spac_list, start=1):
            try:
                spac_record, liq_record = build_spac_record(spac, corp_codes, deposit_rate, ipo_prices)
                spac_results.append(spac_record)
                if liq_record is not None:
                    liquidation_results.append(liq_record)
            except Exception as exc:
                print(f"[WARN] [{spac.get('code', '')}] 처리 중 개별 오류: {exc}")
            if idx % 20 == 0:
                print(f"[PROGRESS] {idx}/{len(spac_list)} 처리 중...")
            time.sleep(0.05)

        valid_price_count = sum(1 for r in spac_results if (r.get("current_price") or 0) > 0)
        if valid_price_count == 0 and existing_spacs:
            print("[WARN] 모든 종목 현재가가 0원입니다. 기존 spacs.json 데이터를 보존합니다.")
            spac_results = existing_spacs

        save_json_payload(spac_results, OUTPUT_PATH, updated_at)
        save_json_payload(liquidation_results, LIQUIDATIONS_PATH, updated_at)

    except Exception as exc:
        print(f"[ERROR] 실행 중 예외 발생: {exc}")
        traceback.print_exc()
        save_json_payload(existing_spacs, OUTPUT_PATH, updated_at)
        save_json_payload(existing_liqs, LIQUIDATIONS_PATH, updated_at)


if __name__ == "__main__":
    main()
