import csv
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
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


def get_risk_disclosure_titles(raw: Dict[str, Any]) -> List[str]:
    risk_keywords = (
        "상장폐지",
        "해산",
        "청산",
        "주권매매정지",
        "기업인수목적회사 해산",
    )
    disclosures = raw.get("recent_disclosures") or []
    if not isinstance(disclosures, list):
        return []
    matched: List[str] = []
    for d in disclosures:
        if isinstance(d, dict):
            title = str(d.get("report_nm") or "").strip()
            if title and any(k in title for k in risk_keywords):
                matched.append(title)
    return matched


def evaluate_spac_eligibility(
    raw: Dict[str, Any],
    name: str,
    delisting_date_str: str,
    remaining_days: Optional[int],
    status: str,
    current_price: Optional[int],
    liquidation_price: Optional[int],
    yield_isa: Optional[float],
    yield_general: Optional[float],
) -> tuple[bool, List[str]]:
    reasons: List[str] = []

    if not name:
        reasons.append("missing_name")

    is_spac_flag = raw.get("is_spac")
    if is_spac_flag is False:
        reasons.append("is_spac_false")
    elif is_spac_flag is not True:
        dept = str(raw.get("krx_dept") or "")
        if not re.search(r"(스팩|SPAC|기업인수목적)", f"{name} {dept}", flags=re.IGNORECASE):
            reasons.append("not_spac")

    if not delisting_date_str or remaining_days is None:
        reasons.append("missing_delisting_date")
    elif remaining_days <= 0:
        reasons.append("expired_maturity")

    raw_rem = to_float(raw.get("remaining_days"))
    if raw_rem is not None and raw_rem <= 0:
        reasons.append("non_positive_raw_remaining_days")

    if any(k in status for k in ("상장폐지", "폐지", "해산", "청산", "합병완료")):
        reasons.append(f"status:{status}")

    if bool(raw.get("is_suspended")) or "정지" in status:
        reasons.append("trading_suspended")

    if current_price is None or current_price <= 0:
        reasons.append("invalid_current_price")

    if liquidation_price is None or liquidation_price <= 0:
        reasons.append("invalid_liquidation_price")

    if yield_isa is None or yield_general is None:
        reasons.append("missing_yield")

    if EXCLUDE_DELISTING_NOTICE:
        dept = str(raw.get("krx_dept") or "")
        if "관리종목" in dept:
            reasons.append("administrative_issue")
        if bool(raw.get("has_liquidation_notice")):
            reasons.append("liquidation_notice_flag")
        risk_titles = get_risk_disclosure_titles(raw)
        if risk_titles:
            reasons.append(f"risk_disclosure:{risk_titles[0]}")

    return (len(reasons) == 0), reasons


def is_valid_active_spac(
    raw: Dict[str, Any],
    delisting_date_str: str,
    remaining_days: Optional[int],
    status: str,
    yield_isa: Optional[float],
    yield_general: Optional[float],
) -> bool:
    name = str(raw.get("name") or raw.get("stock_name") or "").strip()
    current_price = to_int(raw.get("current_price") or raw.get("price") or raw.get("close"))
    liquidation_price = to_int(
        raw.get("liquidation_price")
        or raw.get("expected_liquidation_price")
        or raw.get("refund_price")
    )
    ok, reasons = evaluate_spac_eligibility(
        raw=raw,
        name=name,
        delisting_date_str=delisting_date_str,
        remaining_days=remaining_days,
        status=status,
        current_price=current_price,
        liquidation_price=liquidation_price,
        yield_isa=yield_isa,
        yield_general=yield_general,
    )
    if not ok:
        code = str(raw.get("code") or raw.get("ticker") or raw.get("short_code") or "").strip()
        print(f"[FILTER] [{code}] {name} 제외 -> {', '.join(reasons)}")
    return ok


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
        return explicit

    candidates = [v for v in (yield_isa, yield_general) if v is not None]
    if candidates:
        return "O" if max(candidates) >= after_tax_deposit_rate else "X"

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
