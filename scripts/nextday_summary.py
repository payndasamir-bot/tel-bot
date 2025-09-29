#!/usr/bin/env python3
import os, sys, json, argparse, datetime, urllib.request, urllib.parse

SEEN_FILE = os.path.join("data", "seen.json")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")   or os.getenv("TG_CHAT_ID")

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

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
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=20) as resp:
        print("Telegram HTTP:", resp.status)

def fetch_feed():
    with urllib.request.urlopen(FEED_URL, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)

def fmt(ts):
    return datetime.datetime.utcfromtimestamp(int(ts))

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

    published = []
    upcoming = []
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
        title    = (ev.get("title") or "").strip()
        actual   = str(ev.get("actual") or "").strip()
        forecast = str(ev.get("forecast") or "").strip()
        previous = str(ev.get("previous") or "").strip()
        impact   = str(ev.get("impact") or "").strip()

        if dt.date() != today:
            continue

        key = f"{cur}|{title}|{ts}|{actual}"

        if actual and key not in seen:
            published.append(f"• {dt.strftime('%H:%M')} <b>{cur}</b> {title} — Actual: <b>{actual}</b> | Fcst: {forecast} | Prev: {previous} (Impact: {impact})")
            seen.add(key)
        elif not actual and dt >= now_utc:
            upcoming.append(f"• {dt.strftime('%H:%M')} <b>{cur}</b> {title}" + (f" (Fcst: {forecast})" if forecast else ""))

    # --- Diagnostická zpráva: pošleme VŽDY souhrn s čísly ---
    lines = [f"🔎 <b>Fundament souhrn (EUR/USD/JPY)</b>",
             f"Feed items: <code>{len(feed)}</code> | Relevant (EUR/USD/JPY): <code>{total_rel}</code>",
             f"Dnes zveřejněno: <code>{len(published)}</code> | Dnes ještě přijde: <code>{len(upcoming)}</code>"]

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

