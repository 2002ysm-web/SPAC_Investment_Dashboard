#!/usr/bin/env python3
"""DART 공시에서 SPAC 청산분배금 지급일/분배단가를 모아 data/liquidations.json 생성.
※ 실제 DART 서버에 접속해 테스트하지 못한 초안입니다. 공시 문구가 다르면 정규식을 조정하세요.
사용: DART_API_KEY=... python scripts/fetch_liquidations.py   (GitHub Actions 등에서 주기 실행)
"""
import os, re, io, json, zipfile, datetime, requests

KEY = os.environ["DART_API_KEY"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "liquidations.json")  # scripts/ 에 두면 저장소 루트의 data/ 에 저장
today = datetime.date.today()
bgn = (today - datetime.timedelta(days=120)).strftime("%Y%m%d")

def dart_list(page):
    r = requests.get("https://opendart.fss.or.kr/api/list.json", params={
        "crtfc_key": KEY, "bgn_de": bgn, "end_de": today.strftime("%Y%m%d"),
        "page_no": page, "page_count": 100}, timeout=30)
    return r.json().get("list", [])

def doc_text(rcept_no):
    r = requests.get("https://opendart.fss.or.kr/api/document.xml",
                     params={"crtfc_key": KEY, "rcept_no": rcept_no}, timeout=30)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    raw = z.read(z.namelist()[0]).decode("utf-8", "ignore")
    return re.sub(r"<[^>]+>", " ", raw)

def date_after(label, text):
    m = re.search(label + r"[^0-9]{0,20}(\d{4})[.\-년 ]+(\d{1,2})[.\-월 ]+(\d{1,2})", text)
    return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}" if m else None

def main():
    items = {}
    for page in range(1, 6):
        for d in dart_list(page):
            nm, rpt = d.get("corp_name", ""), d.get("report_nm", "")
            if "스팩" not in nm or "청산" not in rpt:
                continue
            try:
                t = doc_text(d["rcept_no"])
            except Exception:
                continue
            pay = date_after("청산분배금\\s*지급(?:예정)?일", t) or date_after("분배금\\s*지급일", t)
            m = re.search(r"(?:1주당|주당)?\s*청산\s*분배(?:금|단가)[^0-9]{0,30}([\d,]{4,7})\s*원", t)
            if pay and m:
                items[nm] = {"name": nm, "pay_date": pay, "unit_price": int(m[1].replace(",", ""))}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"updated_at": today.isoformat(), "items": list(items.values())},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(len(items), "건 저장")

if __name__ == "__main__":
    main()
