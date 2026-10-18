"""주간 증시 캘린더: 일정을 모아 이미지로 만들고 텔레그램으로 보낸다.

사용법:
  python calendar_bot.py               # 이번 주(월~일) 캘린더를 만들어 텔레그램 발송
  python calendar_bot.py --dry-run     # 발송 없이 out/ 에 이미지만 저장
  python calendar_bot.py --week 2026-10-19 --dry-run
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
from bs4 import BeautifulSoup

KST = ZoneInfo("Asia/Seoul")
ET_TZ = ZoneInfo("America/New_York")
ROOT = Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
WEEKDAYS = "월화수목금토일"

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


# ---------------------------------------------------------------- 수집

# 해외 기업 ADR: Finnhub 시총이 현지 통화라서 ADR 가격 × 주식수 ÷ (ADR 1주당 원주 수)로 달러 시총을 계산
ADR_RATIO = {"TSM": 5, "ASML": 1}


def finnhub_get(path, **params):
    params["token"] = env("FINNHUB_API_KEY")
    for _ in range(3):
        r = requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=20)
        if r.status_code != 429:
            time.sleep(1.05)  # 무료 한도 분당 60회
            return r.json() if r.ok else {}
        time.sleep(30)
    return {}


def us_top30():
    caps = []
    for sym in US_POOL:
        p = finnhub_get("stock/profile2", symbol=sym)
        cap = p.get("marketCapitalization")
        if cap and p.get("currency") != "USD":
            if sym not in ADR_RATIO:
                continue
            price = finnhub_get("quote", symbol=sym).get("c")
            cap = price * p["shareOutstanding"] / ADR_RATIO[sym] if price else None
        if cap:
            caps.append((cap, sym))
    caps.sort(reverse=True)
    return [s for _, s in caps[:30]]


def us_earnings(start, end, top):
    """미국 실적. 장 마감 후(amc) 발표는 한국 시간으로 다음 날 새벽."""
    r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                     params={"from": (start - timedelta(days=1)).isoformat(),
                             "to": end.isoformat(), "token": env("FINNHUB_API_KEY")},
                     timeout=30)
    r.raise_for_status()
    out = []
    for e in r.json().get("earningsCalendar", []):
        sym = e["symbol"]
        if sym not in top:
            continue
        d = date.fromisoformat(e["date"])
        hour = e.get("hour")
        if hour == "amc":
            d, label = d + timedelta(days=1), "새벽"
        elif hour == "bmo":
            label = "밤"
        else:
            label = ""
        if start <= d <= end:
            out.append({"date": d, "country": "US", "kind": "earn",
                        "name": US_POOL.get(sym, sym), "time": label,
                        "rank": top.index(sym)})
    return out


def kr_top30():
    stocks = []
    for market in ("KOSPI", "KOSDAQ"):
        r = requests.get(f"https://m.stock.naver.com/api/stocks/marketValue/{market}",
                         params={"page": 1, "pageSize": 50}, headers=UA, timeout=20)
        r.raise_for_status()
        for s in r.json()["stocks"]:
            # 보통주만 (우선주는 종목코드 끝자리가 0이 아님)
            if s.get("stockEndType") == "stock" and s["itemCode"].endswith("0"):
                stocks.append((float(s["marketValue"].replace(",", "")),
                               s["itemCode"], s["stockName"]))
    stocks.sort(reverse=True)
    return [(code, name) for _, code, name in stocks[:30]]


def kr_earnings(start, end, top):
    """DART '기업설명회(IR)개최' 공시에서 실적발표 일시를 읽는다."""
    key = env("OPENDART_API_KEY")
    z = zipfile.ZipFile(io.BytesIO(requests.get(
        "https://opendart.fss.or.kr/api/corpCode.xml",
        params={"crtfc_key": key}, timeout=60).content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    corp = {c.findtext("stock_code").strip(): c.findtext("corp_code")
            for c in root.iter("list") if (c.findtext("stock_code") or "").strip()}

    out = []
    for rank, (code, name) in enumerate(top):
        if code not in corp:
            continue
        r = requests.get("https://opendart.fss.or.kr/api/list.json", params={
            "crtfc_key": key, "corp_code": corp[code],
            "bgn_de": (start - timedelta(days=45)).strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"), "page_count": 100}, timeout=30).json()
        for item in r.get("list", []):
            if "기업설명회" not in item["report_nm"]:
                continue
            doc = requests.get("https://opendart.fss.or.kr/api/document.xml", params={
                "crtfc_key": key, "rcept_no": item["rcept_no"]}, timeout=30).content
            dz = zipfile.ZipFile(io.BytesIO(doc))
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ",
                          dz.read(dz.namelist()[0]).decode("utf-8", "ignore")))
            m = re.search(r"일시\s*(\d{4})[-.](\d{2})[-.](\d{2})\s*(\d{1,2}):(\d{2})?", text)
            if not m or not re.search(r"개최목적[^.]{0,60}(실적|결산)", text):
                continue
            dt = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5] or 0), tzinfo=KST)
            if start <= dt.date() <= end:
                out.append({"date": dt.date(), "country": "KR", "kind": "earn",
                            "name": name, "time": kst_time_label(dt), "rank": rank})
    # 같은 회사가 IR을 여러 번(정정) 공시한 경우 한 번만
    seen, uniq = set(), []
    for e in out:
        if (e["name"], e["date"]) not in seen:
            seen.add((e["name"], e["date"]))
            uniq.append(e)
    return uniq


def kr_listings(start, end):
    """38커뮤니케이션 신규상장 표 (스팩 제외)."""
    r = requests.get("https://www.38.co.kr/html/fund/index.htm?o=nw", headers=UA, timeout=20)
    r.encoding = "euc-kr"
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        if len(tds) < 3 or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", tds[1]):
            continue
        d = datetime.strptime(tds[1], "%Y/%m/%d").date()
        name = re.sub(r"\(구\..*?\)", "", tds[0]).strip()
        if start <= d <= end and "스팩" not in name:
            out.append({"date": d, "country": "KR", "kind": "event",
                        "title": f"{name} 상장", "time": ""})
    return out


def us_macro(start, end):
    r = requests.get("https://api.stlouisfed.org/fred/releases/dates", params={
        "api_key": env("FRED_API_KEY"), "file_type": "json",
        "realtime_start": (start - timedelta(days=1)).isoformat(),
        "realtime_end": end.isoformat(),
        "include_release_dates_with_no_data": "true", "limit": 1000}, timeout=30)
    r.raise_for_status()
    out = []
    for x in r.json()["release_dates"]:
        if x["release_id"] not in FRED_RELEASES:
            continue
        title, (h, m), key = FRED_RELEASES[x["release_id"]]
        d = date.fromisoformat(x["date"])
        dt = datetime(d.year, d.month, d.day, h, m, tzinfo=ET_TZ).astimezone(KST)
        if start <= dt.date() <= end:
            out.append({"date": dt.date(), "country": "US", "kind": "event",
                        "title": title, "time": kst_time_label(dt), "key": key})
    return out


def next_weekday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def kr_trade_stats(start, end):
    """정기 수출입 통계: 매월 1일 수출입동향(산업부), 21일 1~20일 수출입(관세청). 주말이면 다음 평일."""
    out = []
    for y, mo in {(start.year, start.month), (end.year, end.month)}:
        for day, title, t in ((1, f"{mo - 1 or 12}월 수출입동향 (산업부)", "오전 9시"),
                              (21, f"{mo}월 1~20일 수출입 (관세청)", "")):
            d = next_weekday(date(y, mo, day))
            if start <= d <= end:
                out.append({"date": d, "country": "KR", "kind": "event", "title": title, "time": t})
    return out


def static_events(start, end):
    data = json.loads((ROOT / "events.json").read_text())
    out = []
    for e in data["events"]:
        d = date.fromisoformat(e["date"])
        if start <= d <= end:
            out.append({"date": d, "country": e["country"], "kind": "event",
                        "title": e["title"], "time": e.get("time", ""), "key": e.get("key", False)})
    return out


# ---------------------------------------------------------------- 정리·렌더링

def build_rows(items):
    """날짜별로 묶고, 같은 날·같은 시간대 실적은 '테슬라·메타 실적'처럼 한 줄로 합친다."""
    days = defaultdict(list)
    earn = defaultdict(list)
    for it in items:
        if it["kind"] == "earn":
            earn[(it["date"], it["country"], it["time"])].append(it)
        else:
            days[it["date"]].append(it)
    for (d, c, t), group in earn.items():
        group.sort(key=lambda g: g["rank"])
        days[d].append({"country": c, "kind": "event", "time": t, "key": False,
                        "title": "·".join(g["name"] for g in group) + " 실적",
                        "order": group[0]["rank"]})

    def sort_key(e):
        return (not e.get("key"), e["country"] != "KR", e.get("order", -1))
    return {d: sorted(v, key=sort_key) for d, v in sorted(days.items())}


FLAG = {"KR": "🇰🇷", "US": "🇺🇸", "CN": "🇨🇳", "JP": "🇯🇵", "EU": "🇪🇺"}


def render_html(start, end, rows):
    cards = []
    for d, events in rows.items():
        hot = any(e.get("key") for e in events)
        lines = []
        for e in events:
            badge = '<span class="badge">★ 핵심</span>' if e.get("key") else ""
            t = f'<span class="t">{html.escape(e["time"])}</span>' if e["time"] else ""
            lines.append(f'<div class="ev"><span class="flag">{FLAG.get(e["country"], "🌐")}</span>'
                         f'<span class="ti">{html.escape(e["title"])}</span>{t}{badge}</div>')
        cards.append(f'''<div class="card{' hot' if hot else ''}">
  <div class="day"><div class="dn">{d.day}</div><div class="dw">{WEEKDAYS[d.weekday()]}</div></div>
  <div class="evs">{''.join(lines)}</div></div>''')
    if not cards:
        cards.append('<div class="card"><div class="evs"><div class="ev">이번 주는 큰 일정이 없어요</div></div></div>')

    return f'''<!doctype html><html><head><meta charset="utf-8">
<link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{width:540px;background:#fdf6ef;font-family:Pretendard,-apple-system,sans-serif;color:#2b2622;padding:44px 28px 30px}}
.head{{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:28px}}
h1{{font-size:32px;font-weight:900;letter-spacing:-1px}}
.range{{text-align:right;font-size:19px;font-weight:800;letter-spacing:-.5px}}
.card{{display:flex;background:#fff;border:1.5px solid #f0e6dc;border-radius:14px;padding:14px 16px;margin-bottom:10px}}
.card.hot{{background:#fdf3c8;border-color:#f3d76b}}
.day{{width:54px;flex:none;text-align:center;border-right:1.5px solid #f0e6dc;margin-right:14px;padding-right:12px;display:flex;flex-direction:column;justify-content:center}}
.dn{{font-size:26px;font-weight:900;line-height:1}}
.hot .dn{{color:#e8774a}}
.dw{{font-size:12px;color:#9b928a;margin-top:6px}}
.evs{{flex:1;display:flex;flex-direction:column;justify-content:center;gap:7px}}
.ev{{font-size:15.5px;font-weight:700;letter-spacing:-.4px;line-height:1.35}}
.flag{{margin-right:7px}}
.t{{font-size:11.5px;color:#a49a91;font-weight:600;margin-left:6px;white-space:nowrap}}
.badge{{display:inline-block;background:#2b2622;color:#f7c948;font-size:10.5px;font-weight:800;border-radius:9px;padding:2px 8px;margin-left:6px;vertical-align:2px}}
.foot{{display:flex;justify-content:space-between;font-size:10.5px;color:#a49a91;margin-top:18px}}
</style></head><body>
<div class="head"><h1>이번 주 증시 캘린더</h1>
<div class="range">{start.month}.{start.day} – {end.month}.{end.day}</div></div>
{''.join(cards)}
<div class="foot"><span>한국 시간 기준 · 일정은 바뀔 수 있어요 · 투자 권유 아님</span><span>미·한 시총 상위 30</span></div>
</body></html>'''


def screenshot(html_text, png_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:
            browser = p.chromium.launch(channel="chrome")  # 로컬 Mac: 설치된 크롬 사용
        page = browser.new_page(viewport={"width": 540, "height": 800}, device_scale_factor=2)
        page.set_content(html_text, wait_until="networkidle")
        page.screenshot(path=str(png_path), full_page=True)
        browser.close()


def send_telegram(png_path, caption):
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    with open(png_path, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                          data={"chat_id": chat, "caption": caption}, files={"photo": f}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"텔레그램 발송 실패: {r.status_code} {r.text[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="주 시작일(월요일) YYYY-MM-DD. 생략하면 이번 주")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 발송 없이 이미지만 저장")
    args = ap.parse_args()
    load_dotenv()

    today = datetime.now(KST).date()
    start = date.fromisoformat(args.week) if args.week else today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)

    items, errors = [], []

    def collect(label, fn, *a):
        try:
            got = fn(*a)
            print(f"[ok] {label}: {len(got) if hasattr(got, '__len__') else got}")
            return got
        except Exception as e:  # 한 소스가 실패해도 나머지로 발송
            print(f"[실패] {label}: {e}", file=sys.stderr)
            errors.append(label)
            return []

    us_top = collect("미국 시총 상위30", us_top30)
    kr_top = collect("한국 시총 상위30", kr_top30)
    if us_top:
        items += collect("미국 실적", us_earnings, start, end, us_top)
    if kr_top:
        items += collect("한국 실적(DART)", kr_earnings, start, end, kr_top)
    items += collect("한국 신규상장", kr_listings, start, end)
    items += collect("미국 경제지표", us_macro, start, end)
    items += collect("한국 수출입", kr_trade_stats, start, end)
    items += collect("고정 일정", static_events, start, end)

    rows = build_rows(items)
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    html_text = render_html(start, end, rows)
    (out / f"calendar_{start}.html").write_text(html_text)
    png = out / f"calendar_{start}.png"
    screenshot(html_text, png)
    print(f"이미지 저장: {png}")

    caption = f"📅 이번 주 증시 캘린더 ({start.month}.{start.day}–{end.month}.{end.day})"
    if errors:
        caption += "\n⚠️ 못 가져온 항목: " + ", ".join(errors)
    if not args.dry_run:
        send_telegram(png, caption)
        print("텔레그램 발송 완료")


if __name__ == "__main__":
    main()
