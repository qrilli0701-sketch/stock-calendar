"""주간 증시 캘린더: 일정을 모아 주간 HTML 페이지 하나를 만들고, 그 윗부분을 캡처해 텔레그램으로 보낸다.

  python calendar_bot.py build [--week 2026-10-19]   # docs/<주>/ 에 data.json, index.html, calendar.png 생성
  python calendar_bot.py send  [--week 2026-10-19]   # 만들어둔 이미지와 페이지 링크를 텔레그램으로 발송

docs/ 는 GitHub Pages로 게시된다.
"""
import argparse
import html
import io
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
ET_TZ = ZoneInfo("America/New_York")
ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
# GitHub Pages 주소. Actions에서는 워크플로가 저장소 이름으로 채워준다 (https://<아이디>.github.io/<저장소>)
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
WEEKDAYS = "월화수목금토일"
FLAG = {"KR": "🇰🇷", "US": "🇺🇸", "CN": "🇨🇳", "JP": "🇯🇵", "EU": "🇪🇺"}

# 미국 시총 상위 30을 고를 후보군 (시총은 실행할 때마다 Finnhub에서 새로 받아 정렬)
US_POOL = {
    "NVDA": "엔비디아", "AAPL": "애플", "MSFT": "마이크로소프트", "GOOGL": "알파벳",
    "AMZN": "아마존", "META": "메타", "AVGO": "브로드컴", "TSLA": "테슬라",
    "BRK.B": "버크셔해서웨이", "TSM": "TSMC", "LLY": "일라이릴리", "JPM": "JP모건",
    "WMT": "월마트", "V": "비자", "ORCL": "오라클", "MA": "마스터카드", "XOM": "엑슨모빌",
    "NFLX": "넷플릭스", "COST": "코스트코", "JNJ": "존슨앤드존슨", "PLTR": "팔란티어",
    "ABBV": "애브비", "HD": "홈디포", "BAC": "뱅크오브아메리카", "PG": "P&G", "AMD": "AMD",
    "UNH": "유나이티드헬스", "KO": "코카콜라", "GE": "GE에어로스페이스", "CVX": "셰브론",
    "CSCO": "시스코", "WFC": "웰스파고", "IBM": "IBM", "MU": "마이크론", "PM": "필립모리스",
    "CRM": "세일즈포스", "MS": "모건스탠리", "GS": "골드만삭스", "AXP": "아메리칸익스프레스",
    "LRCX": "램리서치", "AMAT": "어플라이드머티어리얼즈", "TMUS": "T모바일", "ABT": "애보트",
    "MCD": "맥도날드", "RTX": "RTX", "CAT": "캐터필러", "DIS": "디즈니", "LIN": "린데",
    "INTU": "인튜이트", "T": "AT&T", "PEP": "펩시코", "ISRG": "인튜이티브서지컬",
    "UBER": "우버", "NOW": "서비스나우", "QCOM": "퀄컴", "BX": "블랙스톤", "SCHW": "찰스슈왑",
    "BKNG": "부킹홀딩스", "ASML": "ASML", "INTC": "인텔", "SPOT": "스포티파이",
    "KLAC": "KLA", "ANET": "아리스타", "APP": "앱러빈",
}
# 해외 기업 ADR: Finnhub 시총이 현지 통화라서 ADR 가격 × 주식수 ÷ (ADR 1주당 원주 수)로 달러 시총을 계산
ADR_RATIO = {"TSM": 5, "ASML": 1}

# FRED 발표 중 캘린더에 넣을 것: release_id → (이름, 미 동부시간 발표 시각, 핵심 여부)
FRED_RELEASES = {
    10: ("소비자물가(CPI)", (8, 30), True),
    50: ("고용보고서", (8, 30), True),
    53: ("GDP", (8, 30), False),
    54: ("PCE 물가", (8, 30), False),
    46: ("생산자물가(PPI)", (8, 30), False),
    9: ("소매판매", (8, 30), False),
    192: ("JOLTS 구인", (10, 0), False),
    91: ("미시간대 소비자심리", (10, 0), False),
}
TOP_N = 30


def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} 가 설정되지 않았어요")
    return v


def load_dotenv():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def kst_time_label(dt):
    """KST datetime → '새벽 5시', '오전 10시', '오후 2시 10분', '밤 11시' 같은 표시."""
    h, m = dt.hour, dt.minute
    if h < 6:
        part, hh = "새벽", h
    elif h < 12:
        part, hh = "오전", h
    elif h < 18:
        part, hh = "오후", h - 12 if h > 12 else 12
    elif h < 21:
        part, hh = "저녁", h - 12
    else:
        part, hh = "밤", h - 12
    return f"{part} {hh}시" + (f" {m}분" if m else "")


def get_retry(url, tries=3, wait=10, **kw):
    kw.setdefault("timeout", 60)
    for attempt in range(tries):
        try:
            r = requests.get(url, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(wait)


# ---------------------------------------------------------------- 수집
# 모든 일정 항목은 같은 모양의 dict:
#   date, country, title, time, key(핵심), url(원문), detail(부가 설명),
#   group(같은 날·시간 실적을 카드에서 한 줄로 합칠 때 쓰는 이름, 실적만), rank, filtered(기준 밖이라 카드엔 안 나옴)

def finnhub_get(path, **params):
    params["token"] = env("FINNHUB_API_KEY")
    for _ in range(3):
        r = requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=20)
        if r.status_code != 429:
            time.sleep(1.05)  # 무료 한도 분당 60회
            return r.json() if r.ok else {}
        time.sleep(30)
    return {}


def us_ranking():
    """후보군 전체를 달러 시총(10억 달러) 순으로."""
    rows = []
    for sym, name in US_POOL.items():
        p = finnhub_get("stock/profile2", symbol=sym)
        cap = p.get("marketCapitalization")  # 백만 단위
        if cap and p.get("currency") != "USD":
            if sym not in ADR_RATIO:
                continue
            price = finnhub_get("quote", symbol=sym).get("c")
            cap = price * p["shareOutstanding"] / ADR_RATIO[sym] if price else None
        if cap:
            rows.append({"symbol": sym, "name": name, "name_en": p.get("name", ""),
                         "cap_usd_b": round(cap / 1000, 1)})
    rows.sort(key=lambda r: -r["cap_usd_b"])
    return rows


def us_earnings(start, end, ranking):
    """미국 실적. 장 마감 후(amc) 발표는 한국 시간으로 다음 날 새벽. 후보군 중 30위 밖은 filtered."""
    rank = {r["symbol"]: i for i, r in enumerate(ranking)}
    data = get_retry("https://finnhub.io/api/v1/calendar/earnings", params={
        "from": (start - timedelta(days=1)).isoformat(), "to": end.isoformat(),
        "token": env("FINNHUB_API_KEY")}).json()
    out = []
    for e in data.get("earningsCalendar", []):
        sym = e["symbol"]
        if sym not in rank:
            continue
        d = date.fromisoformat(e["date"])
        hour = e.get("hour")
        if hour == "amc":
            d, label, when = d + timedelta(days=1), "새벽", "미국 장 마감 후"
        elif hour == "bmo":
            label, when = "밤", "미국 장 시작 전"
        else:
            label, when = "", "시간 미정"
        if not start <= d <= end:
            continue
        parts = [when]
        if e.get("epsEstimate") is not None:
            parts.append(f"EPS 예상 ${e['epsEstimate']:.2f}")
        if e.get("revenueEstimate"):
            parts.append(f"매출 예상 ${e['revenueEstimate'] / 1e9:.1f}B")
        name = US_POOL[sym]
        out.append({
            "date": d, "country": "US", "title": f"{name} 실적", "group": name, "time": label, "symbol": sym,
            "key": False, "detail": " · ".join(parts), "rank": rank[sym],
            "url": f"https://www.nasdaq.com/market-activity/stocks/{sym.lower().replace('.', '-')}/earnings",
            "filtered": rank[sym] >= TOP_N, "filter_reason": f"미국 시총 {rank[sym] + 1}위",
        })
    return out


def kr_ranking():
    """코스피+코스닥 보통주 시총(억원) 순."""
    rows = []
    for market in ("KOSPI", "KOSDAQ"):
        r = get_retry(f"https://m.stock.naver.com/api/stocks/marketValue/{market}",
                      params={"page": 1, "pageSize": 60}, headers=UA)
        for s in r.json()["stocks"]:
            # 보통주만 (우선주는 종목코드 끝자리가 0이 아님)
            if s.get("stockEndType") == "stock" and s["itemCode"].endswith("0"):
                rows.append({"code": s["itemCode"], "name": s["stockName"], "market": market,
                             "cap_krw_eok": int(float(s["marketValue"].replace(",", "")))})
    rows.sort(key=lambda r: -r["cap_krw_eok"])
    return rows[:TOP_N]


def kr_earnings(start, end, ranking):
    """DART '기업설명회(IR)개최' 공시에서 실적발표 일시를 읽는다."""
    key = env("OPENDART_API_KEY")
    z = zipfile.ZipFile(io.BytesIO(get_retry(
        "https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}).content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    corp = {c.findtext("stock_code").strip(): c.findtext("corp_code")
            for c in root.iter("list") if (c.findtext("stock_code") or "").strip()}

    out, seen = [], set()
    for rank, row in enumerate(ranking):
        if row["code"] not in corp:
            continue
        r = get_retry("https://opendart.fss.or.kr/api/list.json", params={
            "crtfc_key": key, "corp_code": corp[row["code"]],
            "bgn_de": (start - timedelta(days=45)).strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"), "page_count": 100}).json()
        for item in r.get("list", []):  # 최신 공시부터 → 정정 공시가 있으면 최신 것이 남음
            if "기업설명회" not in item["report_nm"]:
                continue
            doc = get_retry("https://opendart.fss.or.kr/api/document.xml", params={
                "crtfc_key": key, "rcept_no": item["rcept_no"]}).content
            dz = zipfile.ZipFile(io.BytesIO(doc))
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ",
                          dz.read(dz.namelist()[0]).decode("utf-8", "ignore")))
            m = re.search(r"일시\s*(\d{4})[-.](\d{2})[-.](\d{2})\s*(\d{1,2}):(\d{2})?", text)
            purpose = re.search(r"개최목적\s*(.+?)\s*\d\.\s", text)
            method = re.search(r"개최방법\s*(.+?)\s*\d\.\s", text)
            if not m or not purpose or not re.search(r"실적|결산", purpose[1]):
                continue
            dt = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5] or 0), tzinfo=KST)
            if not start <= dt.date() <= end or (row["name"], dt.date()) in seen:
                continue
            seen.add((row["name"], dt.date()))
            out.append({
                "date": dt.date(), "country": "KR", "title": f"{row['name']} 실적",
                "group": row["name"], "time": kst_time_label(dt), "key": False,
                "detail": " · ".join(x for x in (purpose[1][:40], method[1][:30] if method else "") if x),
                "rank": rank, "filtered": False,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item['rcept_no']}",
            })
    return out


def us_macro(start, end):
    r = get_retry("https://api.stlouisfed.org/fred/releases/dates", params={
        "api_key": env("FRED_API_KEY"), "file_type": "json",
        "realtime_start": (start - timedelta(days=1)).isoformat(), "realtime_end": end.isoformat(),
        "include_release_dates_with_no_data": "true", "limit": 1000})
    out = []
    for x in r.json()["release_dates"]:
        if x["release_id"] not in FRED_RELEASES:
            continue
        title, (h, m), key = FRED_RELEASES[x["release_id"]]
        d = date.fromisoformat(x["date"])
        dt = datetime(d.year, d.month, d.day, h, m, tzinfo=ET_TZ).astimezone(KST)
        if start <= dt.date() <= end:
            out.append({"date": dt.date(), "country": "US", "title": title,
                        "time": kst_time_label(dt), "key": key,
                        "detail": f"미 동부 {h}:{m:02d} 발표 · {x['release_name']}",
                        "url": f"https://fred.stlouisfed.org/release?rid={x['release_id']}"})
    return out


def next_weekday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def kr_trade_stats(start, end):
    """정기 수출입 통계: 매월 1일 수출입동향(산업부), 21일 1~20일 수출입(관세청). 주말이면 다음 평일."""
    out = []
    for y, mo in {(start.year, start.month), (end.year, end.month)}:
        for day, title, t, url in (
                (1, f"{mo - 1 or 12}월 수출입동향 (산업부)", "오전 9시", "https://www.motie.go.kr"),
                (21, f"{mo}월 1~20일 수출입 (관세청)", "", "https://www.customs.go.kr")):
            d = next_weekday(date(y, mo, day))
            if start <= d <= end:
                out.append({"date": d, "country": "KR", "title": title, "time": t, "key": False,
                            "detail": "정기 발표일 규칙으로 계산 (공휴일은 반영 안 됨)", "url": url})
    return out


def static_events(start, end):
    data = json.loads((ROOT / "events.json").read_text())
    out = []
    for e in data["events"]:
        d = date.fromisoformat(e["date"])
        if start <= d <= end:
            out.append({"date": d, "country": e["country"], "title": e["title"],
                        "time": e.get("time", ""), "key": e.get("key", False),
                        "detail": e.get("detail", ""), "url": e.get("url", "")})
    return out


def collect_week(start, end):
    """모든 소스를 모아 data.json에 들어갈 dict를 만든다. 한 소스가 실패해도 나머지는 계속."""
    sources, items = [], []

    def run(label, fn, *a):
        try:
            got = fn(*a)
            sources.append({"name": label, "ok": True, "count": len(got)})
            print(f"[ok] {label}: {len(got)}")
            return got
        except Exception as e:
            sources.append({"name": label, "ok": False, "count": 0, "error": str(e)[:300]})
            print(f"[실패] {label}: {e}", file=sys.stderr)
            return []

    us_rank = run("미국 시총 순위 (Finnhub)", us_ranking)
    kr_rank = run("한국 시총 순위 (네이버)", kr_ranking)
    if us_rank:
        items += run("미국 실적 (Finnhub)", us_earnings, start, end, us_rank)
    if kr_rank:
        items += run("한국 실적 (DART)", kr_earnings, start, end, kr_rank)
    items += run("미국 경제지표 (FRED)", us_macro, start, end)
    items += run("한국 수출입 (규칙)", kr_trade_stats, start, end)
    items += run("고정 일정 (events.json)", static_events, start, end)

    for it in items:
        it["date"] = it["date"].isoformat()
        it.setdefault("filtered", False)
    items.sort(key=lambda i: (i["date"], not i["key"], i["country"] != "KR", i.get("rank", -1)))
    return {
        "week_start": start.isoformat(), "week_end": end.isoformat(),
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "items": items, "sources": sources,
        "us_ranking": us_rank, "kr_ranking": kr_rank,
    }


# ---------------------------------------------------------------- 페이지

def esc(s):
    return html.escape(str(s or ""))


def card_lines(day_items):
    """요약 카드용: 같은 날·나라·시간 실적은 '메타·테슬라 실적'처럼 한 줄로."""
    lines, groups = [], defaultdict(list)
    for it in day_items:
        if it.get("group"):
            groups[(it["country"], it["time"])].append(it)
        else:
            lines.append(dict(it))
    for (country, t), g in groups.items():
        g.sort(key=lambda x: x["rank"])
        lines.append({"country": country, "time": t, "key": False, "rank": g[0]["rank"],
                      "title": "·".join(x["group"] for x in g) + " 실적"})
    lines.sort(key=lambda e: (not e.get("key"), e["country"] != "KR", e.get("rank", -1)))
    return lines


def render_page(data):
    start, end = date.fromisoformat(data["week_start"]), date.fromisoformat(data["week_end"])
    shown = [i for i in data["items"] if not i["filtered"]]
    by_day = defaultdict(list)
    for it in shown:
        by_day[it["date"]].append(it)
    days = sorted(by_day)

    # ① 요약 카드 (이 영역만 캡처해서 텔레그램 이미지로 씀)
    cards = []
    for d in days:
        dd = date.fromisoformat(d)
        lines = card_lines(by_day[d])
        hot = any(e.get("key") for e in lines)
        evs = "".join(
            f'<div class="ev"><span class="flag">{FLAG.get(e["country"], "🌐")}</span>'
            f'<span class="ti">{esc(e["title"])}</span>'
            + (f'<span class="t">{esc(e["time"])}</span>' if e["time"] else "")
            + ('<span class="badge">★ 핵심</span>' if e.get("key") else "") + "</div>"
            for e in lines)
        cards.append(f'<a class="card{" hot" if hot else ""}" href="#d{d}"><div class="day">'
                     f'<div class="dn">{dd.day}</div><div class="dw">{WEEKDAYS[dd.weekday()]}</div></div>'
                     f'<div class="evs">{evs}</div></a>')
    if not cards:
        cards.append('<div class="card"><div class="evs"><div class="ev">이번 주는 큰 일정이 없어요</div></div></div>')

    # ② 날짜별 상세
    sections = []
    for d in days:
        dd = date.fromisoformat(d)
        rows = "".join(
            f'<div class="row"><span class="flag">{FLAG.get(it["country"], "🌐")}</span>'
            f'<div class="rb"><div class="rt">{esc(it["title"])}'
            + (f' <span class="t">{esc(it["time"])}</span>' if it["time"] else "")
            + (' <span class="badge">★ 핵심</span>' if it.get("key") else "") + "</div>"
            + (f'<div class="rd">{esc(it["detail"])}</div>' if it.get("detail") else "")
            + (f'<div class="res">✅ 결과 · {esc(it["result"])}</div>' if it.get("result") else "") + "</div>"
            + (f'<a class="src" href="{esc(it["url"])}" target="_blank" rel="noopener">원문 ↗</a>' if it.get("url") else "")
            + "</div>"
            for it in by_day[d])
        sections.append(f'<section id="d{d}"><h2>{dd.month}월 {dd.day}일 ({WEEKDAYS[dd.weekday()]})</h2>{rows}</section>')

    # ③ 백데이터
    src_rows = "".join(
        f'<tr><td>{esc(s["name"])}</td><td>{"✅" if s["ok"] else "❌"}</td><td>{s["count"]}</td>'
        f'<td class="err">{esc(s.get("error", ""))}</td></tr>' for s in data["sources"])
    us_rows = "".join(
        f'<tr class="{"in" if i < TOP_N else "out"}"><td>{i + 1}</td><td>{esc(r["name"])}</td>'
        f'<td>{esc(r["symbol"])}</td><td>${r["cap_usd_b"]:,.0f}B</td></tr>'
        for i, r in enumerate(data["us_ranking"]))
    kr_rows = "".join(
        f'<tr class="in"><td>{i + 1}</td><td>{esc(r["name"])}</td><td>{esc(r["market"])}</td>'
        f'<td>{r["cap_krw_eok"] / 10000:,.1f}조</td></tr>' for i, r in enumerate(data["kr_ranking"]))
    filtered = [i for i in data["items"] if i["filtered"]]
    flt_rows = "".join(
        f'<tr><td>{esc(i["date"][5:])}</td><td>{esc(i["title"])}</td><td>{esc(i.get("filter_reason", ""))}</td></tr>'
        for i in filtered) or '<tr><td colspan="3">없음</td></tr>'

    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>증시 캘린더 {start.month}.{start.day}–{end.month}.{end.day}</title>
<link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#fdf6ef;--ink:#2b2622;--sub:#a49a91;--line:#f0e6dc;--card:#fff;--hot:#fdf3c8;--hotline:#f3d76b;--accent:#e8774a}}
body{{background:var(--bg);font-family:Pretendard,-apple-system,sans-serif;color:var(--ink)}}
a{{color:inherit;text-decoration:none}}
#card{{width:540px;max-width:100%;margin:0 auto;padding:44px 28px 30px;background:var(--bg)}}
.head{{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:28px;gap:12px}}
h1{{font-size:32px;font-weight:900;letter-spacing:-1px}}
.range{{text-align:right;font-size:19px;font-weight:800;letter-spacing:-.5px;white-space:nowrap}}
.card{{display:flex;background:var(--card);border:1.5px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:10px}}
.card.hot{{background:var(--hot);border-color:var(--hotline)}}
.day{{width:54px;flex:none;text-align:center;border-right:1.5px solid var(--line);margin-right:14px;padding-right:12px;display:flex;flex-direction:column;justify-content:center}}
.dn{{font-size:26px;font-weight:900;line-height:1}}
.hot .dn{{color:var(--accent)}}
.dw{{font-size:12px;color:#9b928a;margin-top:6px}}
.evs{{flex:1;display:flex;flex-direction:column;justify-content:center;gap:7px}}
.ev{{font-size:15.5px;font-weight:700;letter-spacing:-.4px;line-height:1.35}}
.flag{{margin-right:7px}}
.t{{font-size:11.5px;color:var(--sub);font-weight:600;margin-left:6px;white-space:nowrap}}
.badge{{display:inline-block;background:var(--ink);color:#f7c948;font-size:10.5px;font-weight:800;border-radius:9px;padding:2px 8px;margin-left:6px;vertical-align:2px}}
.foot{{display:flex;justify-content:space-between;gap:8px;font-size:10.5px;color:var(--sub);margin-top:18px}}
main{{max-width:720px;margin:0 auto;padding:8px 16px 60px}}
section{{margin-top:28px;scroll-margin-top:12px}}
h2{{font-size:18px;font-weight:800;margin-bottom:10px}}
h3{{font-size:16px;font-weight:800;margin:36px 0 10px}}
.row{{display:flex;align-items:flex-start;gap:4px;background:var(--card);border:1.5px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:8px}}
.rb{{flex:1;min-width:0}}
.rt{{font-size:15px;font-weight:700}}
.rd{{font-size:12.5px;color:#7d746c;margin-top:4px;line-height:1.45}}
.res{{font-size:13px;font-weight:700;color:#2f7d4f;margin-top:6px;line-height:1.45}}
.src{{flex:none;font-size:12.5px;font-weight:700;color:var(--accent);padding:2px 0 2px 8px}}
details{{background:var(--card);border:1.5px solid var(--line);border-radius:12px;margin-bottom:8px}}
summary{{cursor:pointer;padding:12px 14px;font-size:14px;font-weight:700}}
.tw{{overflow-x:auto;padding:0 14px 12px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px}}
td,th{{text-align:left;padding:5px 6px;border-top:1px solid var(--line);vertical-align:top}}
tr.out{{color:var(--sub)}}
.err{{color:#c0492b;word-break:break-all}}
.meta{{font-size:12px;color:var(--sub);margin-top:24px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}}
.meta a{{color:var(--accent);font-weight:700}}
@media (max-width:420px){{h1{{font-size:26px}} .range{{font-size:16px}} #card{{padding:32px 16px 24px}}}}
</style></head><body>
<div id="card">
<div class="head"><h1>이번 주 증시 캘린더</h1><div class="range">{start.month}.{start.day} – {end.month}.{end.day}</div></div>
{"".join(cards)}
<div class="foot"><span>한국 시간 기준 · 일정은 바뀔 수 있어요 · 투자 권유 아님</span><span>미·한 시총 상위 {TOP_N}</span></div>
</div>
<main>
{"".join(sections)}
<h3>백데이터</h3>
<details><summary>수집 현황</summary><div class="tw"><table><tr><th>소스</th><th>상태</th><th>건수</th><th>오류</th></tr>{src_rows}</table></div></details>
<details><summary>기준에서 빠진 실적 일정 ({len(filtered)}건)</summary><div class="tw"><table><tr><th>날짜</th><th>일정</th><th>이유</th></tr>{flt_rows}</table></div></details>
<details><summary>미국 시총 순위 (후보 {len(data["us_ranking"])}개 중 상위 {TOP_N})</summary><div class="tw"><table><tr><th>#</th><th>종목</th><th>티커</th><th>시총</th></tr>{us_rows}</table></div></details>
<details><summary>한국 시총 상위 {TOP_N} (보통주)</summary><div class="tw"><table><tr><th>#</th><th>종목</th><th>시장</th><th>시총</th></tr>{kr_rows}</table></div></details>
<div class="meta"><span>업데이트 {esc(data["updated_at"])} KST</span><span><a href="data.json">data.json</a> · <a href="../">지난 캘린더</a></span></div>
</main>
</body></html>'''


def render_index():
    """docs/index.html: 주간 페이지 목록 (최신 순)."""
    weeks = []
    for p in sorted(DOCS.glob("*/data.json"), reverse=True):
        d = json.loads(p.read_text())
        s, e = date.fromisoformat(d["week_start"]), date.fromisoformat(d["week_end"])
        n = sum(1 for i in d["items"] if not i["filtered"])
        weeks.append(f'<a class="w" href="{p.parent.name}/"><b>{s.year}. {s.month}.{s.day} – {e.month}.{e.day}</b>'
                     f'<span>일정 {n}건</span></a>')
    (DOCS / "index.html").write_text(f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>주간 증시 캘린더</title>
<link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0}}body{{background:#fdf6ef;color:#2b2622;font-family:Pretendard,-apple-system,sans-serif;max-width:720px;margin:0 auto;padding:40px 16px}}
h1{{font-size:28px;font-weight:900;margin-bottom:20px}}.w{{display:flex;justify-content:space-between;background:#fff;border:1.5px solid #f0e6dc;border-radius:12px;padding:14px 16px;margin-bottom:8px;color:inherit;text-decoration:none}}
.w span{{color:#a49a91;font-size:13px}}</style></head><body><h1>주간 증시 캘린더</h1><a class="w" href="daily/"><b>📰 데일리 브리핑 모아보기</b><span>→</span></a>{"".join(weeks)}</body></html>''')


def screenshot_card(page_path, png_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:
            browser = p.chromium.launch(channel="chrome")  # 로컬 Mac: 설치된 크롬 사용
        page = browser.new_page(viewport={"width": 540, "height": 900}, device_scale_factor=2)
        page.goto(page_path.resolve().as_uri(), wait_until="networkidle")
        page.locator("#card").screenshot(path=str(png_path))
        browser.close()


# ---------------------------------------------------------------- 실행

def week_start_of(arg):
    if arg:
        return date.fromisoformat(arg)
    today = datetime.now(KST).date()
    return today - timedelta(days=today.weekday())


def build(start):
    end = start + timedelta(days=6)
    data = collect_week(start, end)
    out = DOCS / start.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "data.json").exists():
        old = {(i["date"], i["title"]): i for i in json.loads((out / "data.json").read_text())["items"]}
        for it in data["items"]:
            prev = old.get((it["date"], it["title"]), {})
            for k in ("result", "result_url"):
                if prev.get(k):
                    it[k] = prev[k]
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    (out / "index.html").write_text(render_page(data))
    screenshot_card(out / "index.html", out / "calendar.png")
    render_index()
    print(f"생성: {out}/ (index.html, calendar.png, data.json)")


def send(start):
    out = DOCS / start.isoformat()
    data = json.loads((out / "data.json").read_text())
    end = date.fromisoformat(data["week_end"])
    caption = f"📅 이번 주 증시 캘린더 ({start.month}.{start.day}–{end.month}.{end.day})"
    failed = [s["name"] for s in data["sources"] if not s["ok"]]
    if failed:
        caption += "\n⚠️ 못 가져온 항목: " + ", ".join(failed)
    buttons = {"inline_keyboard": [[
        {"text": "🔗 자세히 보기", "url": f"{PAGES_URL}/{start.isoformat()}/"},
        {"text": "📚 지난 캘린더", "url": f"{PAGES_URL}/"}]]}
    with open(out / "calendar.png", "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendPhoto",
                          data={"chat_id": env("TELEGRAM_CHAT_ID"), "caption": caption,
                                "reply_markup": json.dumps(buttons)},
                          files={"photo": f}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"텔레그램 발송 실패: {r.status_code} {r.text[:200]}")
    print("텔레그램 발송 완료")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["build", "send"])
    ap.add_argument("--week", help="주 시작일(월요일) YYYY-MM-DD. 생략하면 이번 주")
    args = ap.parse_args()
    load_dotenv()
    start = week_start_of(args.week)
    build(start) if args.command == "build" else send(start)


if __name__ == "__main__":
    main()
