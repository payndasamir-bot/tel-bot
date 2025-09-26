import requests
from bs4 import BeautifulSoup
import datetime
import os
import json

# Načtení proměnných z GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Soubor, kde sledujeme už odeslané události
SEEN_FILE = "seen.json"

def send_telegram_message(text: str):
    """Pošle zprávu do Telegramu."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Error sending message:", e)

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return json.load(f)
    return []

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)

def fetch_calendar(day="today"):
    """Stáhne kalendář z ForexFactory pro today nebo tomorrow."""
    url = f"https://www.forexfactory.com/calendar?day={day}"
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
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

        if event:
            events.append({
                "time": time.get_text(strip=True) if time else "",
                "currency": currency.get_text(strip=True) if currency else "",
                "impact": impact.get("title") if impact else "",
                "event": event.get_text(strip=True),
                "actual": actual.get_text(strip=True) if actual else "",
                "forecast": forecast.get_text(strip=True) if forecast else "",
                "previous": previous.get_text(strip=True) if previous else "",
            })
    return events

def analyze_event(ev):
    """Základní komentář podle typu události."""
    text = ""
    if "CPI" in ev["event"] or "Inflation" in ev["event"]:
        text = "📊 Inflace: vyšší než očekávání = silnější měna, slabší zlato."
    elif "GDP" in ev["event"]:
        text = "📈 HDP: vyšší než očekávání = silnější měna."
    elif "Unemployment" in ev["event"] or "Labor" in ev["event"]:
        text = "👷‍♂️ Trh práce: nižší nezaměstnanost = silnější měna."
    elif "Retail" in ev["event"]:
        text = "🛍️ Maloobchodní tržby: vyšší spotřeba = růst měny."
    return text

def main():
    today = datetime.date.today().strftime("%Y-%m-%d")
    seen = load_seen()

    # Dnešní události
    events = fetch_calendar("today")
    for ev in events:
        if ev["actual"] and ev["event"] not in seen:
            msg = f"📢 <b>{ev['currency']}</b> {ev['event']}\n" \
                  f"🕒 {ev['time']}\n" \
                  f"Actual: {ev['actual']} | Forecast: {ev['forecast']} | Previous: {ev['previous']}\n" \
                  f"{analyze_event(ev)}"
            send_telegram_message(msg)
            seen.append(ev["event"])

    save_seen(seen)

    # Večer pošleme zítřejší přehled
    now = datetime.datetime.now().strftime("%H:%M")
    if now >= "20:00":
        tomorrow_events = fetch_calendar("tomorrow")
        if tomorrow_events:
            msg = "📅 <b>Zítřejší události:</b>\n"
            for ev in tomorrow_events:
                msg += f"- {ev['time']} {ev['currency']} {ev['event']} (Forecast: {ev['forecast']})\n"
            send_telegram_message(msg)

if __name__ == "__main__":
    main()

