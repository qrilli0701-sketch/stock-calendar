"""데일리 브리핑: 간밤 시장, 심리 지표, 어제 일정의 결과, 이슈(급등락·공시), 오늘 일정을 HTML 한 장으로.

  python daily.py build [--date 2026-10-23]   # docs/daily/<날짜>/ 에 data.json, index.html, card.png 생성
  python daily.py send  [--date 2026-10-23]   # 텔레그램 발송

주간 캘린더(calendar_bot.py)의 data.json에서 일정을 가져오고, 어제 일정의 결과를 주간 페이지에도 써넣는다.
"""
import argparse
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta
from urllib.parse import quote

import requests

import calendar_bot as cb
from calendar_bot import KST, UA, DOCS, PAGES_URL, FLAG, WEEKDAYS, esc, env, get_retry, finnhub_get

DAILY = DOCS / "daily"
BRIEF_AT = dtime(7, 30)
MOVE_PCT = 3.0  # 상위 30 중 이 이상 움직이면 이슈
NAME_TO_SYMBOL = {v: k for k, v in cb.US_POOL.items()}
FG_KO = {"extreme fear": "극단적 공포", "fear": "공포", "neutral": "중립",
         "greed": "탐욕", "extreme greed": "극단적 탐욕"}
FG_PARTS = {
    "market_momentum_sp500": "S&P500 모멘텀", "stock_price_strength": "52주 신고가·신저가",
    "stock_price_breadth": "거래량 폭", "put_call_options": "풋/콜 비율",
    "market_volatility_vix": "변동성(VIX)", "junk_bond_demand": "정크본드 수요",
    "safe_haven_demand": "안전자산 수요",
}
# 이슈로 볼 DART 공시 유형
DISCLOSURE_KEYS = ("자기주식", "유상증자", "무상증자", "단일판매", "공급계약", "합병", "분할",
                   "잠정", "현금ㆍ현물배당", "최대주주", "타법인주식", "소송", "영업정지", "감자")
# 경제지표 결과: FRED release_id → [(시리즈, 이름, 변환)]  변환: yoy(전년비 %), mom(전월비 %), diff(전월 대비 증감), level
FRED_RESULTS = {
    10: [("CPIAUCSL", "CPI", "yoy"), ("CPILFESL", "근원 CPI", "yoy")],
    50: [("PAYEMS", "비농업 고용(천명)", "diff"), ("UNRATE", "실업률", "level")],
    53: [("A191RL1Q225SBEA", "GDP 성장률(연율)", "level")],
    54: [("PCEPI", "PCE", "yoy"), ("PCEPILFE", "근원 PCE", "yoy")],
    46: [("PPIFIS", "PPI", "yoy")],
    9: [("RSAFS", "소매판매", "mom")],
    192: [("JTSJOL", "구인건수(천건)", "level")],
    91: [("UMCSENT", "소비자심리", "level")],
}


# ---------------------------------------------------------------- 시장·지표

def pct(v):
    return float(str(v).replace(",", "").replace("+", ""))


SPARK_N = 22  # 스파크라인: 최근 약 1개월(거래일)


def naver_closes(kind, code):
    """kind: foreign/index, domestic/index, domestic/item, foreign/item → [(날짜, 종가)] 오래된 순."""
    d = get_retry(f"https://api.stock.naver.com/chart/{kind}/{code}", params={"periodType": "dayCandle"},
                  headers=UA, tries=2, timeout=20).json()
    return [(p["localDate"], float(p["closePrice"])) for p in d.get("priceInfos", [])]


_US_CODE = {}


def us_closes(sym):
    """네이버 미국 종목 코드: 나스닥은 '.O', 뉴욕거래소는 티커 그대로 또는 '.K' (BRK.B → BRKb)."""
    cands = [_US_CODE[sym]] if sym in _US_CODE else (["BRKb"] if sym == "BRK.B" else [sym + ".O", sym, sym + ".K"])
    for c in cands:
        try:
            rows = naver_closes("foreign/item", c)
        except requests.RequestException:
            continue
        if rows:
            _US_CODE[sym] = c
            return rows
    return []


def index_tile(kind, code, name, url):
    rows = naver_closes(kind, code)
    (at, last), (_, prev) = rows[-1], rows[-2]
    return {"name": name, "value": f"{last:,.2f}", "chg": round((last / prev - 1) * 100, 2),
            "at": f"{at[:4]}-{at[4:6]}-{at[6:]}", "spark": [v for _, v in rows[-SPARK_N:]], "url": url}


def kr_flow(code):
    i = get_retry(f"https://m.stock.naver.com/api/index/{code}/integration", headers=UA).json()
    f = i.get("dealTrendInfo") or {}
    return {"개인": f.get("personalValue"), "외국인": f.get("foreignValue"), "기관": f.get("institutionalValue")}


def market_tile(path, name, prefix="", suffix="", digits=2):
    rows = get_retry(f"https://api.stock.naver.com/marketindex/{path}/prices",
                     params={"page": 1, "pageSize": SPARK_N}, headers=UA).json()
    vals = [pct(r["closePrice"]) for r in reversed(rows)]
    return {"name": name, "value": f"{prefix}{vals[-1]:,.{digits}f}{suffix}",
            "chg": round((vals[-1] / vals[-2] - 1) * 100, 2), "at": rows[0]["localTradedAt"][:10],
            "spark": vals, "url": f"https://m.stock.naver.com/marketindex/{path.split('/')[0]}/{path.split('/')[1]}"}


def markets():
    nv = "https://m.stock.naver.com"
    return {
        "us": [index_tile("foreign/index", ".INX", "S&P500", f"{nv}/worldstock/index/.INX"),
               index_tile("foreign/index", ".IXIC", "나스닥", f"{nv}/worldstock/index/.IXIC"),
               index_tile("foreign/index", ".DJI", "다우", f"{nv}/worldstock/index/.DJI"),
               index_tile("foreign/index", ".VIX", "VIX", f"{nv}/worldstock/index/.VIX")],
        "kr": [dict(index_tile("domestic/index", "KOSPI", "코스피", f"{nv}/domestic/index/KOSPI"), flow=kr_flow("KOSPI")),
               dict(index_tile("domestic/index", "KOSDAQ", "코스닥", f"{nv}/domestic/index/KOSDAQ"), flow=kr_flow("KOSDAQ"))],
        "fx": [market_tile("exchange/FX_USDKRW", "원/달러"),
               index_tile("foreign/index", ".DXY", "달러인덱스", f"{nv}/worldstock/index/.DXY"),
               market_tile("exchange/FX_JPYKRW", "원/100엔"),
               market_tile("exchange/FX_CNYKRW", "원/위안"),
               market_tile("exchange/FX_EURKRW", "원/유로")],
        "rates": [market_tile("bond/US10YT=RR", "미 10년물", suffix="%"),
                  market_tile("energy/CLcv1", "WTI", prefix="$"),
                  market_tile("metals/GCcv1", "금", prefix="$", digits=0)],
        "crypto": [crypto_tile("BTC-USD", "비트코인"), crypto_tile("ETH-USD", "이더리움")],
    }


def crypto_tile(product, name):
    """코인베이스 일봉 (UTC 기준, 최신이 맨 앞): [시각, 저가, 고가, 시가, 종가, 거래량]."""
    rows = get_retry(f"https://api.exchange.coinbase.com/products/{product}/candles",
                     params={"granularity": 86400}, headers=UA, tries=2).json()
    vals = [r[4] for r in reversed(rows[:SPARK_N])]
    at = datetime.fromtimestamp(rows[0][0], KST).strftime("%Y-%m-%d")
    return {"name": name, "value": f"${vals[-1]:,.0f}", "chg": round((vals[-1] / vals[-2] - 1) * 100, 2),
            "at": at, "spark": vals, "url": f"https://www.coinbase.com/price/{product.split('-')[0].lower()}"}


US_SECTORS = [("기술", "XLK"), ("반도체", "SMH"), ("통신서비스", "XLC"), ("경기소비재", "XLY"),
              ("금융", "XLF"), ("헬스케어", "XLV"), ("산업재", "XLI"), ("에너지", "XLE"), ("소재", "XLB"),
              ("필수소비재", "XLP"), ("유틸리티", "XLU"), ("부동산", "XLRE")]
KR_SECTORS = [("반도체", "091160"), ("2차전지", "305720"), ("자동차", "091180"), ("조선", "494670"),
              ("방산", "449450"), ("바이오", "244580"), ("인터넷", "365000"), ("은행", "091170"),
              ("증권", "102970"), ("철강", "117680"), ("화학", "117460"), ("건설", "117700")]


def sector_row(name, code, closes, url):
    vals = [v for _, v in closes]
    month = vals[-SPARK_N] if len(vals) >= SPARK_N else vals[0]
    return {"name": name, "code": code, "chg": round((vals[-1] / vals[-2] - 1) * 100, 2),
            "chg_1m": round((vals[-1] / month - 1) * 100, 1), "spark": vals[-SPARK_N:], "url": url}


def sectors():
    """업종 대표 ETF로 본 섹터 흐름 (1일·1개월 등락)."""
    us, kr = [], []
    for name, sym in US_SECTORS:
        rows = us_closes(sym)
        if len(rows) >= 2:
            us.append(sector_row(name, sym, rows, f"https://m.stock.naver.com/worldstock/etf/{_US_CODE.get(sym, sym)}"))
    for name, code in KR_SECTORS:
        rows = naver_closes("domestic/item", code)
        if len(rows) >= 2:
            kr.append(sector_row(name, code, rows, f"https://m.stock.naver.com/domestic/stock/{code}"))
    return {"us": sorted(us, key=lambda r: -r["chg"]), "kr": sorted(kr, key=lambda r: -r["chg"])}


def fear_greed():
    out = {}
    d = get_retry("https://production.dataviz.cnn.io/index/fearandgreed/graphdata", tries=2, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130 Safari/537.36",
        "Referer": "https://edition.cnn.com/"}).json()
    f = d["fear_and_greed"]
    hist = [p["y"] for p in d.get("fear_and_greed_historical", {}).get("data", [])][-SPARK_N:]
    out["cnn"] = {"score": round(f["score"]), "rating": FG_KO.get(f["rating"], f["rating"]),
                  "prev_close": round(f["previous_close"]), "prev_week": round(f["previous_1_week"]),
                  "prev_month": round(f["previous_1_month"]), "spark": hist,
                  "parts": [{"name": FG_PARTS[k], "score": round(d[k]["score"]),
                             "rating": FG_KO.get(d[k]["rating"], d[k]["rating"])}
                            for k in FG_PARTS if k in d],
                  "url": "https://edition.cnn.com/markets/fear-and-greed"}
    try:
        c = get_retry(f"https://api.alternative.me/fng/?limit={SPARK_N}", tries=2).json()["data"]
        ko = {"Extreme Fear": "극단적 공포", "Fear": "공포", "Neutral": "중립", "Greed": "탐욕", "Extreme Greed": "극단적 탐욕"}
        out["crypto"] = {"score": int(c[0]["value"]), "rating": ko.get(c[0]["value_classification"], ""),
                         "prev_close": int(c[1]["value"]), "spark": [int(x["value"]) for x in reversed(c)],
                         "url": "https://alternative.me/crypto/fear-and-greed-index/"}
    except Exception as e:
        print(f"[실패] 크립토 공포·탐욕: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- 뉴스

def google_news(query, n=3):
    url = f"https://news.google.com/rss/search?q={quote(query + ' when:1d')}&hl=ko&gl=KR&ceid=KR:ko"
    root = ET.fromstring(get_retry(url, tries=2, headers=UA, timeout=20).content)
    out = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        src = it.findtext("source") or ""
        out.append({"title": re.sub(rf"\s*-\s*{re.escape(src)}$", "", title) if src else title,
                    "source": src, "url": it.findtext("link")})
        if len(out) >= n:
            break
    return out


def us_news(sym, name_en, since, n=3):
    keyword = (name_en or "").split()[0].lower() if name_en else sym.lower()
    items = finnhub_get("company-news", symbol=sym, **{"from": since.isoformat(), "to": date.today().isoformat()})
    out = []
    for x in items if isinstance(items, list) else []:
        h = x.get("headline", "")
        if keyword in h.lower() or sym.lower() in h.lower().split():
            out.append({"title": h, "source": x.get("source", ""), "url": x.get("url", "")})
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- 이슈

def us_movers(ranking, since):
    rows = []
    for r in ranking[:cb.TOP_N]:
        closes = us_closes(r["symbol"])
        if len(closes) < 2:
            continue
        last, prev = closes[-1][1], closes[-2][1]
        rows.append({"symbol": r["symbol"], "name": r["name"], "name_en": r.get("name_en", ""),
                     "price": f"{last:,.2f}", "chg": round((last / prev - 1) * 100, 2),
                     "spark": [v for _, v in closes[-SPARK_N:]],
                     "url": f"https://www.nasdaq.com/market-activity/stocks/{r['symbol'].lower().replace('.', '-')}"})
    for m in rows:
        if abs(m["chg"]) >= MOVE_PCT:
            m["news"] = us_news(m["symbol"], m["name_en"], since)
    return rows


SPARK_TOP = 10  # 카드에 스파크라인으로 보여줄 기업 수 (나라별)


def kr_movers(ranking):
    codes = {r["code"]: r["name"] for r in ranking}
    rows = []
    for market in ("KOSPI", "KOSDAQ"):
        r = get_retry(f"https://m.stock.naver.com/api/stocks/marketValue/{market}",
                      params={"page": 1, "pageSize": 60}, headers=UA)
        for s in r.json()["stocks"]:
            if s["itemCode"] in codes:
                rows.append({"code": s["itemCode"], "name": s["stockName"],
                             "price": s["closePrice"], "chg": pct(s["fluctuationsRatio"]),
                             "url": f"https://m.stock.naver.com/domestic/stock/{s['itemCode']}"})
    rows.sort(key=lambda x: list(codes).index(x["code"]))
    for m in rows[:SPARK_TOP]:
        m["spark"] = [v for _, v in naver_closes("domestic/item", m["code"])[-SPARK_N:]]
    for m in rows:
        if abs(m["chg"]) >= MOVE_PCT:
            m["news"] = google_news(f'"{m["name"]}"')
    return rows


def kr_disclosures(ranking, since, until):
    key = env("OPENDART_API_KEY")
    z = zipfile.ZipFile(io.BytesIO(get_retry(
        "https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}).content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    corp = {c.findtext("stock_code").strip(): c.findtext("corp_code")
            for c in root.iter("list") if (c.findtext("stock_code") or "").strip()}
    out = []
    for r in ranking:
        if r["code"] not in corp:
            continue
        res = get_retry("https://opendart.fss.or.kr/api/list.json", params={
            "crtfc_key": key, "corp_code": corp[r["code"]], "bgn_de": since.strftime("%Y%m%d"),
            "end_de": until.strftime("%Y%m%d"), "page_count": 100}).json()
        for it in res.get("list", []):
            name = re.sub(r"\s+", " ", it["report_nm"]).strip()
            if any(k in name for k in DISCLOSURE_KEYS):
                out.append({"company": r["name"], "title": name, "date": it["rcept_dt"],
                            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"})
    return out


# ---------------------------------------------------------------- 어제 일정의 결과

def approx_dt(item):
    """주간 캘린더 항목의 대략적인 KST 시각 ('새벽', '오후 3시' 같은 표시에서)."""
    d = date.fromisoformat(item["date"])
    t = item.get("time", "")
    part = t.split()[0] if t else ""
    m = re.search(r"(\d+)시(?:\s*(\d+)분)?", t)
    if m:
        h = int(m[1])
        if part in ("오후", "저녁", "밤") and h < 12:
            h += 12
        mi = int(m[2] or 0)
    else:
        h, mi = {"새벽": 4, "오전": 9, "오후": 14, "저녁": 19, "밤": 22}.get(part, 12), 0
    return datetime.combine(d, dtime(h, mi), KST)


def weekly_items(*days):
    items, seen = [], set()
    for d in days:
        p = DOCS / (d - timedelta(days=d.weekday())).isoformat() / "data.json"
        if p.exists() and p not in seen:
            seen.add(p)
            items += [dict(i, _week=p.parent.name) for i in json.loads(p.read_text())["items"] if not i["filtered"]]
    return items


def fred_value(series, how):
    obs = get_retry("https://api.stlouisfed.org/fred/series/observations", params={
        "series_id": series, "api_key": env("FRED_API_KEY"), "file_type": "json",
        "sort_order": "desc", "limit": 14}).json()["observations"]
    vals = [(o["date"], float(o["value"])) for o in obs if o["value"] not in (".", "")]

    def calc(i):
        if how == "yoy":
            return (vals[i][1] / vals[i + 12][1] - 1) * 100
        if how == "mom":
            return (vals[i][1] / vals[i + 1][1] - 1) * 100
        if how == "diff":
            return vals[i][1] - vals[i + 1][1]
        return vals[i][1]
    now, prev = calc(0), calc(1)
    unit = "%" if how in ("yoy", "mom") or series in ("UNRATE", "A191RL1Q225SBEA") else ""
    fmt = (lambda v: f"{v:+,.0f}") if how == "diff" else (lambda v: f"{v:,.1f}{unit}")
    return f"{fmt(now)} (직전 {fmt(prev)}, {vals[0][0][:7]} 기준)"


def results(window_start, window_end, us_earn_cache, disclosures):
    out = []
    for it in weekly_items(window_start.date(), window_end.date()):
        if not window_start < approx_dt(it) <= window_end:
            continue
        r = {"date": it["date"], "title": it["title"], "country": it["country"],
             "time": it["time"], "_week": it["_week"], "result": "", "url": "", "status": "pending"}
        try:
            if it.get("group") and it["country"] == "US":
                sym = it.get("symbol") or NAME_TO_SYMBOL.get(it["group"])
                e = us_earn_cache.get(sym)
                if e and e.get("epsActual") is not None:
                    est, act = e.get("epsEstimate"), e["epsActual"]
                    txt = f"EPS ${act:.2f}"
                    if est:
                        surprise = (act - est) / abs(est) * 100
                        tag = "서프라이즈" if surprise >= 3 else "쇼크" if surprise <= -3 else "부합"
                        txt += f" (예상 ${est:.2f}) {tag} {surprise:+.0f}%"
                        r["status"] = {"서프라이즈": "good", "쇼크": "bad"}.get(tag, "neutral")
                    else:
                        r["status"] = "neutral"
                    if e.get("revenueActual"):
                        txt += f" · 매출 ${e['revenueActual'] / 1e9:.1f}B"
                    r["result"] = txt
                r["url"] = it.get("url", "")
            elif it.get("group") and it["country"] == "KR":
                hit = [d for d in disclosures if d["company"] == it["group"] and "잠정" in d["title"]]
                if hit:
                    r.update(result="잠정실적 공시 · " + hit[0]["title"], url=hit[0]["url"], status="neutral")
            elif "rid=" in (it.get("url") or ""):
                rid = int(it["url"].split("rid=")[1])
                if rid in FRED_RESULTS:
                    r["result"] = " · ".join(f"{name} {fred_value(s, how)}" for s, name, how in FRED_RESULTS[rid])
                    r["status"] = "neutral"
                r["url"] = it["url"]
            if not r["result"]:
                news = google_news(re.sub(r"\(.*?\)", "", it["title"]).strip(), n=1)
                if news:
                    r.update(result="📰 " + news[0]["title"], url=news[0]["url"], status="news")
        except Exception as e:
            print(f"[실패] 결과 {it['title']}: {e}", file=sys.stderr)
        out.append(r)
    return out


def write_back(res):
    """결과를 주간 data.json과 주간 페이지에도 반영."""
    weeks = {r["_week"] for r in res if r["result"]}
    for w in weeks:
        p = DOCS / w / "data.json"
        data = json.loads(p.read_text())
        by_key = {(r["date"], r["title"]): r for r in res if r["result"] and r["_week"] == w}
        for it in data["items"]:
            r = by_key.get((it["date"], it["title"]))
            if r:
                it["result"], it["result_url"] = r["result"], r["url"]
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        (p.parent / "index.html").write_text(cb.render_page(data))


# ---------------------------------------------------------------- 수집

def collect(day):
    now = datetime.combine(day, BRIEF_AT, KST)
    prev_day = day - timedelta(days=2 if day.weekday() == 0 else 1)  # 월요일은 토요일 브리핑 이후부터
    window_start = datetime.combine(prev_day, BRIEF_AT, KST)
    sources = []

    def run(label, fn, *a, default=None):
        try:
            got = fn(*a)
            n = len(got) if hasattr(got, "__len__") else 1
            sources.append({"name": label, "ok": True, "count": n})
            print(f"[ok] {label}: {n}")
            return got
        except Exception as e:
            sources.append({"name": label, "ok": False, "count": 0, "error": str(e)[:300]})
            print(f"[실패] {label}: {e}", file=sys.stderr)
            return default if default is not None else []

    week = DOCS / (day - timedelta(days=day.weekday())).isoformat() / "data.json"
    wdata = json.loads(week.read_text()) if week.exists() else {}
    if not wdata:
        prev_week = DOCS / (day - timedelta(days=day.weekday() + 7)).isoformat() / "data.json"
        wdata = json.loads(prev_week.read_text()) if prev_week.exists() else {}
    us_rank, kr_rank = wdata.get("us_ranking", []), wdata.get("kr_ranking", [])

    mk = run("지수·환율·금리 (네이버)", markets, default={})
    fg = run("공포·탐욕 지수 (CNN·alternative.me)", fear_greed, default={})
    sec = run("섹터 흐름 (업종 ETF·네이버)", sectors, default={})
    us_mv = run("미국 상위30 등락·뉴스 (Finnhub)", us_movers, us_rank, prev_day) if us_rank else []
    kr_mv = run("한국 상위30 등락·뉴스 (네이버·구글뉴스)", kr_movers, kr_rank) if kr_rank else []
    disc = run("한국 주요 공시 (DART)", kr_disclosures, kr_rank, prev_day, day) if kr_rank else []

    def earn_cache():
        d = get_retry("https://finnhub.io/api/v1/calendar/earnings", params={
            "from": (prev_day - timedelta(days=2)).isoformat(), "to": day.isoformat(),
            "token": env("FINNHUB_API_KEY")}).json().get("earningsCalendar", [])
        return {e["symbol"]: e for e in d}
    cache = run("미국 실적 결과 (Finnhub)", earn_cache, default={})
    res = run("어제 일정 결과", results, window_start, now, cache, disc)

    today_items = [i for i in weekly_items(day, day + timedelta(days=1))
                   if now < approx_dt(i) <= now + timedelta(hours=24)]
    today_items.sort(key=approx_dt)
    return {
        "date": day.isoformat(), "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "window": [window_start.isoformat(), now.isoformat()],
        "markets": mk, "fear_greed": fg, "sectors": sec, "us_movers": us_mv, "kr_movers": kr_mv,
        "disclosures": disc, "results": res, "today": today_items, "sources": sources,
        "week": (day - timedelta(days=day.weekday())).isoformat(),
    }


# ---------------------------------------------------------------- 페이지

def chg_html(v, digits=2):
    v = round(v, digits)  # 반올림해서 0이면 보합으로
    cls = "up" if v > 0 else "down" if v < 0 else "flat"
    arrow = "▲" if v > 0 else "▼" if v < 0 else "–"
    return f'<span class="{cls}">{arrow} {abs(v):.{digits}f}%</span>'


def spark(vals, w=100, h=26, color=None):
    """1개월 추이 스파크라인 (인라인 SVG). 색은 기간 등락 방향: 상승 빨강, 하락 파랑."""
    vals = [v for v in vals or [] if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pad = 2
    pts = [(pad + i * (w - 2 * pad) / (len(vals) - 1), pad + (hi - v) / span * (h - 2 * pad)) for i, v in enumerate(vals)]
    c = color or ("var(--up)" if vals[-1] > vals[0] else "var(--down)" if vals[-1] < vals[0] else "var(--sub)")
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    return (f'<svg class="sp" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polyline points="{line}" fill="none" stroke="{c}" stroke-width="1.6" stroke-linejoin="round" '
            f'stroke-linecap="round" vector-effect="non-scaling-stroke"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="{c}"/></svg>')


def tile(r):
    return (f'<a class="tile" href="{esc(r.get("url"))}"><div class="tl">{esc(r["name"])}</div>'
            f'<div class="tv">{esc(r["value"])}</div><div class="tc">{chg_html(r["chg"])}</div>'
            f'{spark(r.get("spark"))}</a>')


def fg_tile(name, fg):
    diff = fg["score"] - fg["prev_close"]
    return (f'<a class="tile" href="{esc(fg["url"])}"><div class="tl">{esc(name)}</div>'
            f'<div class="tv">{fg["score"]} <small>{esc(fg["rating"])}</small></div>'
            f'<div class="tc flat">전일 {fg["prev_close"]} ({diff:+d})</div>'
            f'<div class="mbar"><span style="left:{fg["score"]}%"></span></div>'
            f'<div class="mticks"><span>공포</span><span>탐욕</span></div></a>')


def meter(label, fg):
    s = fg["score"]
    return (f'<div class="fg"><div class="fgh"><span class="fgl">{esc(label)}</span>'
            f'<span class="fgs">{s}</span><span class="fgr">{esc(fg["rating"])}</span>'
            f'<span class="fgp">전일 {fg["prev_close"]}</span></div>'
            f'<div class="bar"><span style="left:{s}%"></span></div>'
            f'<div class="ticks"><span>극단적 공포</span><span>중립</span><span>극단적 탐욕</span></div></div>')


def sector_rows(rows, scale):
    """0을 가운데 둔 양방향 막대: 상승은 오른쪽 빨강, 하락은 왼쪽 파랑."""
    out = []
    for r in rows:
        w = min(abs(r["chg"]) / scale * 50, 50)
        side = "left" if r["chg"] >= 0 else "right"  # 막대가 가운데(50%)에서 시작
        out.append(f'<a class="se" href="{esc(r["url"])}"><span class="sn">{esc(r["name"])}</span>'
                   f'<span class="sb"><i class="{"pos" if r["chg"] >= 0 else "neg"}" style="{side}:50%;width:{w:.1f}%"></i></span>'
                   f'<span class="sc">{chg_html(r["chg"], 1)}</span>{spark(r.get("spark"), w=44, h=14)}</a>')
    return "".join(out)


def company_rows(rows, cur):
    def price(m):
        return f'${m["price"]}' if cur == "USD" else f'{m["price"]}원'
    return "".join(
        f'<a class="co" href="{esc(m["url"])}"><span class="cn">{esc(m["name"])}</span>'
        f'<span class="cp">{esc(price(m))}</span>'
        f'{spark(m.get("spark"), w=60, h=16)}<span class="cc">{chg_html(m["chg"], 1)}</span></a>'
        for m in rows[:SPARK_TOP])


def issue_lines(data, limit=None):
    lines = []
    for m in sorted(data["us_movers"] + data["kr_movers"], key=lambda x: -abs(x["chg"])):
        if abs(m["chg"]) < MOVE_PCT:
            continue
        flag = FLAG["US"] if "symbol" in m else FLAG["KR"]
        head = m.get("news", [{}])[0] if m.get("news") else {}
        lines.append({"flag": flag, "title": m["name"], "chg": m["chg"],
                      "sub": head.get("title", ""), "url": head.get("url") or m["url"], "news": m.get("news", [])})
    for d in data["disclosures"]:
        lines.append({"flag": FLAG["KR"], "title": d["company"], "chg": None,
                      "sub": "📢 " + d["title"], "url": d["url"], "news": []})
    return lines[:limit] if limit else lines


STATUS_ICON = {"good": "✅", "bad": "⚠️", "neutral": "☑️", "news": "📰", "pending": "⏳"}


def render_card(data):
    day = date.fromisoformat(data["date"])
    mk, fg = data["markets"] or {}, data["fear_greed"] or {}
    us, kr = mk.get("us", []), mk.get("kr", [])
    us_tiles = [tile(r) for r in us] + ([fg_tile("CNN 공포·탐욕", fg["cnn"])] if fg.get("cnn") else [])
    flow_rows = "".join(
        f'<div class="fr"><b>{esc(k["name"])}</b> 외국인 {esc(k["flow"]["외국인"])} · 기관 {esc(k["flow"]["기관"])}'
        f' · 개인 {esc(k["flow"]["개인"])}</div>' for k in kr if (k.get("flow") or {}).get("외국인"))
    kr_tiles = [tile(r) for r in kr] + (
        [f'<div class="tile flowt" style="grid-column:span 3"><div class="tl">순매수 (억원)</div>{flow_rows}</div>']
        if flow_rows else [])
    crypto_tiles = [tile(r) for r in mk.get("crypto", [])] + (
        [fg_tile("공포·탐욕", fg["crypto"])] if fg.get("crypto") else [])

    def group(label, tiles, cols):
        return (f'<div class="mg" style="flex:{cols}"><div class="mgl">{label}</div>'
                f'<div class="grid" style="grid-template-columns:repeat({cols},minmax(0,1fr))">{"".join(tiles)}</div></div>')
    grid = "".join(f'<div class="mrow">{"".join(g)}</div>' for g in [
        [group(f'{FLAG["US"]} 미국 증시', us_tiles, 5)],
        [group(f'{FLAG["KR"]} 한국 증시', kr_tiles, 5)],
        [group("💱 환율", [tile(r) for r in mk.get("fx", [])], 5)],
        [group("📈 금리·원자재", [tile(r) for r in mk.get("rates", [])], 3),
         group("🪙 크립토", crypto_tiles, 3)],
    ])
    asof = " · ".join(x for x in (f'미국 {us[0]["at"][5:].replace("-", "/")}' if us else "",
                                  f'한국 {kr[0]["at"][5:].replace("-", "/")}' if kr else "") if x)

    def one(flag, title, extra="", sub=""):
        return (f'<div class="ln"><span class="flag">{flag}</span><b>{esc(title)}</b>{extra}'
                + (f'<span class="ls">{esc(sub)}</span>' if sub else "") + "</div>")

    res = "".join(one(FLAG.get(r["country"], "🌐"), r["title"],
                      sub=f'{STATUS_ICON[r["status"]]} {r["result"].replace("📰 ", "") or "결과 집계 전"}')
                  for r in data["results"]) or '<div class="ln muted">어제 예정된 큰 일정이 없었어요</div>'
    iss = "".join(one(i["flag"], i["title"], f' {chg_html(i["chg"], 1)}' if i["chg"] is not None else "", i["sub"])
                  for i in issue_lines(data, limit=5)) or \
        f'<div class="ln muted">상위 {cb.TOP_N} 중 ±{MOVE_PCT:.0f}% 넘게 움직인 종목이 없어요</div>'
    today = "".join(one(FLAG.get(i["country"], "🌐"), i["title"],
                        f'<span class="t">{esc(i["time"])}</span>' + ('<span class="badge">★ 핵심</span>' if i.get("key") else ""))
                    for i in data["today"]) or '<div class="ln muted">오늘은 큰 일정이 없어요</div>'

    sec = data.get("sectors") or {}
    sector_blk = ""
    if sec.get("us") or sec.get("kr"):
        def scale(rows):  # 막대 길이는 나라별 최대 등락 기준
            return max([abs(r["chg"]) for r in rows] + [1.0])
        sector_blk = (f'<div class="blk"><div class="bt">섹터 흐름 <span class="at">업종 대표 ETF · 1일 등락순 · 막대 길이는 나라별 기준 · 1개월 추이</span></div>'
                      f'<div class="cos"><div><div class="ch">{FLAG["US"]} 미국</div>{sector_rows(sec.get("us", []), scale(sec.get("us", [])))}</div>'
                      f'<div><div class="ch">{FLAG["KR"]} 한국</div>{sector_rows(sec.get("kr", []), scale(sec.get("kr", [])))}</div></div></div>')

    return f'''<div id="card">
<div class="head"><h1>데일리 브리핑</h1><div class="range">{day.month}.{day.day} ({WEEKDAYS[day.weekday()]})</div></div>
<div class="blk"><div class="bt">시장 <span class="at">{esc(asof)} 마감 · 1개월 추이</span></div>{grid}</div>
{sector_blk}
<div class="blk"><div class="bt">주요 기업 <span class="at">시총 상위 {SPARK_TOP} · 1개월 추이</span></div>
<div class="cos"><div><div class="ch">{FLAG["US"]} 미국</div>{company_rows(data["us_movers"], "USD")}</div>
<div><div class="ch">{FLAG["KR"]} 한국</div>{company_rows(data["kr_movers"], "KRW")}</div></div></div>
<div class="blk"><div class="bt">어제 일정 결과</div>{res}</div>
<div class="blk"><div class="bt">오늘의 이슈</div>{iss}</div>
<div class="blk"><div class="bt">오늘 일정</div>{today}</div>
<div class="foot"><span>한국 시간 기준 · 투자 권유 아님</span><span>스파크라인 색: 1개월 상승 빨강 · 하락 파랑</span></div>
</div>'''


def render(data):
    day = date.fromisoformat(data["date"])
    fg = data["fear_greed"] or {}
    status_icon = STATUS_ICON
    card = render_card(data)
    fg_html = (meter("CNN 공포·탐욕", fg["cnn"]) if fg.get("cnn") else "") + \
              (meter("크립토 공포·탐욕", fg["crypto"]) if fg.get("crypto") else "")

    # 상세
    def news_list(news):
        return "".join(f'<li><a href="{esc(n["url"])}" target="_blank" rel="noopener">{esc(n["title"])}</a>'
                       f' <span class="muted">{esc(n["source"])}</span></li>' for n in news)
    res_detail = "".join(
        f'<div class="row"><span class="flag">{FLAG.get(r["country"], "🌐")}</span><div class="rb">'
        f'<div class="rt">{esc(r["title"])} <span class="t">{esc(r["date"][5:])} {esc(r["time"])}</span></div>'
        f'<div class="rd">{status_icon[r["status"]]} {esc(r["result"] or "결과 집계 전")}</div></div>'
        + (f'<a class="src" href="{esc(r["url"])}" target="_blank" rel="noopener">원문 ↗</a>' if r["url"] else "") + "</div>"
        for r in data["results"]) or '<p class="muted">없음</p>'
    iss_detail = "".join(
        f'<div class="row"><span class="flag">{i["flag"]}</span><div class="rb"><div class="rt">{esc(i["title"])}'
        + (f' {chg_html(i["chg"])}' if i["chg"] is not None else "") + "</div>"
        + (f'<ul>{news_list(i["news"])}</ul>' if i["news"] else (f'<div class="rd">{esc(i["sub"])}</div>' if i["sub"] else ""))
        + f'</div><a class="src" href="{esc(i["url"])}" target="_blank" rel="noopener">원문 ↗</a></div>'
        for i in issue_lines(data)) or '<p class="muted">없음</p>'

    def mover_table(rows, cur):
        return "".join(f'<tr><td>{n + 1}</td><td><a href="{esc(m["url"])}">{esc(m["name"])}</a></td>'
                       f'<td>{cur}{esc(m["price"])}</td><td>{chg_html(m["chg"])}</td></tr>' for n, m in enumerate(rows))
    parts = "".join(f'<tr><td>{esc(p["name"])}</td><td>{p["score"]}</td><td>{esc(p["rating"])}</td></tr>'
                    for p in fg.get("cnn", {}).get("parts", []))
    cnn = fg.get("cnn", {})
    src_rows = "".join(
        f'<tr><td>{esc(s["name"])}</td><td>{"✅" if s["ok"] else "❌"}</td><td>{s["count"]}</td>'
        f'<td class="err">{esc(s.get("error", ""))}</td></tr>' for s in data["sources"])

    css = CSS
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>데일리 브리핑 {day.month}.{day.day}</title>
<link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" rel="stylesheet">
<style>{css}</style></head><body>
{card}
<main>
<h3>시장 심리</h3><div class="blk">{fg_html or "<p class=muted>불러오지 못했어요</p>"}</div>
<h3>어제 일정 결과</h3>{res_detail}
<h3>오늘의 이슈 (±{MOVE_PCT:.0f}% 이상 등락 · 주요 공시)</h3>{iss_detail}
<h3>백데이터</h3>
<details><summary>CNN 공포·탐욕 세부 지표 (1주 전 {cnn.get("prev_week", "-")} · 1달 전 {cnn.get("prev_month", "-")})</summary>
<div class="tw"><table><tr><th>지표</th><th>점수</th><th>판정</th></tr>{parts}</table>
<p class="muted"><a href="{esc(cnn.get("url", ""))}">CNN 원문 ↗</a></p></div></details>
<details><summary>미국 상위 30 등락</summary><div class="tw"><table><tr><th>#</th><th>종목</th><th>현재가</th><th>등락</th></tr>{mover_table(data["us_movers"], "$")}</table></div></details>
<details><summary>한국 상위 30 등락</summary><div class="tw"><table><tr><th>#</th><th>종목</th><th>현재가</th><th>등락</th></tr>{mover_table(data["kr_movers"], "")}</table></div></details>
<details><summary>수집 현황</summary><div class="tw"><table><tr><th>소스</th><th>상태</th><th>건수</th><th>오류</th></tr>{src_rows}</table></div></details>
<div class="meta"><span>업데이트 {esc(data["updated_at"])} KST</span>
<span><a href="../../{esc(data["week"])}/">이번 주 캘린더</a> · <a href="data.json">data.json</a> · <a href="../">지난 브리핑</a></span></div>
</main></body></html>'''


CSS = '''*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#fdf6ef;--ink:#2b2622;--sub:#a49a91;--line:#f0e6dc;--card:#fff;--accent:#e8774a;--up:#d64545;--down:#2f6fd0}
body{background:var(--bg);font-family:Pretendard,-apple-system,sans-serif;color:var(--ink)}
a{color:inherit;text-decoration:none}
#card{width:540px;max-width:100%;margin:0 auto;padding:30px 20px 22px;background:var(--bg)}
.head{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:14px}
h1{font-size:27px;font-weight:900;letter-spacing:-1px}
.range{font-size:19px;font-weight:800}
.blk{background:var(--card);border:1.5px solid var(--line);border-radius:14px;padding:11px 12px 10px;margin-bottom:8px}
.bt{font-size:12.5px;font-weight:800;color:#7d746c;margin-bottom:7px}
.at{font-weight:600;color:var(--sub);margin-left:4px}
.grid{display:grid;gap:5px}
.mrow{display:flex;gap:8px;margin-bottom:8px}.mrow:last-child{margin-bottom:0}
.mg{min-width:0}
.mgl{font-size:11px;font-weight:800;color:#7d746c;margin-bottom:4px}
.flowt{justify-content:center;gap:3px}
.fr{font-size:11.5px;color:#5f5750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fr b{color:var(--ink);margin-right:4px}
.tile{min-width:0;background:#faf6f1;border-radius:9px;padding:6px 7px 4px;display:flex;flex-direction:column}
.tile .sp{width:100%;height:20px;margin-top:3px}
.tv small{font-size:11px;font-weight:700}
.mbar{position:relative;height:6px;border-radius:3px;margin-top:8px;background:linear-gradient(90deg,#5b8bd6,#c9c3bd 50%,#e8774a)}
.mbar span{position:absolute;top:-3px;width:3px;height:12px;margin-left:-1.5px;border-radius:2px;background:var(--ink);box-shadow:0 0 0 1.5px #fff}
.mticks{display:flex;justify-content:space-between;font-size:9.5px;color:var(--sub);margin-top:2px}
.tl{font-size:10.5px;color:#7d746c;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tv{font-size:13px;letter-spacing:-.3px;font-weight:800;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tc{font-size:10.5px;white-space:nowrap;font-weight:700;margin-top:1px}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--sub)}
.flow{font-size:11.5px;color:#7d746c;margin-top:8px}
.cos{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}
.ch{font-size:12px;font-weight:800;margin-bottom:3px}
.co{display:flex;align-items:center;gap:5px;padding:3px 0;border-top:1px solid #f5eee7;font-size:12.5px}
.cn{flex:1;min-width:0;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.co .sp{width:38px;height:15px;flex:none}
.cp{flex:none;font-size:10.5px;font-weight:600;color:#5f5750;text-align:right;letter-spacing:-.2px}
.se .sp{width:40px;height:14px;flex:none}
.se{display:flex;align-items:center;gap:4px;padding:2.5px 0;font-size:12px}
.sn{width:56px;flex:none;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb{flex:1;position:relative;height:9px}
.sb::before{content:'';position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:#cfc5bb}
.sb i{position:absolute;top:0;height:9px}
.sb i.pos{background:var(--up);border-radius:0 3px 3px 0}
.sb i.neg{background:var(--down);border-radius:3px 0 0 3px}
.sc{width:46px;white-space:nowrap;text-align:right;font-size:11px;font-weight:700;flex:none}
.sm{width:42px;white-space:nowrap;text-align:right;font-size:9.5px;color:var(--sub);flex:none}
.cc{width:50px;white-space:nowrap;text-align:right;font-size:11.5px;font-weight:700;flex:none}
.fg{margin-bottom:10px}.fg:last-child{margin-bottom:2px}
.fgh{display:flex;align-items:baseline;gap:7px}
.fgl{font-size:13px;font-weight:700;flex:1}
.fgs{font-size:20px;font-weight:900}
.fgr{font-size:13px;font-weight:800}
.fgp{font-size:11.5px;color:var(--sub)}
.bar{position:relative;height:8px;border-radius:4px;margin-top:6px;background:linear-gradient(90deg,#5b8bd6,#c9c3bd 50%,#e8774a)}
.bar span{position:absolute;top:-4px;width:4px;height:16px;margin-left:-2px;border-radius:2px;background:var(--ink);box-shadow:0 0 0 2px #fff}
.ticks{display:flex;justify-content:space-between;font-size:10px;color:var(--sub);margin-top:4px}
.ln{font-size:13.5px;line-height:1.35;padding:3px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ls{font-size:12px;color:#7d746c;margin-left:7px}
.ln b{font-weight:700}
.sub{font-size:12px;color:#7d746c;margin:2px 0 0 26px;line-height:1.4}
.flag{margin-right:6px}
.t{font-size:11.5px;color:var(--sub);font-weight:600;margin-left:6px}
.badge{display:inline-block;background:var(--ink);color:#f7c948;font-size:10.5px;font-weight:800;border-radius:9px;padding:2px 8px;margin-left:6px}
.muted{color:var(--sub)}
.foot{display:flex;justify-content:space-between;font-size:10.5px;color:var(--sub);margin-top:14px}
main{max-width:720px;margin:0 auto;padding:8px 16px 60px}
h3{font-size:17px;font-weight:800;margin:30px 0 10px}
.row{display:flex;align-items:flex-start;gap:4px;background:var(--card);border:1.5px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:8px}
.rb{flex:1;min-width:0}.rt{font-size:15px;font-weight:700}
.rd{font-size:13px;color:#5f5750;margin-top:4px;line-height:1.45}
.row ul{margin:6px 0 0 18px;font-size:13px;line-height:1.5}
.row li a{text-decoration:underline;text-decoration-color:#e0d6cc}
.src{flex:none;font-size:12.5px;font-weight:700;color:var(--accent);padding-left:8px}
details{background:var(--card);border:1.5px solid var(--line);border-radius:12px;margin-bottom:8px}
summary{cursor:pointer;padding:12px 14px;font-size:14px;font-weight:700}
.tw{overflow-x:auto;padding:0 14px 12px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
td,th{text-align:left;padding:5px 6px;border-top:1px solid var(--line)}
.err{color:#c0492b;word-break:break-all}
.meta{font-size:12px;color:var(--sub);margin-top:24px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
.meta a{color:var(--accent);font-weight:700}
@media (max-width:420px){#card{padding:22px 12px 18px}.cos{grid-template-columns:1fr}h1{font-size:24px}}'''


def render_index():
    rows = []
    for p in sorted(DAILY.glob("*/data.json"), reverse=True):
        d = json.loads(p.read_text())
        day = date.fromisoformat(d["date"])
        us = (d.get("markets") or {}).get("us") or [{}]
        sp = f'S&P {us[0]["chg"]:+.1f}%' if us[0].get("chg") is not None else ""
        fg = (d.get("fear_greed") or {}).get("cnn")
        rows.append(f'<a class="w" href="{p.parent.name}/"><b>{day.month}.{day.day} ({WEEKDAYS[day.weekday()]})</b>'
                    f'<span>{sp}{" · 공포탐욕 " + str(fg["score"]) if fg else ""}</span></a>')
    (DAILY / "index.html").write_text(f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>데일리 브리핑</title>
<link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0}}body{{background:#fdf6ef;color:#2b2622;font-family:Pretendard,-apple-system,sans-serif;max-width:720px;margin:0 auto;padding:40px 16px}}
h1{{font-size:28px;font-weight:900;margin-bottom:20px}}.w{{display:flex;justify-content:space-between;background:#fff;border:1.5px solid #f0e6dc;border-radius:12px;padding:14px 16px;margin-bottom:8px;color:inherit;text-decoration:none}}
.w span{{color:#a49a91;font-size:13px}}</style></head><body><h1>데일리 브리핑</h1>
<a class="w" href="../"><b>📅 주간 캘린더 모아보기</b><span>→</span></a>{"".join(rows)}</body></html>''')


# ---------------------------------------------------------------- 실행

def build(day):
    data = collect(day)
    out = DAILY / day.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    (out / "index.html").write_text(render(data))
    cb.screenshot_card(out / "index.html", out / "card.png")
    write_back(data["results"])
    render_index()
    print(f"생성: {out}/ (index.html, card.png, data.json)")


def send(day):
    out = DAILY / day.isoformat()
    data = json.loads((out / "data.json").read_text())
    us = (data["markets"] or {}).get("us", [])
    summary = " · ".join(f'{m["name"]} {m["chg"]:+.1f}%' for m in us[:2])
    fg = data["fear_greed"].get("cnn")
    if fg:
        summary += f' · 공포탐욕 {fg["score"]}({fg["rating"]})'
    caption = f"📰 {day.month}.{day.day} ({WEEKDAYS[day.weekday()]}) 데일리 브리핑\n{summary}"
    failed = [s["name"] for s in data["sources"] if not s["ok"]]
    if failed:
        caption += "\n⚠️ 못 가져온 항목: " + ", ".join(failed)
    buttons = {"inline_keyboard": [
        [{"text": "🔗 자세히 보기", "url": f"{PAGES_URL}/daily/{day.isoformat()}/"}],
        [{"text": "📅 이번 주 캘린더", "url": f"{PAGES_URL}/{data['week']}/"},
         {"text": "📚 지난 브리핑", "url": f"{PAGES_URL}/daily/"}]]}
    with open(out / "card.png", "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendPhoto",
                          data={"chat_id": env("TELEGRAM_CHAT_ID"), "caption": caption,
                                "reply_markup": json.dumps(buttons)}, files={"photo": f}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"텔레그램 발송 실패: {r.status_code} {r.text[:200]}")
    print("텔레그램 발송 완료")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["build", "send"])
    ap.add_argument("--date", help="브리핑 날짜 YYYY-MM-DD. 생략하면 오늘(KST)")
    args = ap.parse_args()
    cb.load_dotenv()
    day = date.fromisoformat(args.date) if args.date else datetime.now(KST).date()
    build(day) if args.command == "build" else send(day)


if __name__ == "__main__":
    main()
