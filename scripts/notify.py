#!/usr/bin/env python3
# FF weekly JSON -> Telegram; posílá jen události s actual + High/Medium impact.
import requests, os, json, sys
from datetime import datetime, timezone
import pytz

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SEEN_FILE = "data/seen.json"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
if not BOT_TOKEN or not CHAT_ID:
    print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr); sys.exit(1)

def load_seen():
    try:
        with open(SEEN_FILE,"r",encoding="utf-8") as f: return set(json.load(f))
    except Exception: return set()

def save_seen(s):
    with open(SEEN_FILE,"w",encoding="utf-8") as f: json.dump(sorted(list(s)), f, ensure_ascii=False, indent=2)

def uid(e):
    return f"{e.get('date','')}|{e.get('time','')}|{e.get('country','')}|{e.get('title','')}"

def num(x):
    if x is None: return None
    try: return float(str(x).replace("%","").replace(",",".").strip())
    except: return None

def fmt(ts):
    if ts is None: return "?"
    try:
        if isinstance(ts,(int,float)): dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        else: dt = datetime.fromisoformat(str(ts))
        return dt.astimezone(pytz.timezone("Europe/Prague")).strftime("%Y-%m-%d %H:%M")
    except: return str(ts)

def send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode":"HTML"}, timeout=15)
    if r.status_code >= 300: print("Telegram send failed:", r.text, file=sys.stderr)

def main():
    seen = load_seen()
    events = requests.get(FF_URL, timeout=20).json()
    new_seen, sent = set(seen), 0
    for e in events:
        impact = (e.get("impact") or "").lower()
        if "high" not in impact and "medium" not in impact: continue
        uid_ = uid(e)
        if uid_ in seen: continue
        actual = e.get("actual")
        if not actual: continue  # posíláme až po vyhlášení
        title, country = e.get("title","UNKNOWN"), e.get("country","")
        t = fmt(e.get("timestamp") or e.get("date") or e.get("time"))
        forecast = e.get("forecast") or e.get("consensus") or ""
        previous = e.get("previous") or ""
        txt = f"⚠️ <b>{country} {title}</b> ({e.get('impact','')})\n⏱️ {t}\nActual: {actual} | Forecast: {forecast}"
        if previous: txt += f" | Prev: {previous}"
        an, fo = num(actual), num(forecast)
        if an is not None and fo is not None:
            if an > fo: txt += "\n→ Krátce: Vyšší než očekávání — býčí pro měnu / tlak na sazby."
            elif an < fo: txt += "\n→ Krátce: Nižší než očekávání — slabší pro měnu."
            else: txt += "\n→ Krátce: V souladu s očekáváním."
        else:
            txt += "\n→ Krátce: Nový údaj — zkontroluj detaily."
        send(txt); new_seen.add(uid_); sent += 1
    print(f"Sent {sent} messages." if sent else "No new releases with actual to send.")
    save_seen(new_seen)

if __name__ == "__main__":
    main()
