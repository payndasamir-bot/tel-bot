#!/usr/bin/env python3
import os, sys, json, argparse, datetime, time
import requests
from html import escape
from zoneinfo import ZoneInfo

# =================== Nastavení ===================
LOOKBACK_DAYS   = 7           # kolik dnů zpětně vždy shrnout
UPCOMING_HOURS  = 48          # co přijde v nejbližších X hodinách

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")   or os.getenv("TG_CHAT_ID")

TZ_NAME   = os.getenv("TZ", "Europe/Prague")
TZ_LOCAL  = ZoneInfo(TZ_NAME)

# JSON feedy z ForexFactory (aktuální + minulý týden)
FEEDS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_lastweek.json",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.forexfactory.com/calendar",
    "Cache-Control": "no-cache",
}

# =================== Pomocné funkce ===================
def pairs_to_currencies(pairs_list):
    """EURUSD,USDJPY -> {'EUR','USD','JPY'}"""
    cur = set()
    for p in pairs_list:
        p = p.upper().strip()
        if len(p) == 6:
            cur.add(p[:3]); cur.add(p[3:])
    return cur

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

def fetch_json(url):
    """Stáhne JSON s retry a cache-busterem."""
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS,
                             params={"_": int(time.time())}, timeout=20)
            if r.status_code == 404:
                # ignore 404 (třeba lastweek není k dispozici)
                return []
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} {r.reason}")
            return r.json()
        except Exception as e:
            last_err = e
            wait = 1 + attempt
            print(f"WARN: fetch {url} (attempt {attempt+1}) failed: {e}; retry in {wait}s")
            time.sleep(wait)
    print(f"WARN: giving up {url}: {last_err}")
    return []

def to_local(ts: int) -> datetime.datetime:
    """UTC timestamp -> lokální aware datetime."""
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).astimezone(TZ_LOCAL)

def is_value_present(val: str) -> bool:
    """Je 'Actual' reálně vyplněný? Některé feedy posílají '-', '—', 'N/A' atd."""
    if val is None: return False
    v = str(val).strip()
    return v not in {"", "-", "—", "N/A", "na", "NaN"}

# =================== Hlavní logika ===================
def main():
    # ---- argumenty ----
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=os.getenv("PAIRS", "EURUSD,USDJPY"))
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided."); sys.exit(2)

    target = pairs_to_currencies(pairs)  # {'EUR','USD','JPY'}
    print("Target currencies:", sorted(target))

    # ---- časové okno ----
    now_local   = datetime.datetime.now(TZ_LOCAL)
    today_local = now_local.date()
    from_date   = today_local - datetime.timedelta(days=LOOKBACK_DAYS)
    upto_local  = now_local + datetime.timedelta(hours=UPCOMING_HOURS)

   # ---- načtení a sloučení feedů ----
feed_merged = []
for url in FEEDS:
    data = fetch_json(url)
    if isinstance(data, list):
        feed_merged.extend(data)

print("Feed items merged:", len(feed_merged))

# ---- DEBUG + sestavení výstupů ----
occurred = []            # to, co už proběhlo v lookback okně
upcoming = []            # co teprve přijde (dnes+zítra)
relevant_in_window = 0   # počet položek uvnitř okna

dbg_total = 0
dbg_cur = 0
dbg_window = 0
dbg_examples = []

for ev in feed_merged:
    dbg_total += 1

    cur = (ev.get("country") or "").upper()
    if cur not in target:
        continue
    dbg_cur += 1

    ts = ev.get("timestamp")
    if not ts:
        continue

    # lokální čas události
    dt = to_local(ts)

    # jen pro rychlou diagnostiku – pár ukázkových událostí, které spadají do okna
    if from_date <= dt.date() <= today_local:
        dbg_window += 1
        if len(dbg_examples) < 5:
            dbg_examples.append(
                f"{dt.strftime('%Y-%m-%d %H:%M')} {cur} {(ev.get('title') or '').strip()} "
                f"| act='{str(ev.get('actual') or '').strip()}' "
                f"fc='{str(ev.get('forecast') or '').strip()}'"
            )

    # ---- klasifikace: publication / upcoming ----
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

    # 1) Už proběhlo v lookback okně (bez podmínky na 'actual')
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

    # 2) Přijde dnes/zítra
    elif today_local <= dt.date() <= (today_local + datetime.timedelta(days=2)) and dt > now_local:
        line = f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title}"
        if forecast:
            line += f" (Fcst: {forecast})"
        upcoming.append(line)

# ---- DEBUG výpis (mimo smyčku!) ----
print(
    "DEBUG:",
    f"total={dbg_total}",
    f"in_currency={dbg_cur}",
    f"in_window={dbg_window}",
    f"from={from_date} to={today_local}",
    f"now={now_local.strftime('%Y-%m-%d %H:%M')}",
    sep="\n"
)
if dbg_examples:
    print("DEBUG examples (first matches in window):")
    for ex in dbg_examples:
        print("  -", ex)
else:
    print("DEBUG examples: none matched the window")

# ---- DÁL už pokračuje původní skládání zprávy ----
# (tj. následuje tvé `lines = [` a vše pod tím)

        # základní pole
        title_raw    = (ev.get("title") or "").strip()
        actual_raw   = None if ev.get("actual") is None else str(ev.get("actual")).strip()
        forecast_raw = None if ev.get("forecast") is None else str(ev.get("forecast")).strip()
        previous_raw = None if ev.get("previous") is None else str(ev.get("previous")).strip()
        impact_raw   = None if ev.get("impact") is None else str(ev.get("impact")).strip()

        title    = escape(title_raw)
        actual   = escape(actual_raw)   if actual_raw   else ""
        forecast = escape(forecast_raw) if forecast_raw else ""
        previous = escape(previous_raw) if previous_raw else ""
        impact   = escape(impact_raw)   if impact_raw   else ""
        cur_disp = escape(cur)

        # označ, že je to relevantní událost v našem lookback okně
        if from_date <= dt.date() <= today_local or (today_local <= dt.date() <= upto_local.date()):
            relevant_count += 1

        # 1) Proběhlé v lookback okně (NEvyžaduje vyplněné 'Actual')
        if from_date <= dt.date() <= today_local and dt <= now_local:
            line = f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title}"
            detail = []
            if is_value_present(actual):   detail.append(f"Actual: <b>{actual}</b>")
            if is_value_present(forecast): detail.append(f"Fcst: {forecast}")
            if is_value_present(previous): detail.append(f"Prev: {previous}")
            if is_value_present(impact):   detail.append(f"(Impact: {impact})")
            if detail:
                line += " — " + " | ".join(detail)
            occurred.append(line)
            continue

        # 2) Přijde do 48 hodin (budoucí vůči 'now')
        if now_local < dt <= upto_local:
            line = f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title}"
            if is_value_present(forecast):
                line += f" (Fcst: {forecast})"
            upcoming.append(line)

    # ---- sestavení zprávy ----
    header = (
        f"🔎 <b>Fundament souhrn (EUR/USD/JPY)</b>\n"
        f"Období: {from_date} → {today_local}\n"
        f"Sluočený feed items: {len(feed_merged)}\n"
        f"Relevantních v období (EUR/USD/JPY): {relevant_count}\n"
        f"Události v období: {len(occurred)} | Nejbližších {UPCOMING_HOURS} h: {len(upcoming)}"
    )

    lines = [header]

    if occurred:
        lines.append("\n📢 <b>Zveřejněno (posledních 7 dní)</b>")
        lines.extend(occurred[:25])  # omezíme délku
        if len(occurred) > 25:
            lines.append(f"… a dalších {len(occurred)-25}")

    if upcoming:
        lines.append(f"\n⏳ <b>Přijde do {UPCOMING_HOURS} h</b>")
        lines.extend(upcoming[:25])
        if len(upcoming) > 25:
            lines.append(f"… a dalších {len(upcoming)-25}")

    # když náhodou nic, ať to aspoň něco řekne
    if not occurred and not upcoming:
        lines.append("\n(⚠️ V zadaném okně nebyly nalezeny žádné položky.)")

    send_telegram("\n".join(lines))
    print("Hotovo.")

if __name__ == "__main__":
    main()

