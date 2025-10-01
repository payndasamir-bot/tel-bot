#!/usr/bin/env python3
import os, sys, json, argparse, datetime, time
import requests
from html import escape
from zoneinfo import ZoneInfo
FORCE_PROBE = False  # <- DOČASNĚ: po ověření přepni na False nebo řádek smaž

# ============ Konfigurace ============

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")   or os.getenv("TG_CHAT_ID")

TZ_NAME  = os.getenv("TZ", "Europe/Prague")
TZ_LOCAL = ZoneInfo(TZ_NAME)

# dva feedy (tento a minulý týden); zkoušíme 2 hosty kvůli blokaci/ výpadkům
FEED_PATHS = [
    "ff_calendar_thisweek.json",
    "ff_calendar_lastweek.json",
]
FEED_HOSTS = [
    "https://nfs.faireconomy.media/",
    "https://cdn-nfs.faireconomy.media/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.forexfactory.com/calendar",
    "Cache-Control": "no-cache",
}

# ============ Pomocné funkce ============

def to_local(ts: int) -> datetime.datetime:
    """UTC timestamp -> lokalizovaný datetime."""
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).astimezone(TZ_LOCAL)

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

def fetch_json_from_hosts(path: str):
    """Zkusí stáhnout JSON z více hostů (s retries) a vrátí list nebo []"""
    last_err = None
    for host in FEED_HOSTS:
        url = host.rstrip("/") + "/" + path.lstrip("/")
        for attempt in range(3):
            try:
                r = requests.get(
                    url,
                    headers=HEADERS,
                    params={"_": int(time.time())},  # cache-buster
                    timeout=20,
                )
                if r.status_code >= 400:
                    raise requests.HTTPError(f"{r.status_code} {r.reason}")
                data = r.json()
                if isinstance(data, list):
                    return data
                return []
            except Exception as e:
                last_err = e
                wait = 1 + attempt
                print(f"WARN: {e} (url={url}); retry in {wait}s")
                time.sleep(wait)
    print(f"WARN: failed all hosts for {path}: {last_err}")
    return []

# ============ Hlavní logika ============

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=os.getenv("PAIRS", "EURUSD,USDJPY"))
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided.")
        sys.exit(2)

    target = pairs_to_currencies(pairs)  # např. {'EUR','USD','JPY'}
    print("Cílové měny:", sorted(target))

     # --- časové okno ---
     LOOKBACK_DAYS = 30       # dočasně zvětšeno pro test
     AHEAD_HOURS   = 168      # dočasně zvětšeno na týden dopředu

     now_local   = datetime.datetime.now(TZ_LOCAL)
     today_local = now_local.date()
     from_date   = today_local - datetime.timedelta(days=LOOKBACK_DAYS)
     horizon_end = now_local + datetime.timedelta(hours=AHEAD_HOURS)

    # ---- načtení a sloučení feedů ----
    feed_merged = []
    for path in FEED_PATHS:
        data = fetch_json_from_hosts(path)
        if isinstance(data, list):
            feed_merged.extend(data)

    print("Feed items merged:", len(feed_merged))
        # === PROBE mód: pošli syrové ukázky bez filtrů, ať vidíme, že data tečou ===
    if FORCE_PROBE:
        countries = {}
        examples = []
        for ev in feed_merged:
            cur = (ev.get("country") or "").upper()
            countries[cur] = countries.get(cur, 0) + 1

            ts = ev.get("timestamp")
            if ts:
                dt = to_local(ts)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            else:
                time_str = "—"

            if len(examples) < 10:  # pošli prvních 10 pro ochutnávku
                examples.append(
                    f"• {time_str} <b>{escape(cur)}</b> "
                    f"{escape((ev.get('title') or '').strip())} | "
                    f"act=<b>{escape(str(ev.get('actual') or '').strip())}</b> "
                    f"fcst={escape(str(ev.get('forecast') or '').strip())}"
                )

        # top 10 zemí podle počtu
        top_countries = sorted(countries.items(), key=lambda x: x[1], reverse=True)[:10]
        top_str = ", ".join([f"{c}:{n}" for c, n in top_countries]) if top_countries else "—"

        msg = [
            "🧪 <b>PROBE: syrový výpis z feedu</b>",
            f"Feedů sloučeno: <code>{len(feed_merged)}</code>",
            f"Počty podle zemí (Top10): {top_str}",
        ]
        if examples:
            msg.append("\n📋 <b>Příklady položek</b>")
            msg.extend(examples)
        else:
            msg.append("\n⚠️ Žádné položky k ukázce.")

        send_telegram("\n".join(msg))
        print("PROBE done, exiting early.")
        return
   
    

    # ---- zpracování ----
    occurred = []            # co proběhlo v lookback okně
    upcoming = []            # co přijde do 48 h
    relevant_in_window = 0   # počet uvnitř okna pro EUR/USD/JPY

    # debug počitadla
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

        dt = to_local(ts)  # lokální čas události

        # ukázkové položky, které spadly do týdenního okna (pro DEBUG)
        if from_date <= dt.date() <= today_local:
            dbg_window += 1
            if len(dbg_examples) < 5:
                dbg_examples.append(
                    f"{dt.strftime('%Y-%m-%d %H:%M')} {cur} {(ev.get('title') or '').strip()} "
                    f"| act='{str(ev.get('actual') or '').strip()}' fc='{str(ev.get('forecast') or '').strip()}'"
                )

        # --- datové sloupce ---
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

        # 1) Události, které už PROBĚHLY v lookback okně
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

        # 2) Události, které teprve přijdou v nejbližších 48 h
        elif now_local < dt <= horizon_end:
            line = f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title}"
            if forecast:
                line += f" (Fcst: {forecast})"
            upcoming.append(line)

    # ---- DEBUG výpis do logu Actions ----
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

    # ---- Sestavení zprávy ----
    header = "🔎 <b>Fundament souhrn (EUR/USD/JPY)</b>"
    meta   = [
        f"Období: {from_date} → {today_local}",
        f"Sloučený feed items: {len(feed_merged)}",
        f"Relevantních v období (EUR/USD/JPY): {relevant_in_window} | "
        f"Události v období: {len(occurred)} | Nejbližších 48 h: {len(upcoming)}"
    ]

    lines = [header] + meta

    if occurred:
        lines.append("\n📢 <b>Zveřejněno v období</b>")
        lines.extend(occurred[:25])
        if len(occurred) > 25:
            lines.append(f"… a dalších {len(occurred) - 25}")

    if upcoming:
        lines.append("\n⏳ <b>Ještě přijde (48 h)</b>")
        lines.extend(upcoming[:25])
        if len(upcoming) > 25:
            lines.append(f"… a dalších {len(upcoming) - 25}")

    if not occurred and not upcoming:
        lines.append("\n⚠️ V zadaném okně nebyly nalezeny žádné položky.")

    send_telegram("\n".join(lines))
    print("Hotovo.")
    sys.exit(0)

if __name__ == "__main__":
    main()

