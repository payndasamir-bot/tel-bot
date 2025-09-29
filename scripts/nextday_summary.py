#!/usr/bin/env python3
import os, sys, json, argparse, datetime, urllib.request, urllib.parse

SEEN_FILE = os.path.join("data", "seen.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")   or os.getenv("TG_CHAT_ID")

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
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("DEBUG: TELEGRAM env missing; skip send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=20) as resp:
        print("Telegram HTTP:", resp.status)

def fetch_calendar_json():
    # Lehký JSON feed od FF (týdenní přehled)
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fmt_utc(ts):
    return datetime.datetime.utcfromtimestamp(int(ts))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=os.getenv("PAIRS", "EURUSD,USDJPY"))
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided."); sys.exit(2)

    target = pairs_to_currencies(pairs)  # např. {'EUR','USD','JPY'}
    print("Target currencies:", sorted(list(target)))

    try:
        feed = fetch_calendar_json()
    except Exception as e:
        print("Calendar fetch error:", e)
        sys.exit(2)

    seen = load_seen()
    today_utc = datetime.datetime.utcnow().date()
    tomorrow_utc = today_utc + datetime.timedelta(days=1)

    # --- Dnešní souhrn (jen události, které už mají 'actual') ---
    today_lines = []
    for ev in feed:
        cur = (ev.get("country") or "").upper()
        if cur not in target: 
            continue
        ts = ev.get("timestamp")
        dt = fmt_utc(ts)
        if dt.date() != today_utc:
            continue
        title = (ev.get("title") or "").strip()
        actual = str(ev.get("actual") or "").strip()
        forecast = str(ev.get("forecast") or "").strip()
        previous = str(ev.get("previous") or "").strip()
        impact = str(ev.get("impact") or "").strip()

        key = f"{cur}|{title}|{ts}|{actual}"
        if actual and key not in seen:
            line = f"• {dt.strftime('%H:%M')} <b>{cur}</b> {title} — Actual: <b>{actual}</b> | Fcst: {forecast} | Prev: {previous} (Impact: {impact})"
            today_lines.append(line)
            seen.add(key)

    sent_any = False
    if today_lines:
        body = "📢 <b>Dnešní fundamenty (EUR/USD/JPY)</b>\n" + "\n".join(today_lines)
        send_telegram(body)
        sent_any = True

    # --- Zítřejší přehled po 20:00 lokálního času (UTC+2/Prague) ---
    # V runneru použijeme UTC, takže spustíme náhled vždy (nevadí).
    now_hm = datetime.datetime.utcnow().strftime("%H:%M")
    if now_hm >= "18:00":  # ~20:00 Prague v létě
        tmrw_lines = []
        for ev in feed:
            cur = (ev.get("country") or "").upper()
            if cur not in target: 
                continue
            dt = fmt_utc(ev.get("timestamp"))
            if dt.date() != tomorrow_utc:
                continue
            title = (ev.get("title") or "").strip()
            fc = str(ev.get("forecast") or "").strip()
            line = f"• {dt.strftime('%H:%M')} <b>{cur}</b> {title}" + (f" (Fcst: {fc})" if fc else "")
            tmrw_lines.append(line)
        if tmrw_lines:
            body = "📅 <b>Zítřejší události (EUR/USD/JPY)</b>\n" + "\n".join(tmrw_lines)
            send_telegram(body)
            sent_any = True

    save_seen(seen)
    sys.exit(0 if sent_any else 2)

if __name__ == "__main__":
    main()
