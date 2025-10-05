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
    parser.add_argument(
        "--pairs", type=str, default=os.getenv("PAIRS", "EURUSD,USDJPY")
    )
    parser.add_argument(
        "--from", dest="from_date", type=str, default=None,
        help="Začátek období (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--to", dest="to_date", type=str, default=None,
        help="Konec období včetně (YYYY-MM-DD)"
    )
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided.")
        sys.exit(2)

    target = pairs_to_currencies(pairs)  # např. {'EUR','USD','JPY'}
    print("Cílové měny:", sorted(target))

     # --- časové okno ---
       # --- časové okno ---
    def _parse_date(s: str) -> datetime.date:
        return datetime.date.fromisoformat(s)

    LOOKBACK_DAYS = 7
    AHEAD_HOURS   = 48

    now_local   = datetime.datetime.now(TZ_LOCAL)
    today_local = now_local.date()

    if args.from_date and args.to_date:
        from_date = _parse_date(args.from_date)
        to_date   = _parse_date(args.to_date)
        if from_date > to_date:
            from_date, to_date = to_date, from_date
        horizon_end = datetime.datetime.combine(
            to_date, datetime.time(23, 59), tzinfo=TZ_LOCAL
        )
    else:
        from_date   = today_local - datetime.timedelta(days=LOOKBACK_DAYS)
        to_date     = today_local
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
   
    

       # ---- zpracování (bez časového filtru) ----
    from html import escape as _esc

    def _line(ev, show_time=True):
        cur = (ev.get("country") or "").upper()
        ts  = ev.get("timestamp")
        if ts:
            dt = to_local(ts)
            tstr = dt.strftime("%Y-%m-%d %H:%M")
        else:
            tstr = "—"

        title    = _esc((ev.get("title") or "").strip())
        actual   = _esc(str(ev.get("actual") or "").strip())
        forecast = _esc(str(ev.get("forecast") or "").strip())
        previous = _esc(str(ev.get("previous") or "").strip())
        impact   = _esc(str(ev.get("impact") or "").strip())
        cur_disp = _esc(cur)

        parts = []
        if show_time:
            parts.append(f"{tstr}")
        parts.append(f"<b>{cur_disp}</b> {title}")

        details = []
        if actual:
            details.append(f"Actual: <b>{actual}</b>")
        if forecast:
            details.append(f"Fcst: {forecast}")
        if previous:
            details.append(f"Prev: {previous}")
        if impact:
            details.append(f"(Impact: {impact})")

        if details:
            return "• " + " ".join(parts) + " — " + " | ".join(details)
        else:
            return "• " + " ".join(parts)

    # jen události pro zadané měny (EUR/USD/JPY apod.)
    relevant = []
    for ev in feed_merged:
        cur = (ev.get("country") or "").upper()
        if cur in target:
            relevant.append(ev)

    # rozdělení: „zveřejněno“ (má actual) vs „ještě nepřišlo“ (actual prázdné)
    published = []
    upcoming  = []
    for ev in relevant:
        has_actual = str(ev.get("actual") or "").strip() not in {"", "-", "—", "N/A", "na", "NaN"}
        if has_actual:
            published.append(ev)
        else:
            upcoming.append(ev)

    # seřaď (volitelné): zveřejněné a „čeká“ podle času (když ho mají)
    def _key(ev):
        try:
            return int(ev.get("timestamp") or 0)
        except Exception:
            return 0

    published.sort(key=_key, reverse=True)   # nejnovější nahoře
    upcoming.sort(key=_key)                  # nejdřív nejbližší

    # zpráva
    header = "🔎 <b>Fundament souhrn (EUR/USD/JPY)</b>"
    meta = [
        f"Sloučený feed items: {len(feed_merged)}",
        f"Relevantních (EUR/USD/JPY): {len(relevant)} | "
        f"Zveřejněno: {len(published)} | Bez 'actual' (ještě nepřišlo): {len(upcoming)}",
    ]

    lines = [header] + meta

    if published:
        lines.append("\n📢 <b>Zveřejněno</b>")
        lines.extend([_line(ev) for ev in published[:25]])
        if len(published) > 25:
            lines.append(f"… a dalších {len(published) - 25}")

    if upcoming:
        lines.append("\n⏳ <b>V kalendáři (bez 'actual')</b>")
        lines.extend([_line(ev) for ev in upcoming[:25]])
        if len(upcoming) > 25:
            lines.append(f"… a dalších {len(upcoming) - 25}")

    if not published and not upcoming:
        lines.append("\n⚠️ Ve feedu pro zadané měny nebyly nalezeny žádné položky.")

    send_telegram("\n".join(lines))
    print("Hotovo.")
    sys.exit(0)

if __name__ == "__main__":
    main()

