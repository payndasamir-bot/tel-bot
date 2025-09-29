#!/usr/bin/env python3
import os, sys, json, argparse, datetime, time
import requests
from html import escape

# -----------------------------------
# Config / constants
# -----------------------------------
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

# -----------------------------------
# Helpers
# -----------------------------------
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

def fetch_feed():
    """Stáhne JSON feed s hlavičkami + jednoduchý retry (řeší 403)."""
    urls = [PRIMARY_URL] + ALT_URLS
    last_err = None
    for url in urls:
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                if r.status_code >= 400:
                    raise requests.HTTPError(f"{r.status_code} {r.reason}")
                return r.json()
            except Exception as e:
                last_err = e
                wait = 1 + attempt  # 1s, 2s, 3s
                print(f"fetch attempt {attempt+1} for {url} failed: {e}; retry in {wait}s")
                time.sleep(wait)
    raise last_err

def fmt(ts):
    return datetime.datetime.utcfromtimestamp(int(ts))

def send_telegram(text: str):
    """Bezpečné odeslání HTML zprávy do Telegramu (+log odpovědi)."""
    if not BOT_TOKEN or not CHAT_ID:
        print("DEBUG: TELEGRAM env missing; skip send.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(CHAT_ID),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",   # musí být řetězec
    }
    try:
        r = requests.post(url, data=payload, timeout=20)
        print("Telegram HTTP:", r.status_code, r.text[:300])
    except Exception as e:
        print("Telegram exception:", e)

# -----------------------------------
# Main
# -----------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=os.getenv("PAIRS", "EURUSD,USDJPY"))
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided."); sys.exit(2)

    target = pairs_to_currencies(pairs)  # {'EUR','USD','JPY'}
    print("Target currencies:", sorted(target))

    try:
        feed = fetch_feed()
    except Exception as e:
        msg = f"❗️Calendar fetch error: {e}"
        print(msg)
        send_telegram(msg)
        sys.exit(2)

    print("Feed items:", len(feed))

    seen = load_seen()
    now_utc = datetime.datetime.utcnow()
    today = now_utc.date()

    published = []  # dnes s 'actual'
    upcoming  = []  # dnes bez 'actual' a čas >= teď
    total_rel = 0

    for ev in feed:
        cur = (ev.get("country") or "").upper()
        if cur not in target:
            continue
        total_rel += 1

        ts = ev.get("timestamp")
        if not ts:
            continue
        dt = fmt(ts)

        # --- RAW hodnoty z feedu ---
        title_raw    = (ev.get("title") or "").strip()
        actual_raw   = str(ev.get("actual") or "").strip()
        forecast_raw = str(ev.get("forecast") or "").strip()
        previous_raw = str(ev.get("previous") or "").strip()
        impact_raw   = str(ev.get("impact") or "").strip()

        # --- escaped pro HTML (do zprávy) ---
        title    = escape(title_raw)
        actual   = escape(actual_raw)
        forecast = escape(forecast_raw)
        previous = escape(previous_raw)
        impact   = escape(impact_raw)
        cur_disp = escape(cur)

        if dt.date() != today:
            continue

        # POZOR: key tvoříme z RAW hodnot (kvůli deduplikaci)
        key = f"{cur}|{title_raw}|{ts}|{actual_raw}"

        if actual_raw and key not in seen:
            published.append(
                f"• {dt.strftime('%H:%M')} <b>{cur_disp}</b> {title} — "
                f"Actual: <b>{actual}</b> | Fcst: {forecast} | Prev: {previous} (Impact: {impact})"
            )
            seen.add(key)

        elif not actual_raw and dt >= now_utc:
            line = f"• {dt.strftime('%H:%M')} <b>{cur_disp}</b> {title}"
            if forecast:
                line += f" (Fcst: {forecast})"
            upcoming.append(line)

    # --- Sestavení zprávy (mimo smyčku) ---
    lines = [
        f"🔎 <b>Fundament souhrn (EUR/USD/JPY)</b>",
        f"Feed items: <code>{len(feed)}</code> | Relevant (EUR/USD/JPY): <code>{total_rel}</code>",
        f"Dnes zveřejněno: <code>{len(published)}</code> | Dnes ještě přijde: <code>{len(upcoming)}</code>",
    ]

    if published:
        lines.append("\n📢 <b>Zveřejněno dnes</b>")
        lines.extend(published[:20])
        if len(published) > 20:
            lines.append(f"… a dalších {len(published)-20}")

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


