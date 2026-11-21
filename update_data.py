import io
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

# =========================
# 설정값 (환경변수 우선)
# =========================
DART_API_KEY = os.getenv("DART_API_KEY", "").strip()
KRX_API_KEY = os.getenv("KRX_API_KEY", "").strip()
OUT_FILE = Path(os.getenv("OUT_FILE", "data/spacs.json"))

KST = timezone(timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 2024년~2026년 기준 정기예금 금리(연 %, 월별 폴백 테이블)
DEFAULT_DEPOSIT_RATES = {
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


def now_kst() -> datetime:
    return datetime.now(KST)


def parse_int(val, default=0) -> int:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return int(val)
    s = re.sub(r"[^0-9\-]", "", str(val))
    if not s or s == "-":
        return default
    try:
        return int(s)
    except ValueError:
        return default


def parse_float(val, default=0.0) -> float:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = re.sub(r"[^0-9.\-]", "", str(val))
    if not s or s == "-":
        return default
    try:
        return float(s)
    except ValueError:
        return default


def normalize_date(val) -> str | None:
    if not val:
        return None
    s = str(val).strip()
    digits = re.sub(r"[^0-9]", "", s)
    if len(digits) >= 8:
        yyyy, mm, dd = digits[:4], digits[4:6], digits[6:8]
        try:
            dt = datetime(int(yyyy), int(mm), int(dd))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def add_years_safe(date_str: str, years: int) -> str | None:
    norm = normalize_date(date_str)
    if not norm:
        return None
    dt = datetime.strptime(norm, "%Y-%m-%d")
    try:
        return dt.replace(year=dt.year + years).strftime("%Y-%m-%d")
    except ValueError:
        return dt.replace(year=dt.year + years, month=2, day=28).strftime("%Y-%m-%d")


def is_spac_name(name: str) -> bool:
    if not name:
        return False
    return bool(re.search(r"(스팩|SPAC|기업인수목적)", name, re.IGNORECASE))


# =========================
# 1. 한국은행(ECOS) 기준 예금금리 조회
# =========================
def fetch_bok_deposit_rates() -> dict:
    rates = dict(DEFAULT_DEPOSIT_RATES)
    ecos_key = os.getenv("ECOS_API_KEY", "").strip()
    if not ecos_key:
        return rates

    start_ym = "202401"
    end_ym = now_kst().strftime("%Y%m")
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{ecos_key}/json/kr/1/200/"
        f"121Y002/M/{start_ym}/{end_ym}/0101000"
    )
    try:
        res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=12)
        res.raise_for_status()
        rows = res.json().get("StatisticSearch", {}).get("row", [])
        for r in rows:
            time_str = str(r.get("TIME", "")).strip()
            val = parse_float(r.get("DATA_VALUE"), default=-1.0)
            if len(time_str) == 6 and val > 0:
                ym = f"{time_str[:4]}-{time_str[4:6]}"
                rates[ym] = round(val, 2)
    except Exception as e:
        print(f"[WARN] ECOS 금리 조회 실패(폴백 사용): {e}")

    return rates


# =========================
# 2. KRX 종목/시세 조회
# =========================
def get_recent_business_days(count: int = 10) -> list[str]:
    days = []
    cur = now_kst().date()
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y%m%d"))
        cur -= timedelta(days=1)
    return days


def fetch_krx_spac_list_and_quotes() -> list[dict]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
        "X-Requested-With": "XMLHttpRequest",
    })

    try:
        session.get(
            "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?menuId=MDC0201020101",
            timeout=10,
        )
    except Exception:
        pass

    quote_map = {}
    used_trade_date = None

    for ymd in get_recent_business_days(10):
        temp_map = {}
        for mkt_id in ("KSQ", "STK"):
            payload = {
                "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
                "locale": "ko_KR",
                "mktId": mkt_id,
                "trdDd": ymd,
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            }
            try:
                resp = session.post(
                    "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
                    data=payload,
                    timeout=15,
                )
                resp.raise_for_status()
                out_block = resp.json().get("OutBlock_1", [])
                for row in out_block:
                    name = (row.get("ISU_ABBRV") or "").strip()
                    code = (row.get("ISU_SRT_CD") or "").strip().zfill(6)
                    if not code or not is_spac_name(name):
                        continue

                    close_prc = parse_int(row.get("TDD_CLSPRC"), default=0)
                    chg_rt = parse_float(row.get("FLUC_RT"), default=0.0)
                    acc_val = parse_int(row.get("ACC_TRDVAL"), default=0)
                    acc_vol = parse_int(row.get("ACC_TRDVOL"), default=0)
                    mkt_cap = parse_int(row.get("MKTCAP"), default=0)

                    temp_map[code] = {
                        "code": code,
                        "name": name,
                        "market": "KOSDAQ" if mkt_id == "KSQ" else "KOSPI",
                        "current_price": close_prc,
                        "change_rate": chg_rt,
                        "trading_value": acc_val,
                        "volume": acc_vol,
                        "market_cap": mkt_cap,
                        "trade_date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}",
                    }
            except Exception as e:
                print(f"[WARN] KRX 시세 조회 실패 ({mkt_id}, {ymd}): {e}")

        if len(temp_map) >= 10:
            quote_map = temp_map
            used_trade_date = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
            break

    print(f"[INFO] KRX 스팩 시세 확보: {len(quote_map)}개 (기준일: {used_trade_date})")

    listing_map = {}
    for ymd in ([used_trade_date.replace("-", "")] if used_trade_date else get_recent_business_days(5)):
        for mkt_id in ("KSQ", "STK"):
            payload = {
                "bld": "dbms/MDC/STAT/standard/MDCSTAT01901",
                "locale": "ko_KR",
                "mktId": mkt_id,
                "trdDd": ymd,
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            }
            try:
                resp = session.post(
                    "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
                    data=payload,
                    timeout=15,
                )
                if resp.status_code == 200:
                    rows = resp.json().get("OutBlock_1", [])
                    for r in rows:
                        c = (r.get("ISU_SRT_CD") or "").strip().zfill(6)
                        ld = normalize_date(r.get("LIST_DD"))
                        if c and ld:
                            listing_map[c] = ld
            except Exception:
                pass
        if listing_map:
            break

    results = []
    for code, q in quote_map.items():
        q["listing_date"] = listing_map.get(code)
        results.append(q)
    return results


# =========================
# 3. OpenDART 공시/합병/청산 조회
# =========================
def fetch_dart_corp_map() -> dict:
    corp_map = {}
    if not DART_API_KEY:
        return corp_map

    url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={urllib.parse.quote(DART_API_KEY)}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_name = zf.namelist()[0]
            xml_bytes = zf.read(xml_name)
        root = ET.fromstring(xml_bytes)
        for item in root.findall("list"):
            stock_code = (item.findtext("stock_code") or "").strip().zfill(6)
            corp_code = (item.findtext("corp_code") or "").strip()
            corp_name = (item.findtext("corp_name") or "").strip()
            if stock_code != "000000" and corp_code and is_spac_name(corp_name):
                corp_map[stock_code] = corp_code
        print(f"[INFO] DART 스팩 고유번호 매핑: {len(corp_map)}개")
    except Exception as e:
        print(f"[WARN] DART corpCode 조회 실패: {e}")

    return corp_map


def inspect_dart_disclosures(corp_code: str) -> dict:
    info = {
        "merger_status": None,
        "merger_target": None,
        "merger_approved_at": None,
        "has_liquidation_notice": False,
        "liquidation_price": None,
        "liquidation_date": None,
        "latest_report": None,
        "latest_report_date": None,
    }
    if not DART_API_KEY or not corp_code:
        return info

    bgn_de = (now_kst() - timedelta(days=730)).strftime("%Y%m%d")
    end_de = now_kst().strftime("%Y%m%d")
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "page_count": 30,
    }
    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "000":
            return info

        entries = data.get("list", [])
        if entries:
            info["latest_report"] = entries[0].get("report_nm")
            info["latest_report_date"] = normalize_date(entries[0].get("rcept_dt"))

        for e in entries:
            rep = str(e.get("report_nm") or "")
            rcept_dt = normalize_date(e.get("rcept_dt"))

            if any(k in rep for k in ("합병", "주식교환", "영업양수도", "주요사항보고서(회사합병결정)")):
                if not info["merger_status"]:
                    if "승인" in rep or "결의" in rep:
                        info["merger_status"] = "합병승인"
                    elif "신고서" in rep or "결정" in rep:
                        info["merger_status"] = "합병추진"
                    else:
                        info["merger_status"] = "합병관련공시"
                    info["merger_approved_at"] = rcept_dt
                    m_target = re.search(r"합병\s*\(([^)]+)\)", rep)
                    if m_target:
                        info["merger_target"] = m_target.group(1).strip()

            if any(k in rep for k in ("해산", "청산", "분배금", "예치금반환", "상장폐지")):
                info["has_liquidation_notice"] = True
                m_amt = re.search(r"([1-9][0-9,]{2,4})\s*원", rep)
                if m_amt and not info["liquidation_price"]:
                    cand_price = parse_int(m_amt.group(1), default=0)
                    if 1500 <= cand_price <= 15000:
                        info["liquidation_price"] = cand_price
                if not info["liquidation_date"]:
                    info["liquidation_date"] = rcept_dt

    except Exception as e:
        print(f"[WARN] DART 공시 조회 오류 ({corp_code}): {e}")

    return info


# =========================
# 4. 청산가 / 예상수익률 / 매수적정 계산
# =========================
def estimate_ipo_base_price(current_price: int) -> int:
    if current_price >= 8000:
        return 10000
    return 2000


def estimate_liquidation_price(
    ipo_price: int,
    listing_date: str | None,
    maturity_date: str | None,
    deposit_rates: dict,
    dart_liq_price: int | None = None,
) -> tuple[int, str]:
    if dart_liq_price and dart_liq_price > 0:
        return dart_liq_price, "DART 청산/분배 공시"

    start_dt = datetime.strptime(listing_date, "%Y-%m-%d") if listing_date else (now_kst().replace(tzinfo=None) - timedelta(days=365))
    end_dt = datetime.strptime(maturity_date, "%Y-%m-%d") if maturity_date else (start_dt + timedelta(days=1095))
    days = max(30, (end_dt - start_dt).days)
    years = days / 365.0

    ym_key = (listing_date or now_kst().strftime("%Y-%m-%d"))[:7]
    gross_rate = deposit_rates.get(ym_key, 2.35)
    net_rate = (gross_rate * 0.845) / 100.0

    est_price = round(ipo_price * ((1.0 + net_rate) ** years))
    est_price = max(ipo_price, min(est_price, int(ipo_price * 1.25)))
    return est_price, f"예금금리({gross_rate:.2f}%·세후) 복리추정"


def calc_annualized_yield(buy_price: int, target_price: int, days_to_exit: int, is_isa: bool) -> float:
    if buy_price <= 0 or target_price <= 0 or days_to_exit <= 0:
        return 0.0
    effective_target = float(target_price)
    if not is_isa and effective_target > buy_price:
        gain = effective_target - buy_price
        effective_target = buy_price + gain * (1.0 - 0.154)

    ratio = effective_target / float(buy_price)
    if ratio <= 0:
        return -100.0
    ann = (ratio ** (365.0 / float(days_to_exit)) - 1.0) * 100.0
    return round(ann, 2)


def build_spac_records() -> dict:
    today_dt = now_kst()
    today_str = today_dt.strftime("%Y-%m-%d")
    today_date = today_dt.date()

    deposit_rates = fetch_bok_deposit_rates()
    latest_ym = sorted(deposit_rates.keys())[-1]
    base_gross_rate = deposit_rates[latest_ym]
    net_deposit_benchmark = round(base_gross_rate * 0.845, 2)

    krx_items = fetch_krx_spac_list_and_quotes()
    dart_corp_map = fetch_dart_corp_map()

    prev_map = {}
    if OUT_FILE.exists():
        try:
            prev_json = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            for p in prev_json.get("items", []):
                if p.get("code"):
                    prev_map[str(p["code"]).zfill(6)] = p
        except Exception:
            pass

    items = []
    for idx, q in enumerate(krx_items):
        code = str(q["code"]).zfill(6)
        name = q["name"]
        current_price = parse_int(q.get("current_price"), default=0)
        prev_rec = prev_map.get(code, {})

        listing_date = (
            q.get("listing_date")
            or normalize_date(prev_rec.get("listing_date"))
            or normalize_date(prev_rec.get("listingDate"))
        )
        maturity_date = (
            add_years_safe(listing_date, 3)
            if listing_date
            else (
                normalize_date(prev_rec.get("delisting_date"))
                or normalize_date(prev_rec.get("maturity_date"))
                or normalize_date(prev_rec.get("maturityDate"))
            )
        )

        corp_code = dart_corp_map.get(code)
        dart_info = inspect_dart_disclosures(corp_code) if (DART_API_KEY and corp_code) else {
            "merger_status": prev_rec.get("merger_status") or prev_rec.get("mergerStatus"),
            "merger_target": prev_rec.get("merger_target") or prev_rec.get("mergerTarget"),
            "merger_approved_at": prev_rec.get("merger_approved_at") or prev_rec.get("mergerApprovedAt"),
            "has_liquidation_notice": bool(prev_rec.get("has_liquidation_notice", prev_rec.get("liquidationNotice", False))),
            "liquidation_price": prev_rec.get("liquidation_price") or prev_rec.get("liquidationPrice"),
            "liquidation_date": prev_rec.get("liquidation_date") or prev_rec.get("liquidationDate"),
            "latest_report": prev_rec.get("latest_report") or prev_rec.get("latestReport"),
            "latest_report_date": prev_rec.get("latest_report_date") or prev_rec.get("latestReportDate"),
        }
        if DART_API_KEY and corp_code and (idx % 5 == 4):
            time.sleep(0.15)

        remaining_days = None
        if maturity_date:
            mat_d = datetime.strptime(maturity_date, "%Y-%m-%d").date()
            remaining_days = (mat_d - today_date).days

        ipo_price = estimate_ipo_base_price(current_price)
        liq_price, liq_source = estimate_liquidation_price(
            ipo_price=ipo_price,
            listing_date=listing_date,
            maturity_date=maturity_date,
            deposit_rates=deposit_rates,
            dart_liq_price=parse_int(dart_info.get("liquidation_price"), default=0) or None,
        )

        is_suspended = current_price <= 0 or parse_int(q.get("volume"), default=0) == 0
        has_merger = bool(dart_info.get("merger_status"))

        # 대시보드 필터(일반 / 정지 / 합병)와 일치하도록 상태 지정
        if is_suspended:
            status = "정지"
        elif has_merger:
            status = "합병"
        else:
            status = "일반"

        # 대시보드 유형 필터(합병 / 청산)와 일치하도록 지정
        if remaining_days is not None:
            invest_type = "합병" if remaining_days >= 365 else "청산"
        else:
            invest_type = "합병" if has_merger else "청산"

        days_for_calc = remaining_days if (remaining_days and remaining_days > 0) else 365
        isa_ann_yield = calc_annualized_yield(current_price, liq_price, days_for_calc, is_isa=True) if current_price > 0 else 0.0
        gen_ann_yield = calc_annualized_yield(current_price, liq_price, days_for_calc, is_isa=False) if current_price > 0 else 0.0
        simple_yield = round(((liq_price - current_price) / current_price) * 100.0, 2) if current_price > 0 else 0.0

        fair_buy = bool(
            (not is_suspended)
            and current_price > 0
            and (current_price <= liq_price or isa_ann_yield >= net_deposit_benchmark)
        )

        item = {
            "code": code,
            "name": name,
            "market": q.get("market", "KOSDAQ"),
            "status": status,
            "invest_type": invest_type,
            "is_fair_buy": "O" if fair_buy else "X",
            "current_price": current_price,
            "change_rate": q.get("change_rate", 0.0),
            "ipo_price": ipo_price,
            "liquidation_price": liq_price,
            "liquidation_source": liq_source,
            "listing_date": listing_date,
            "delisting_date": maturity_date,
            "maturity_date": maturity_date,
            "remaining_days": remaining_days,
            "trading_on_krx": not is_suspended,
            "volume": q.get("volume", 0),
            "trading_value": q.get("trading_value", 0),
            "market_cap": q.get("market_cap", 0),
            "trade_date": q.get("trade_date"),
            "merger_status": dart_info.get("merger_status"),
            "merger_target": dart_info.get("merger_target"),
            "merger_approved_at": dart_info.get("merger_approved_at"),
            "has_liquidation_notice": bool(dart_info.get("has_liquidation_notice", False)),
            "latest_report": dart_info.get("latest_report"),
            "latest_report_date": dart_info.get("latest_report_date"),
            "yield_isa": isa_ann_yield,
            "yield_general": gen_ann_yield,
            "simple_yield": simple_yield,
            "deposit_rate_gross": base_gross_rate,
            "deposit_rate_after_tax": net_deposit_benchmark,
        }
        items.append(item)

    items.sort(key=lambda x: x.get("name") or "")

    return {
        "updated_at": today_dt.strftime("%Y-%m-%d %H:%M:%S KST"),
        "date": today_str,
        "count": len(items),
        "deposit_rate_gross": base_gross_rate,
        "deposit_rate_after_tax": net_deposit_benchmark,
        "deposit_rates": deposit_rates,
        "items": items,
    }


def main():
    payload = build_spac_records()
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[DONE] {OUT_FILE} 저장 완료 — 총 {payload['count']}개 종목 "
        f"(기준예금금리 세후: {payload['deposit_rate_after_tax']}%)"
    )


if __name__ == "__main__":
    main()
