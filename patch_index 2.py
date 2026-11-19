"""index.html 자동 수정 스크립트.
사용: python patch_index.py index.html [출력파일]   (출력 생략 시 같은 파일을 덮어씀)
"""
# (계좌, 종목명, 매수일, 매수량, 정산금액)  ※ 270-50=일반, 270-77=ISA (사용자 확인)
G='일반'; I='ISA'
RECORDS = [
# --- 일반 (270-50) 30건 ---
(G,'유진스팩11호','2026-02-24',1024,2119750),
(G,'유진스팩10호','2026-02-23',16,33840),
(G,'유진스팩11호','2026-02-25',3806,7878700),
(G,'에스케이증권제12호스팩','2026-02-23',61,125965),
(G,'에스케이증권제13호스팩','2026-02-23',4854,9999600),
(G,'하나33호스팩','2026-02-23',2819,5973990),
(G,'에스케이증권제12호스팩','2026-02-24',4784,9879310),
(G,'미래에셋비전스팩4호','2026-02-23',4761,9998460),
(G,'신한제14호스팩','2026-02-23',4728,9994920),
(G,'디비금융제13호스팩','2026-02-24',484,1014010),
(G,'미래에셋비전스팩7호','2026-02-23',3196,6552030),
(G,'유진스팩10호','2026-02-24',4680,9968760),
(G,'한화플러스제5호스팩','2026-02-23',4878,10000260),
(G,'하나34호스팩','2026-02-23',4583,9532980),
(G,'디비금융제13호스팩','2026-02-23',4256,8916640),
(G,'키움제11호스팩','2026-02-23',4583,9670480),
(G,'신한제15호스팩','2026-02-23',4830,9998460),
(G,'이베스트스팩6호','2026-02-24',4914,10000350),
(G,'키움제10호스팩','2026-02-23',4750,9998230),
(G,'미래에셋비전스팩6호','2026-02-23',4854,9999600),
(G,'KB제31호스팩','2026-02-24',341,707595),
(G,'하나33호스팩','2026-02-25',1882,3999390),
(G,'미래에셋비전스팩7호','2026-02-25',213,436660),
(G,'하나34호스팩','2026-02-25',210,437860),
(G,'KB제31호스팩','2026-02-25',4481,9298405),
(G,'유안타제17호스팩','2026-02-25',4914,10000350),
(G,'키움제11호스팩','2026-02-26',5,10500),
(G,'미래에셋비전스팩7호','2026-03-03',20,41000),
(G,'하나33호스팩','2026-07-22',92,189520),
(G,'신한제16호스팩','2026-09-29',1580,3160110),
# --- ISA (270-77) 48건 ---
(I,'한국제15호스팩','2026-07-20',390,801010),
(I,'유안타제15호스팩','2026-02-12',1027,2119610),
(I,'신한제13호스팩','2026-03-24',137,292360),
(I,'유안타제16호스팩','2026-03-26',1097,2253100),
(I,'에이치엠씨제7호스팩','2026-03-26',963,1963405),
(I,'신한제14호스팩','2026-04-17',1229,2518055),
(I,'유안타제17호스팩','2026-05-04',533,1073350),
(I,'유안타제17호스팩','2026-05-07',16,32220),
(I,'유안타제17호스팩','2026-05-12',486,978700),
(I,'유진스팩11호','2026-05-15',1,2030),
(I,'키움제11호스팩','2026-05-18',1135,2302715),
(I,'키움제10호스팩','2026-05-19',1580,3189720),
(I,'신한제15호스팩','2026-05-19',1222,2503710),
(I,'디비금융제13호스팩','2026-05-19',1225,2503720),
(I,'하나34호스팩','2026-05-27',1228,2503700),
(I,'KB제30호스팩','2026-05-29',1075,2202525),
(I,'유진스팩11호','2026-05-29',10,20380),
(I,'교보17호스팩','2026-05-29',535,1112215),
(I,'유진스팩11호','2026-06-01',1018,2095940),
(I,'교보17호스팩','2026-06-01',473,998835),
(I,'한국제15호스팩','2026-06-26',585,1201510),
(I,'KB제31호스팩','2026-07-14',992,2002660),
(I,'교보16호스팩','2026-06-26',592,1201060),
(I,'KB제30호스팩','2026-06-26',617,1254870),
(I,'엔에이치스팩31호','2026-07-16',582,1209930),
(I,'대신밸런스제19호스팩','2026-07-16',973,2003295),
(I,'신한제13호스팩','2026-07-20',760,1583790),
(I,'엔에이치스팩31호','2026-07-20',387,800665),
(I,'에스케이증권제13호스팩','2026-07-22',499,1017385),
(I,'에스케이증권제13호스팩','2026-08-05',552,1122670),
(I,'교보16호스팩','2026-08-05',390,801010),
(I,'유진스팩11호','2026-08-11',990,2003580),
(I,'교보17호스팩','2026-08-11',987,2002455),
(I,'디비금융제13호스팩','2026-08-11',207,419965),
(I,'신한제14호스팩','2026-08-12',818,1651380),
(I,'디비금융제13호스팩','2026-08-12',544,1100950),
(I,'신한제15호스팩','2026-08-13',762,1542150),
(I,'KB제30호스팩','2026-08-21',53,107790),
(I,'유안타제17호스팩','2026-08-21',510,1027030),
(I,'유안타제17호스팩','2026-08-24',861,1742500),
(I,'대신밸런스제19호스팩','2026-09-30',809,1677790),
(I,'하나34호스팩','2026-09-30',92,186650),
(I,'키움제10호스팩','2026-09-30',880,1803000),
(I,'신한제15호스팩','2026-09-30',491,1000965),
(I,'신한제14호스팩','2026-10-06',418,850140),
(I,'교보17호스팩','2026-10-06',438,901790),
(I,'하나34호스팩','2026-10-07',144,294310),
(I,'KB제31호스팩','2026-10-13',1019,2072480),
]

import re, json
import sys
SRC = sys.argv[1] if len(sys.argv) > 1 else 'index.html'
DST = sys.argv[2] if len(sys.argv) > 2 else SRC
s = open(SRC, encoding='utf-8').read()
if 'SEED_HOLDINGS' in s:
    sys.exit('이미 수정된 index.html 입니다. 원본 파일에 실행하세요.')
def rep(old, new, count=1):
    global s
    assert s.count(old) >= 1, old[:60]
    if count == 0: s = s.replace(old, new)
    else:
        assert s.count(old) == count, (old[:60], s.count(old))
        s = s.replace(old, new)

# (c) date input CSS
rep('''        input[type="date"] {
            height: 42px !important;
            padding-top: 0 !important;
            padding-bottom: 0 !important;
            box-sizing: border-box;
        }''','''        input[type="date"] {
            -webkit-appearance: none;
            appearance: none;
            display: block;
            width: 100%;
            min-width: 0;
            height: 42px !important;
            min-height: 42px;
            max-height: 42px;
            line-height: 42px;
            padding: 0 14px !important;
            margin: 0;
            box-sizing: border-box;
            background-color: #fff;
            text-align: left;
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
        #toast-auto { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 60; }''')

# (d) recommendation card labels
s = re.sub(r'<span>수익률\((?:ISA|일반)\)</span>', '<span>수익률</span>', s)
rep('<span id="yield-header-col">예상수익률 (ISA)</span>', '<span id="yield-header-col">예상수익률</span>')
rep('''                yieldHeader.innerText = accountVal === 'isa' ? '예상수익률 (ISA)' : '예상수익률 (일반)';''','''                yieldHeader.innerText = '예상수익률';''')

# (e) holdings form: account type ISA/일반
rep('''                                <select id="hold-input-account" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                                    <option value="남편" selected>남편</option>
                                    <option value="아내">아내</option>
                                </select>''','''                                <select id="hold-input-account" class="w-full h-[42px] px-3.5 text-sm border border-slate-300 rounded-xl bg-white font-medium text-slate-700">
                                    <option value="ISA" selected>ISA</option>
                                    <option value="일반">일반</option>
                                </select>''')
rep('''                            <option value="남편">남편</option>
                            <option value="아내">아내</option>
                        </select>''','''                            <option value="ISA">ISA</option>
                            <option value="일반">일반</option>
                        </select>''')
# remove 매수일 column header
s = re.sub(r'\n\s*<th onclick="sortHoldingData\(\'buy_date\'\)".*?</th>', '', s, count=1)
rep('''<td class="py-4 px-4 text-slate-600">${item.buy_date}</td>
                    <td class="py-4 px-4 text-slate-700 font-semibold">${item.buy_amount''','''<td class="py-4 px-4 text-slate-700 font-semibold">${item.buy_amount''')
rep("const columns = ['account', 'name', 'buy_date', 'buy_amount'","const columns = ['account', 'name', 'buy_amount'")
rep("(column === 'account' || column === 'name' || column === 'buy_date' || column === 'spacStatus')","(column === 'account' || column === 'name' || column === 'spacStatus')")
rep("let holdingSortColumn = 'buy_date';","let holdingSortColumn = 'name';")
rep("let holdingSortDirection = 'desc';","let holdingSortDirection = 'asc';")
rep('colspan="8" class="py-12 text-center text-slate-400 font-normal">등록된','colspan="7" class="py-12 text-center text-slate-400 font-normal">등록된')
rep('colspan="8" class="py-12 text-center text-slate-400 font-normal">조건에 일치하는 보유','colspan="7" class="py-12 text-center text-slate-400 font-normal">조건에 일치하는 보유')
rep("th onclick=\"sortHoldingData('account')\"", "th onclick=\"sortHoldingData('acct')\"")
rep('id="hold-sort-icon-account"','id="hold-sort-icon-acct"')
rep("const columns = ['account', 'name', 'buy_amount'","const columns = ['acct', 'name', 'buy_amount'")
rep("(column === 'account' || column === 'name' || column === 'spacStatus')","(column === 'acct' || column === 'name' || column === 'spacStatus')")
# render: acct
rep('''                const accountOwner = item.account || '남편';
''','''                const accountOwner = item.account || '남편';
                const acct = item.acct || '일반';
''')
rep("const matchedSpac = allItems.find(s => s.name === item.name);","const matchedSpac = findSpacByName(item.name);")
rep("return { ...item, account: accountOwner, origIndex","return { ...item, account: accountOwner, acct, origIndex")
rep("if (filterAccount !== 'all' && item.account !== filterAccount) return false;","if (filterAccount !== 'all' && item.acct !== filterAccount) return false;")
rep('''                const accountBadge = item.account === '아내'
                    ? `<span class="bg-pink-50 text-pink-700 text-xs px-2 py-0.5 rounded font-semibold">아내</span>`
                    : `<span class="bg-sky-50 text-sky-700 text-xs px-2 py-0.5 rounded font-semibold">남편</span>`;''','''                const accountBadge = item.acct === 'ISA'
                    ? `<span class="bg-indigo-50 text-indigo-700 text-xs px-2 py-0.5 rounded font-semibold">ISA</span>`
                    : `<span class="bg-slate-100 text-slate-700 text-xs px-2 py-0.5 rounded font-semibold">일반</span>`;''')
# add item
rep('''                account: document.getElementById('hold-input-account').value,
                name:''','''                account: '남편',
                acct: document.getElementById('hold-input-account').value,
                name:''')
# sell modal
rep("`[${item.account || '남편'} - ${item.name}]","`[${item.acct || '일반'} - ${item.name}]")
rep('''                buy_date: item.buy_date,
                sell_amount: Number(document.getElementById('sell-modal-amount').value),
                sell_date: document.getElementById('sell-modal-date').value
            };''','''                buy_date: item.buy_date,
                sell_amount: Number(document.getElementById('sell-modal-amount').value),
                sell_date: document.getElementById('sell-modal-date').value,
                acct: item.acct || '일반'
            };''')
# performance: show acct under name
rep('''<td class="py-4 px-5 font-bold text-slate-900">${item.name}</td>
                    <td class="py-4 px-4">${typeBadge}</td>''','''<td class="py-4 px-5 font-bold text-slate-900">${item.name}${item.acct ? ` <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded ${item.acct === 'ISA' ? 'bg-indigo-50 text-indigo-700' : 'bg-slate-100 text-slate-600'}">${item.acct}</span>` : ''}${item.tax ? `<div class="text-[10px] font-normal text-slate-400">세금 ${item.tax.toLocaleString()}원 차감</div>` : ''}</td>
                    <td class="py-4 px-4">${typeBadge}</td>''')

# toast div
rep('<script>\n        let allItems = [];','<div id="toast-auto" class="hidden bg-slate-900 text-white text-sm font-semibold px-4 py-3 rounded-xl shadow-lg"></div>\n    <script>\n        let allItems = [];')

# seed + helpers + auto-liquidation
seed = json.dumps([{"account":"남편","acct":a,"name":n,"buy_amount":amt,"quantity":q,"buy_date":d} for a,n,d,q,amt in RECORDS], ensure_ascii=False)
helpers = '''
        // =========================================================
        // 최초 1회 반영되는 매수 내역 (남편 / 270-77=ISA, 270-50=일반) - 재접속 시 중복 추가 안 됨
        // =========================================================
        const SEED_HOLDINGS = ''' + seed + ''';
        const SEED_KEY = 'spac_seed_applied_v1';
        const GENERAL_TAX_RATE = 0.154;

        function normName(n) {
            return String(n || '').replace(/\\s+/g, '').replace(/^KB/i, '케이비').replace(/^IBKS/i, '아이비케이에스').toLowerCase();
        }
        function findSpacByName(name) {
            const k = normName(name);
            return allItems.find(s => s.name === name) || allItems.find(s => normName(s.name) === k);
        }
        function saveHoldings() { localStorage.setItem('spac_holding_items_v5', JSON.stringify(holdingItems)); }
        function savePerformance() { localStorage.setItem('spac_performance_items_v3', JSON.stringify(performanceItems)); }

        function applySeedHoldings() {
            try { if (localStorage.getItem(SEED_KEY)) return; } catch (e) { return; }
            holdingItems = holdingItems.concat(SEED_HOLDINGS.map(r => ({ ...r })));
            saveHoldings();
            localStorage.setItem(SEED_KEY, new Date().toISOString());
        }

        function showToast(msg) {
            const el = document.getElementById('toast-auto');
            if (!el) return;
            el.innerText = msg;
            el.classList.remove('hidden');
            setTimeout(() => el.classList.add('hidden'), 6000);
        }

        // 청산 공시(data/liquidations.json)의 청산분배금 지급일/분배단가로 보유 -> 성과관리 자동 이관
        // ISA: 세금 없음 / 일반: 매매차익(분배금-매수금)의 15.4% 차감
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
                if (l && l.name && l.pay_date && Number(l.unit_price) > 0) liqMap[normName(l.name)] = l;
            });

            let moved = 0;
            const remain = [];
            holdingItems.forEach(item => {
                const l = liqMap[normName(item.name)];
                if (!l || l.pay_date > today || !(item.quantity > 0)) { remain.push(item); return; }
                const acct = item.acct || '일반';
                const gross = Math.round(item.quantity * Number(l.unit_price));
                const gain = gross - item.buy_amount;
                const tax = acct === '일반' ? Math.floor(Math.max(0, gain) * GENERAL_TAX_RATE) : 0;
                const rec = {
                    name: item.name, type: '청산', buy_amount: item.buy_amount, buy_date: item.buy_date,
                    sell_amount: gross - tax, sell_date: l.pay_date, acct, auto: true
                };
                if (tax > 0) rec.tax = tax;
                performanceItems.unshift(rec);
                moved++;
            });
            if (moved > 0) {
                holdingItems = remain;
                saveHoldings();
                savePerformance();
                showToast(`청산 완료 ${moved}건이 성과 관리로 자동 이관되었습니다.`);
                renderHoldings();
                renderPerformance();
            }
        }
'''
rep('        function getTodayStr() {', helpers + '\n        function getTodayStr() {')

# init: after perf load
marker = """            }
        }

        // =========================================================
        // 종목 분석 탭 필터링"""
assert s.count(marker)==1
s = s.replace(marker, """            }

            applySeedHoldings();
            await processLiquidations();
        }

        // =========================================================
        // 종목 분석 탭 필터링""")
open(DST,'w',encoding='utf-8').write(s)
print('ok', len(s))
