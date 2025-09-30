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

PRIMARY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
ALT_URLS = [
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
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

# Lokální časová zóna (z Actions posíláme TZ=Europe/Prague)
TZ_NAME = os.getenv("TZ", "Europe/Prague")
TZ_LOCAL = ZoneInfo(TZ_NAME)

# ------------------ Helpers ------------------
def pairs_to_currencies(pairs_list):
    """EURUSD,USDJPY -> {'EUR','USD','JPY'}"""
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

# ---------- JSON feed (primary) ----------
def fetch_feed_json():
    """Stáhne JSON feed s hlavičkami + retry (řeší 403)"""
    urls = [PRIMARY_URL] + ALT_URLS
    last_err = None
    for url in urls:
        for attempt in range(3):
            try:
                r = requests.get(
                    url,
                    headers=HEADERS,
                    params={"_": int(time.time())},  # cache buster
                    timeout=20,
                )
                if r.status_code >= 400:
                    raise requests.HTTPError(f"{r.status_code} {r.reason}")
                return r.json()
            except Exception as e:
                last_err = e
                wait = 1 + attempt
                print(f"fetch attempt {attempt+1} for {url} failed: {e}; retry in {wait}s")
                time.sleep(wait)
    raise last_err

def to_local(ts: int) -> datetime.datetime:
    """Timestamp (UTC) -> lokální aware datetime."""
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).astimezone(TZ_LOCAL)

# ---------- HTML fallback (when JSON blocked) ----------
def fetch_today_html_events():
    """Scrape ForexFactory calendar ?day=today (fallback). Časy jsou dle FF stránky (bez přesného TZ)."""
    url = "https://www.forexfactory.com/calendar?day=today"
    r = requests.get(url, headers=HTML_HEADERS, timeout=25)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.reason}")

    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("tr.calendar__row")

    events = []
    for row in rows:
        time_el = row.select_one(".calendar__time")
        cur_el  = row.select_one(".calendar__currency")
        ev_el   = row.select_one(".calendar__event")
        imp_el  = row.select_one(".impact")
        act_el  = row.select_one(".calendar__actual")
        fc_el   = row.select_one(".calendar__forecast")
        prev_el = row.select_one(".calendar__previous")

        title = (ev_el.get_text(strip=True) if ev_el else "")
        if not title:
            continue

        events.append({
            "time_str": time_el.get_text(strip=True) if time_el else "",
            "cur": (cur_el.get_text(strip=True) if cur_el else "").upper(),
            "title": title,
            "impact": imp_el.get("title") if imp_el else "",
            "actual": act_el.get_text(strip=True) if act_el else "",
            "forecast": fc_el.get_text(strip=True) if fc_el else "",
            "previous": prev_el.get_text(strip=True) if prev_el else "",
        })
    return events

# ------------------ Main ------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=os.getenv("PAIRS", "EURUSD,USDJPY"))
    parser.add_argument("--lookback", type=int, default=int(os.getenv("LOOKBACK_DAYS", "7")),
                        help="Počet dnů zpětně pro souhrn (default 7).")
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided."); sys.exit(2)

    target = pairs_to_currencies(pairs)  # {'EUR','USD','JPY'}
    print("Target currencies:", sorted(target))

    # Lokální "teď" a okno souhrnu
    now_local   = datetime.datetime.now(TZ_LOCAL)
    today_local = now_local.date()
    lookback_days = max(1, int(args.lookback))
    from_date = today_local - datetime.timedelta(days=lookback_days)
    today_end = datetime.datetime.combine(today_local, datetime.time(23, 59, 59), tzinfo=TZ_LOCAL)

    # 1) Zkus JSON feed
    feed = None
    json_ok = False
    try:
        feed = fetch_feed_json()
        json_ok = True
    except Exception as e:
        print("JSON feed failed:", e)

    seen = load_seen()
    published = []
    upcoming  = []
    total_rel = 0

    if json_ok and isinstance(feed, list):
        print("Feed items:", len(feed))
        # --- zpracování JSON feedu (v lokálním čase) ---
        for ev in feed:
            cur = (ev.get("country") or "").upper()
            if cur not in target:
                continue

            ts = ev.get("timestamp")
            if not ts:
                continue
            dt = to_local(ts)  # UTC -> lokální

            # filtr: posledních X dní (včetně dneška)
            if not (from_date <= dt.date() <= today_local):
                continue

            total_rel += 1

            title_raw    = (ev.get("title") or "").strip()
            actual_raw   = str(ev.get("actual") or "").strip()
            forecast_raw = str(ev.get("forecast") or "").strip()
            previous_raw = str(ev.get("previous") or "").strip()
            impact_raw   = str(ev.get("impact") or "").strip()

            title    = escape(title_raw)
            actual   = escape(actual_raw)
            forecast = escape(forecast_raw)
            previous = escape(previous_raw)
            impact   = escape(impact_raw)
            cur_disp = escape(cur)

            is_actual = actual_raw not in {"", "-", "—", "N/A", "na", "NaN"}

            key = f"{cur}|{title_raw}|{ts}|{actual_raw}"

            if is_actual and key not in seen:
                published.append(
                    f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title} — "
                    f"Actual: <b>{actual}</b> | Fcst: {forecast} | Prev: {previous} (Impact: {impact})"
                )
                seen.add(key)

            elif (not is_actual) and (dt >= now_local) and (dt <= today_end):
                line = f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title}"
                if forecast:
                    line += f" (Fcst: {forecast})"
                upcoming.append(line)

        prefix = "🔎 <b>Fundament souhrn (EUR/USD/JPY)</b>"
        window_text = f"{from_date.strftime('%Y-%m-%d')} → {today_local.strftime('%Y-%m-%d')}"
    else:
        # 2) Fallback: HTML scraping „today“ (bez přesného TZ – orientačně, pouze dnešek)
        try:
            html_events = fetch_today_html_events()
            for ev in html_events:
                cur = (ev["cur"] or "").upper()
                if cur not in target:
                    continue

                title_raw    = ev["title"]
                actual_raw   = ev["actual"]
                forecast_raw = ev["forecast"]
                previous_raw = ev["previous"]
                impact_raw   = ev["impact"]

                title    = escape(title_raw)
                actual   = escape(actual_raw)
                forecast = escape(forecast_raw)
                previous = escape(previous_raw)
                impact   = escape(impact_raw)
                cur_disp = escape(cur)

                # HTML fallback nemá přesné datum – bereme jen dnešek
                is_actual = actual_raw not in {"", "-", "—", "N/A", "na", "NaN"}
                tstr = ev["time_str"] or "—"

                key = f"HTML|{cur}|{title_raw}|{tstr}|{actual_raw}"

                if is_actual and key not in seen:
                    published.append(
                        f"• {today_local} {tstr} <b>{cur_disp}</b> {title} — "
                        f"Actual: <b>{actual}</b> | Fcst: {forecast} | Prev: {previous} (Impact: {impact})"
                    )
                    seen.add(key)
                elif not is_actual:
                    line = f"• {today_local} {tstr} <b>{cur_disp}</b> {title}"
                    if forecast:
                        line += f" (Fcst: {forecast})"
                    upcoming.append(line)

            total_rel = len(published) + len(upcoming)
            prefix = "🔎 <b>Fundament souhrn (EUR/USD/JPY) — fallback HTML</b>"
            window_text = f"{today_local.strftime('%Y-%m-%d')} (dnešek)"
            print(f"HTML fallback events: {total_rel}")
        except Exception as e:
            msg = f"❗️Calendar fetch error (both JSON & HTML): {e}"
            print(msg)
            send_telegram(msg)
            sys.exit(2)

    # --- Sestavení zprávy (mimo smyčku) ---
    lines = [
        prefix,
        f"Období: <code>{window_text}</code>",
        f"Feed items: <code>{len(feed) if json_ok else 'n/a'}</code> | Relevant (EUR/USD/JPY): <code>{total_rel}</code>",
        f"Zveřejněno v období: <code>{len(published)}</code> | Ještě přijde dnes: <code>{len(upcoming)}</code>",
    ]

    if published:
        lines.append("\n📢 <b>Zveřejněno</b>")
        lines.extend(published[:25])
        if len(published) > 25:
            lines.append(f"… a dalších {len(published)-25}")

    if upcoming:
        lines.append("\n⏳ <b>Dnes ještě přijde</b>")
        lines.extend(upcoming[:20])
        if len(upcoming) > 20:
            lines.append(f"… a dalších {len(upcoming)-20}")

    send_telegram("\n".join(lines))
    save_seen(seen)
    sys.exit(0)

if __name__ == "__main__":
    main()


