import os
import re
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests


def extract_date_range(text: str):
    if not text:
        return None
    m_range = re.search(
        r"(20[2-9][0-9])[년./-]\s*([01]?[0-9])[월./-]\s*([0-3]?[0-9])\s*일?\s*(?:~|부터|-|까지)\s*(?:(20[2-9][0-9])[년./-]\s*)?([01]?[0-9])[월./-]\s*([0-3]?[0-9])\s*일?",
        text,
    )
    if m_range:
        y1 = m_range.group(1)
        m1 = m_range.group(2).zfill(2)
        d1 = m_range.group(3).zfill(2)
        y2 = m_range.group(4) or y1
        m2 = m_range.group(5).zfill(2)
        d2 = m_range.group(6).zfill(2)
        return f"{y1}-{m1}-{d1} ~ {y2}-{m2}-{d2}"

    m_single = re.search(
        r"(20[2-9][0-9])[년./-]\s*([01]?[0-9])[월./-]\s*([0-3]?[0-9])\s*일?",
        text,
    )
    if m_single:
        return f"{m_single.group(1)}-{m_single.group(2).zfill(2)}-{m_single.group(3).zfill(2)}"
    return None


def _parse_krw_amount(raw: str):
    if not raw:
        return None
    cleaned = raw.replace(",", "").strip()
    try:
        val = int(float(cleaned))
        if 100 <= val <= 500000:
            return val
    except ValueError:
        pass
    return None


def parse_merger_decision_text(xml_or_text: str, fallback_target: str = None) -> dict:
    """
    주요사항보고서(회사합병결정) 본문에서
    - 합병 대상 회사명
    - 10. 합병일정 -> 합병반대의사통지 접수기간
    - 주주총회(예정)일
    - 주식매수청구권 행사기간
    을 추출합니다.
    """
    text = re.sub(r"<[^>]+>", " ", xml_or_text or "")
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()

    target = fallback_target
    if not target:
        m_target = re.search(
            r"(?:합병\s*대상\s*회사(?:명)?|피합병회사(?:명)?|합병상대회사(?:명)?|대상회사명)\s*[:：|]?\s*([가-힣A-Za-z0-9()·&\-_\s]{2,40}?)(?=\s*(?:\||주주총회|합병기일|합병비율|주식매수청구권|반대의사|$))",
            text,
        )
        if m_target:
            cand = re.sub(r"\s+", " ", m_target.group(1)).strip(" :：|-")
            if cand and cand not in ("해당사항없음", "미정", "없음"):
                target = cand

    sched_match = re.search(r"10\.\s*합병일정|합병일정", text)
    sched_text = text[sched_match.start() : sched_match.start() + 8000] if sched_match else text

    opp_period = None
    m_opp = re.search(r"합병반대의사통지\s*접수기간.{0,260}", sched_text) or re.search(
        r"(?:합병반대의사통지\s*접수기간|반대의사\s*표시\s*기간).{0,260}", text
    )
    if m_opp:
        opp_period = extract_date_range(m_opp.group(0))

    gm_date = None
    m_gm = re.search(r"(?:주주총회\s*(?:예정)?일|임시주주총회\s*(?:예정)?일|주주총회일).{0,220}", sched_text) or re.search(
        r"(?:주주총회\s*(?:예정)?일|임시주주총회\s*(?:예정)?일|주주총회일).{0,220}", text
    )
    if m_gm:
        gm_date = extract_date_range(m_gm.group(0))

    ex_period = None
    m_ex = re.search(r"(?:주식매수청구권\s*행사\s*기간|매수청구권\s*행사\s*기간|매수청구\s*기간).{0,260}", sched_text) or re.search(
        r"(?:주식매수청구권\s*행사\s*기간|매수청구권\s*행사\s*기간|매수청구\s*기간).{0,260}", text
    )
    if m_ex:
        ex_period = extract_date_range(m_ex.group(0))

    return {
        "merger_target": target,
        "opposing_notice_period": opp_period,
        "general_meeting_date": gm_date,
        "appraisal_exercise_period": ex_period,
    }


def parse_merger_registration_text(xml_or_text: str) -> dict:
    """
    증권신고서(합병)의 '제1부 합병의 개요' -> '주식매수청구권에 관한 사항'에서
    1차 매수청구가(협의 제시가격) 및 2차 매수청구가(협의 불성립 시 제시/매수가격)와 일정을 추출합니다.
    """
    raw_stripped = re.sub(r"<[^>]+>", " ", xml_or_text or "")
    raw_stripped = raw_stripped.replace("&nbsp;", " ")

    part1_m = re.search(r"제\s*1\s*부\s*합병의\s*개요", raw_stripped)
    search_base = raw_stripped[part1_m.start() :] if part1_m else raw_stripped

    buy_m = re.search(r"주식매수청구권에\s*관한\s*사항", search_base)
    if buy_m:
        sec = search_base[buy_m.start() : buy_m.start() + 18000]
    else:
        fb_m = re.search(r"주식매수청구권에\s*관한\s*사항", raw_stripped)
        sec = raw_stripped[fb_m.start() : fb_m.start() + 18000] if fb_m else raw_stripped

    norm_sec = re.sub(r"\s*\|\s*", " ", sec)
    norm_sec = re.sub(r"\s+", " ", norm_sec).strip()

    first_price = None
    p1_patterns = [
        r"(?:1차\s*(?:매수청구(?:가|가격)?|제시\s*가격|협의\s*가격)|협의\s*매수가격|협의를\s*위한\s*회사의\s*제시가격|1차\s*제시\s*매수가격)[^0-9원]{0,120}([1-9][0-9,]{2,5})\s*원",
        r"1차[^0-9원]{0,40}(?:제시|매수|청구)[^0-9원]{0,60}([1-9][0-9,]{2,5})\s*원",
    ]
    for pat in p1_patterns:
        m = re.search(pat, norm_sec)
        if m:
            val = _parse_krw_amount(m.group(1))
            if val is not None:
                first_price = val
                break

    second_price = None
    p2_patterns = [
        r"(?:2차\s*(?:매수청구(?:가|가격)?|제시\s*가격|매수가격)|협의가\s*성립되지\s*(?:아니할|않는|않은)\s*경우[^0-9원]{0,140}|협의\s*불성립\s*시[^0-9원]{0,120})([1-9][0-9,]{2,5})\s*원",
        r"2차[^0-9원]{0,40}(?:매수청구|제시|매수)[^0-9원]{0,60}([1-9][0-9,]{2,5})\s*원",
    ]
    for pat in p2_patterns:
        m = re.search(pat, norm_sec)
        if m:
            val = _parse_krw_amount(m.group(1))
            if val is not None:
                second_price = val
                break

    if first_price is None or second_price is None:
        m_pair = re.search(
            r"1차[^0-9원]{0,50}([1-9][0-9,]{2,5})\s*원.{0,180}?2차[^0-9원]{0,50}([1-9][0-9,]{2,5})\s*원",
            norm_sec,
        )
        if m_pair:
            if first_price is None:
                first_price = _parse_krw_amount(m_pair.group(1))
            if second_price is None:
                second_price = _parse_krw_amount(m_pair.group(2))

    opp_period = None
    m_opp = re.search(r"(?:합병반대의사통지\s*접수기간|반대의사\s*표시\s*기간).{0,260}", norm_sec)
    if m_opp:
        opp_period = extract_date_range(m_opp.group(0))

    gm_date = None
    m_gm = re.search(r"(?:주주총회\s*(?:예정)?일|임시주주총회\s*(?:예정)?일|주주총회일).{0,220}", norm_sec)
    if m_gm:
        gm_date = extract_date_range(m_gm.group(0))

    ex_period = None
    m_ex = re.search(r"(?:주식매수청구권\s*행사\s*기간|매수청구권\s*행사\s*기간|매수청구\s*기간).{0,260}", norm_sec)
    if m_ex:
        ex_period = extract_date_range(m_ex.group(0))

    return {
        "first_offer_price": first_price,
        "second_appraisal_price": second_price,
        "opposing_notice_period": opp_period,
        "general_meeting_date": gm_date,
        "appraisal_exercise_period": ex_period,
    }


def fetch_dart_disclosure_list(corp_code: str, api_key: str, years_back: int = 4) -> list:
    if not api_key or not corp_code:
        return []
    now = datetime.now(timezone.utc)
    end_de = now.strftime("%Y%m%d")
    bgn_de = f"{now.year - years_back}{now.strftime('%m%d')}"
    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={
                "crtfc_key": api_key,
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "page_count": 100,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "000" and isinstance(data.get("list"), list):
            return data["list"]
    except Exception:
        pass
    return []


def fetch_dart_document_text(rcept_no: str, api_key: str) -> str:
    if not api_key or not rcept_no:
        return ""
    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": api_key, "rcept_no": rcept_no},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.content.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def enrich_spac_merger_appraisal_info(spac_item: dict, api_key: str = None, doc_cache: dict = None) -> dict:
    """
    스팩 종목 딕셔너리에 합병 및 주식매수청구권 정보(1차/2차 매수청구가, 반대 표시 기간, 주주총회일, 매수청구권 행사기간)를 채웁니다.
    """
    key = (api_key or os.environ.get("DART_API_KEY") or "").strip()
    cache = doc_cache if isinstance(doc_cache, dict) else {}

    existing_info = spac_item.get("merger_appraisal_info") or {}
    result = {
        "merger_target": spac_item.get("merger_target") or existing_info.get("merger_target"),
        "first_offer_price": spac_item.get("first_offer_price") or existing_info.get("first_offer_price"),
        "second_appraisal_price": spac_item.get("second_appraisal_price") or existing_info.get("second_appraisal_price"),
        "opposing_notice_period": spac_item.get("opposing_notice_period") or existing_info.get("opposing_notice_period"),
        "general_meeting_date": spac_item.get("general_meeting_date") or existing_info.get("general_meeting_date"),
        "appraisal_exercise_period": spac_item.get("appraisal_exercise_period") or existing_info.get("appraisal_exercise_period"),
    }

    corp_code = (spac_item.get("corp_code") or "").strip()
    disclosures = spac_item.get("recent_disclosures") or []
    if key and corp_code and not disclosures:
        disclosures = fetch_dart_disclosure_list(corp_code, key)

    sorted_disc = sorted(
        [d for d in disclosures if isinstance(d, dict)],
        key=lambda x: str(x.get("rcept_dt") or ""),
        reverse=True,
    )

    if key:
        dec_docs = [
            d for d in sorted_disc
            if d.get("rcept_no") and re.search(r"회사합병결정|합병결정", re.sub(r"\s+", "", str(d.get("report_nm") or "")))
        ]
        for d_doc in dec_docs[:2]:
            r_no = str(d_doc["rcept_no"])
            if r_no not in cache:
                cache[r_no] = fetch_dart_document_text(r_no, key)
            parsed_dec = parse_merger_decision_text(cache[r_no], fallback_target=result["merger_target"])
            if parsed_dec.get("merger_target"):
                result["merger_target"] = parsed_dec["merger_target"]
            if not result["opposing_notice_period"] and parsed_dec.get("opposing_notice_period"):
                result["opposing_notice_period"] = parsed_dec["opposing_notice_period"]
            if not result["general_meeting_date"] and parsed_dec.get("general_meeting_date"):
                result["general_meeting_date"] = parsed_dec["general_meeting_date"]
            if not result["appraisal_exercise_period"] and parsed_dec.get("appraisal_exercise_period"):
                result["appraisal_exercise_period"] = parsed_dec["appraisal_exercise_period"]

        reg_docs = [
            d for d in sorted_disc
            if d.get("rcept_no") and "증권신고서(합병)" in re.sub(r"\s+", "", str(d.get("report_nm") or ""))
        ]
        t_norm = re.sub(r"\s+", "", str(result.get("merger_target") or "")).lower()
        if t_norm:
            reg_docs.sort(
                key=lambda d: (
                    1 if t_norm in re.sub(r"\s+", "", str(d.get("report_nm") or "")).lower() else 0,
                    str(d.get("rcept_dt") or ""),
                ),
                reverse=True,
            )

        for r_doc in reg_docs[:3]:
            r_no = str(r_doc["rcept_no"])
            if r_no not in cache:
                cache[r_no] = fetch_dart_document_text(r_no, key)
            parsed_reg = parse_merger_registration_text(cache[r_no])
            if result["first_offer_price"] is None and parsed_reg.get("first_offer_price") is not None:
                result["first_offer_price"] = parsed_reg["first_offer_price"]
            if result["second_appraisal_price"] is None and parsed_reg.get("second_appraisal_price") is not None:
                result["second_appraisal_price"] = parsed_reg["second_appraisal_price"]
            if not result["opposing_notice_period"] and parsed_reg.get("opposing_notice_period"):
                result["opposing_notice_period"] = parsed_reg["opposing_notice_period"]
            if not result["general_meeting_date"] and parsed_reg.get("general_meeting_date"):
                result["general_meeting_date"] = parsed_reg["general_meeting_date"]
            if not result["appraisal_exercise_period"] and parsed_reg.get("appraisal_exercise_period"):
                result["appraisal_exercise_period"] = parsed_reg["appraisal_exercise_period"]

    spac_item["merger_target"] = result["merger_target"]
    spac_item["first_offer_price"] = result["first_offer_price"]
    spac_item["second_appraisal_price"] = result["second_appraisal_price"]
    spac_item["opposing_notice_period"] = result["opposing_notice_period"]
    spac_item["general_meeting_date"] = result["general_meeting_date"]
    spac_item["appraisal_exercise_period"] = result["appraisal_exercise_period"]
    spac_item["merger_appraisal_info"] = result
    return spac_item


def enrich_all_spacs_merger_info(spac_items: list, api_key: str = None) -> list:
    cache = {}
    for item in spac_items or []:
        if isinstance(item, dict):
            enrich_spac_merger_appraisal_info(item, api_key=api_key, doc_cache=cache)
    return spac_items


if __name__ == "__main__":
    sample_dec = """
    10. 합병일정
    합병반대의사통지 접수기간 : 2026년 12월 01일 ~ 2026년 12월 14일
    주주총회 예정일 : 2026년 12월 15일
    주식매수청구권 행사기간 : 2026년 12월 15일 ~ 2027년 01월 04일
    """
    sample_reg = """
    제1부 합병의 개요
    주식매수청구권에 관한 사항
    1차 매수청구가(협의 제시가격) : 2,045원
    협의가 성립되지 아니할 경우 제시 가격(2차 매수청구가) : 2,052원
    합병반대의사통지 접수기간 : 2026.12.01 ~ 2026.12.14
    주주총회 예정일 : 2026.12.15
    주식매수청구권 행사기간 : 2026.12.15 ~ 2027.01.04
    """
    p1 = parse_merger_decision_text(sample_dec, fallback_target="샘플테크")
    p2 = parse_merger_registration_text(sample_reg)
    print("Decision parsed:", p1)
    print("Registration parsed:", p2)
