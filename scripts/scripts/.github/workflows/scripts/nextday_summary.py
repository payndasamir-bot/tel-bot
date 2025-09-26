#!/usr/bin/env python3
import requests, os
from datetime import datetime, timedelta, timezone
import pytz

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")

def send(txt):
    requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                  data={"chat_id": CHAT, "text": txt, "parse_mode":"HTML"}, timeout=20)

def tiny_view(title):
    t = title.lower()
    if "cpi" in t or "pce" in t: return "Inflace: vyšší = USD↑, XAU↓; nižší = USD↓, XAU↑"
    if "gdp" in t: return "HDP: silnější = USD/akcie↑; slabší = USD/akcie↓"
    if "pmi" in t or "ism" in t: return "PMI: 50+ býčí, <50 medvědí"
    if "unemployment" in t: return "Nezaměstnanost: nižší = USD↑"
    if "payroll" in t: return "NFP: nad oček. = USD↑; pod = USD↓"
    if "retail sales" in t: return "Maloobchod: silnější = USD/akcie↑"
    if "rate" in t or "press conference" in t: return "Sazby: jestřábí = USD↑, holubičí = USD↓"
    return "Vliv podle překvapení vs. forecast."

def main():
    tz = pytz.timezone("Europe/Prague")
    tomorrow = (datetime.now(tz).date() + timedelta(days=1))
    evs = requests.get(FF_URL, timeout=25).json()
    picks = []
    for e in evs:
        imp = (e.get("impact") or "").lower()
        if "high" not in imp and "medium" not in imp: continue
        ts = e.get("timestamp")
        if not ts: continue
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(tz)
        if dt.date() == tomorrow:
            picks.append((dt, e))
    if not picks:
        send("🗓️ <b>Zítra</b>: žádné High/Medium události.")
        return
    picks.sort(key=lambda x: x[0])
    lines = ["🗓️ <b>Zítra – klíčové události (CET)</b>"]
    for dt, e in picks:
        title = e.get("title",""); country = e.get("country","")
        fc = e.get("forecast") or e.get("consensus") or "?"
        lines.append(f"• {dt.strftime('%H:%M')} {country} {title} | Fcst: {fc}")
        lines.append(f"  ↳ {tiny_view(title)}")
    send("\n".join(lines))

if __name__ == "__main__":
    main()
