#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
import datetime
import os
import json
import sys

# ---- Nastavení ----
# Filtrujeme jen měny relevantní pro EURUSD a USDJPY:
RELEVANT_CURRENCIES = {"USD", "EUR", "JPY"}

# Cesta k "seen.json" v kořeni repa (složka data/)
SEEN_FILE = os.path.join("data", "seen.json")

# Načtení proměnných z GitHub Secrets
# Zachovám tvoje názvy, ale umím i alternativu (kdyby se v env jmenovaly jinak)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

# ---- Pomocné funkce ----
def send_telegram_message(text: str):
    """Pošle zprávu do Telegramu (bez pádu na chybě)."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in env (skip send).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=20)
        print("Telegram status:", r.status_code, r.text[:200])
    except Exception as e:
        print("Error sending message:", e)

def load_seen():
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print("load_seen error:", e)
    return []

def save_seen(seen):
    try:
        os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("save_seen error:", e)

def fetch_calendar(day="today"):
    """Stáhne kalendář z ForexFactory pro 'today' nebo 'tomorrow'."""
    url = f"https://www.forexfactory.com/calendar?day={day}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("tr.calendar__row")
    events = []
    for row in rows:
        time = row.select_one(".calendar__time")
        currency = row.select_one(".calendar__currency")
        impact = row.select_one(".impact")
        event = row.select_one(".calendar__event")
        actual = row.select_one(".calendar__actual")
        forecast = row.select_one(".calendar__forecast")
        previous = row.select_one(".calendar__previous")

        cur = currency.get_text(strip=True) if currency else ""
        evt_name = event.get_text(strip=True) if event else ""

        if evt_name:
            events.append({
                "time": time.get_text(strip=True) if time else "",
                "currency": cur,
                "impact": impact.get("title") if impact else "",
                "event": evt_name,
                "actual": (actual.get_text(strip=True) if actual else ""),
                "forecast": (forecast.get_text(strip=True) if forecast else ""),
                "previous": (previous.get_text(strip=True) if previous else ""),
            })
    return events

def analyze_event(ev):
    """Základní komentář podle typu události."""
    name = ev["event"].lower()
    if "cpi" in name or "inflation" in name:
        return "📊 Inflace: vyšší než forecast = silnější měna, často tlak na pokles zlata."
    if "gdp" in name:
        return "📈 HDP: vyšší než forecast = silnější měna."
    if "unemployment" in name or "labor" in name or "employment" in name or "jobs" in name:
        return "👷 Trh práce: nižší nezaměstnanost = silnější měna."
    if "retail" in name:
        return "🛍️ Maloobchodní tržby: vyšší spotřeba = silnější měna."
    return ""

# ---- Hlavní běh ----
def main():
    seen = load_seen()
    sent_any = False

    # 1) Dnešní události – pošleme jen USD/EUR/JPY (relevantní pro EURUSD & USDJPY)
    try:
        events = fetch_calendar("today")
    except Exception as e:
        print("fetch today error:", e)
        events = []

    for ev in events:
        if ev["currency"] not in RELEVANT_CURRENCIES:
            continue
        # posílej jen s "Actual" (po zveřejnění) a jen jednou
        key = f"{ev['currency']}|{ev['event']}|{ev['time']}|{ev['actual']}"
        if ev["actual"] and key not in seen:
            msg = (
                f"📢 <b>{ev['currency']}</b> {ev['event']}\n"
                f"🕒 {ev['time']}\n"
                f"Actual: <b>{ev['actual']}</b> | Forecast: {ev['forecast']} | Previous: {ev['previous']}\n"
                f"{analyze_event(ev)}"
            ).strip()
            send_telegram_message(msg)
            seen.append(key)
            sent_any = True

    save_seen(seen)

    # 2) Večer pošli zítřejší přehled (jen USD/EUR/JPY)
    now_hm = datetime.datetime.now().strftime("%H:%M")
    if now_hm >= "20:00":
        try:
            tomorrow_events = fetch_calendar("tomorrow")
        except Exception as e:
            print("fetch tomorrow error:", e)
            tomorrow_events = []

        rel = [ev for ev in tomorrow_events if ev["currency"] in RELEVANT_CURRENCIES]
        if rel:
            lines = ["📅 <b>Zítřejší události (EUR, USD, JPY):</b>"]
            for ev in rel:
                line = f"- {ev['time']} {ev['currency']} {ev['event']}"
                if ev["forecast"]:
                    line += f" (Forecast: {ev['forecast']})"
                lines.append(line)
            send_telegram_message("\n".join(lines))
            sent_any = True

    # Nikdy neshazuj workflow – vrať 0 i když nic není
    if sent_any:
        sys.exit(0)
    else:
        # 2 = 'žádná nová data' (náš workflow to bere jako OK)
        sys.exit(2)

if __name__ == "__main__":
    main()
