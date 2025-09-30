#!/usr/bin/env python3
import os, sys, json, argparse, datetime, time
import requests
from html import escape
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

# ------------------ Config ------------------
SEEN_FILE = os.path.join("data", "seen.json")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")   or os.getenv("TG_CHAT_ID")

PRIMARY_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_lastweek.json",
]
ALT_URLS = [
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://cdn-nfs.faireconomy.media/ff_calendar_lastweek.json",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.forexfactory.com/calendar",
    "Cache-Control": "no-cache",
}

HTML_HEADERS = {
    **HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TZ_NAME  = os.getenv("TZ", "Europe/Prague")
TZ_LOCAL = ZoneInfo(TZ_NAME)

# ------------------ Helpers ------------------
def pairs_to_currencies(pairs_list):
    cur = set()
    for p in pairs_list:
        p = p.upper().strip()
        if len(p) == 6:
            cur.add(p[:3]); cur.add(p[3:])
    return cur

def load_seen():
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, ensure_ascii=False, indent=2)

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("DEBUG: TELEGRAM env missing; skip send.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(CHAT_ID),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    try:
        r = requests.post(url, data=payload, timeout=20)
        print("Telegram HTTP:", r.status_code, r.text[:300])
    except Exception as e:
        print("Telegram exception:", e)

def fetch_one(url):
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS,
                             params={"_": int(time.time())}, timeout=20)
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} {r.reason}")
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    raise last_err

def fetch_feeds_merged():
    feeds = []
    urls = PRIMARY_URLS + ALT_URLS
    ok = set()
    for url in urls:
        tag = url.rsplit("/", 1)[-1]
        if tag in ok:  # thisweek/lastweek stačí jednou
            continue
        try:
            data = fetch_one(url)
            if isinstance(data, list) and data:
                feeds.extend(data)
                ok.add(tag)
        except Exception as e:
            print("WARN:", e)
    return feeds

def to_local(ts: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(
        int(ts), datetime.timezone.utc
    ).astimezone(TZ_LOCAL)

def fetch_today_html_events():
    url = "https://www.forexfactory.com/calendar?day=today"
    r = requests.get(url, headers=HTML_HEADERS, timeout=25)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.reason}")
    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("tr.calendar__row")
    events = []
    for row in rows:
        tim = row.select_one(".calendar__time")
        cur = row.select_one(".calendar__currency")
        ev  = row.select_one(".calendar__event")
        imp = row.select_one(".impact")
        act = row.select_one(".calendar__actual")
        fc  = row.select_one(".calendar__forecast")
        prev= row.select_one(".calendar__previous")
        title = (ev.get_text(strip=True) if ev else "")
        if not title:
            continue
        events.append({
            "time_str": tim.get_text(strip=True) if tim else "",
            "cur": (cur.get_text(strip=True) if cur else "").upper(),
            "title": title,
            "impact": imp.get("title") if imp else "",
            "actual": act.get_text(strip=True) if act else "",
            "forecast": fc.get_text(strip=True) if fc else "",
            "previous": prev.get_text(strip=True) if prev else "",
        })
    return events

# ------------------ Main ------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=os.getenv("PAIRS", "EURUSD,USDJPY"))
    parser.add_argument("--lookback", type=int, default=int(os.getenv("LOOKBACK_DAYS", "7")),
                        help="Počet dnů zpětně pro souhrn (default 7)")
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided."); sys.exit(2)
    target = pairs_to_currencies(pairs)

    now_local   = datetime.datetime.now(TZ_LOCAL)
    today_local = now_local.date()
    lookback_days = max(1, int(args.lookback))
    from_date = today_local - datetime.timedelta(days=lookback_days)
    next_48h_end = now_local + datetime.timedelta(hours=48)

    # 1) JSON feeds (thisweek + lastweek)
    feed = []
    json_ok = False
    try:
        feed = fetch_feeds_merged()
        json_ok = len(feed) > 0
    except Exception as e:
        print("JSON feeds failed:", e)

    seen = load_seen()
    occurred = []          # co se v okně PŘIHLODILO (nezávisle na `actual`)
    upcoming  = []         # co přijde v nejbližších 48 h
    relevant_in_window = 0 # počet relevantních (EUR/USD/JPY) v okně

    if json_ok:
        print("Feed items merged:", len(feed))
        for ev in feed:
            cur = (ev.get("country") or "").upper()
            if cur not in target:
                continue

            ts = ev.get("timestamp")
            if not ts:
                continue
            dt = to_local(ts)

            title_raw    = (ev.get("title") or "").strip()
            actual_raw   = str(ev.get("actual") or "").strip()
            forecast_raw = str(ev.get("forecast") or "").strip()
            previous_raw = str(ev.get("previous") or "").strip()
            impact_raw   = str(ev.get("impact") or "").strip()

            title    = escape(title_raw)
            actual   = escape(actual_raw)   if actual_raw   else ""
            forecast = escape(forecast_raw) if forecast_raw else ""
            previous = escape(previous_raw) if previous_raw else ""
            impact   = escape(impact_raw)   if impact_raw   else ""
            cur_disp = escape(cur)

            # 1) Události, které už PROBĚHLY v lookback okně (bez podmínky na `actual`)
            if from_date <= dt.date() <= today_local and dt <= now_local:
                relevant_in_window += 1
                line = f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title}"
                detail = []
                if actual:   detail.append(f"Actual: <b>{actual}</b>")
                if forecast: detail.append(f"Fcst: {forecast}")
                if previous: detail.append(f"Prev: {previous}")
                if impact:   detail.append(f"(Impact: {impact})")
                if detail:
                    line += " — " + " | ".join(detail)
                occurred.append(line)

            # 2) Nejbližších 48 h (ještě se nestalo a je do 48h)
            elif now_local <= dt <= next_48h_end:
                line = f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title}"
                if forecast:
                    line += f" (Fcst: {forecast})"
                upcoming.append(line)

        occurred.sort()
        upcoming.sort()
        prefix = "🔎 <b>Fundament souhrn (EUR/USD/JPY)</b>"
        window_text = f"{from_date.strftime('%Y-%m-%d')} → {today_local.strftime('%Y-%m-%d')}"
        merged_count = len(feed)
    else:
        # 2) Fallback: HTML today (nouzově – aspoň dnešní výpis)
        try:
            html_events = fetch_today_html_events()
            for ev in html_events:
                cur = (ev["cur"] or "").upper()
                if cur not in target:
                    continue
                title_raw = ev["title"]
                actual = escape(ev["actual"]) if ev["actual"] else ""
                forecast = escape(ev["forecast"]) if ev["forecast"] else ""
                previous = escape(ev["previous"]) if ev["previous"] else ""
                impact   = escape(ev["impact"])   if ev["impact"]   else ""
                cur_disp = escape(cur)
                tstr = ev["time_str"] or "—"
                line = f"• {today_local} {tstr} <b>{cur_disp}</b> {escape(title_raw)}"
                detail = []
                if actual:   detail.append(f"Actual: <b>{actual}</b>")
                if forecast: detail.append(f"Fcst: {forecast}")
                if previous: detail.append(f"Prev: {previous}")
                if impact:   detail.append(f"(Impact: {impact})")
                if detail:
                    line += " — " + " | ".join(detail)
                occurred.append(line)

            prefix = "🔎 <b>Fundament souhrn (EUR/USD/JPY) — fallback HTML</b>"
            window_text = f"{today_local.strftime('%Y-%m-%d')} (dnešek)"
            merged_count = "n/a"
            relevant_in_window = len(occurred)
        except Exception as e:
            msg = f"❗️Calendar fetch error (both JSON & HTML): {e}"
            print(msg)
            send_telegram(msg)
            sys.exit(2)

    # --- Sestavení zprávy ---
    lines = [
        prefix,
        f"Období: <code>{window_text}</code>",
        f"Sloučený feed items: <code>{merged_count}</code>",
        f"Relevantních v období (EUR/USD/JPY): <code>{relevant_in_window}</code>",
        f"Události v období: <code>{len(occurred)}</code> | Nejbližších 48 h: <code>{len(upcoming)}</code>",
    ]

    if occurred:
        lines.append("\n📢 <b>Proběhlo v období</b>")
        lines.extend(occurred[:25])
        if len(occurred) > 25:
            lines.append(f"… a dalších {len(occurred)-25}")

    if upcoming:
        lines.append("\n⏳ <b>Nejbližších 48 h</b>")
        lines.extend(upcoming[:20])
        if len(upcoming) > 20:
            lines.append(f"… a dalších {len(upcoming)-20}")

    # i kdyby pořád nic – bude aspoň hlavička s nulami
    send_telegram("\n".join(lines))
    save_seen(seen)
    sys.exit(0)

if __name__ == "__main__":
    main()
