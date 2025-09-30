#!/usr/bin/env python3
import os, sys, json, argparse, datetime, time
import requests
from html import escape
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
from collections import defaultdict

# ================== KONFIG ==================
SEEN_FILE = os.path.join("data", "seen.json")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")   or os.getenv("TG_CHAT_ID")

PRIMARY_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_lastweek.json",
]
ALT_URLS = [
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://cdn-nfs.faireconomy.media/ff_calendar_lastweek.json",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.forexfactory.com/calendar",
    "Cache-Control": "no-cache",
}

HTML_HEADERS = {
    **HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TZ_NAME  = os.getenv("TZ", "Europe/Prague")
TZ_LOCAL = ZoneInfo(TZ_NAME)

# Klíčová slova pro tematický souhrn
THEMES = {
    "inflace": ["cpi", "inflation", "ppi", "core cpi", "pce"],
    "pmi": ["pmi", "manufacturing pmi", "services pmi"],
    "hdp": ["gdp", "gross domestic product"],
    "nezaměstnanost": ["unemployment", "jobless", "employment", "nonfarm payroll", "jolts", "initial jobless"],
    "maloobchod": ["retail sales"],
    "sazby/cb": ["rate decision", "interest rate", "central bank", "ecb", "fed", "boj", "boe", "snB", "nb", "press conference"],
    "průmysl": ["industrial production", "factory", "durable goods"],
    "bydlení": ["housing", "building permits", "new home", "existing home"],
}

IMPACT_WEIGHT = {
    "high": 3,
    "medium": 2,
    "low": 1,
}

# ================ UTIL ================
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

def fetch_one(url):
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, params={"_": int(time.time())}, timeout=20)
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} {r.reason}")
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    raise last_err

def fetch_feeds_merged():
    feeds = []
    ok_names = set()
    for url in PRIMARY_URLS + ALT_URLS:
        name = url.rsplit("/", 1)[-1]
        if name in ok_names:
            continue
        try:
            data = fetch_one(url)
            if isinstance(data, list) and data:
                feeds.extend(data); ok_names.add(name)
        except Exception as e:
            print("WARN:", e)
    return feeds

def to_local(ts: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).astimezone(TZ_LOCAL)

# =============== FALLBACK HTML (když JSON úplně padá) ===============
def fetch_today_html_events():
    url = "https://www.forexfactory.com/calendar?day=today"
    r = requests.get(url, headers=HTML_HEADERS, timeout=25)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.reason}")
    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("tr.calendar__row")
    out = []
    for row in rows:
        time_el = row.select_one(".calendar__time")
        cur_el  = row.select_one(".calendar__currency")
        ev_el   = row.select_one(".calendar__event")
        imp_el  = row.select_one(".impact")
        title = (ev_el.get_text(strip=True) if ev_el else "")
        if not title:
            continue
        out.append({
            "time_str": time_el.get_text(strip=True) if time_el else "",
            "cur": (cur_el.get_text(strip=True) if cur_el else "").upper(),
            "title": title,
            "impact": imp_el.get("title") if imp_el else "",
        })
    return out

# =============== MAIN ===============
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=os.getenv("PAIRS", "EURUSD,USDJPY"))
    parser.add_argument("--lookback", type=int, default=int(os.getenv("LOOKBACK_DAYS", "7")))
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided."); sys.exit(2)

    target = pairs_to_currencies(pairs)
    print("Target currencies:", sorted(target))

    now   = datetime.datetime.now(TZ_LOCAL)
    today = now.date()
    lookback_days = max(1, args.lookback)
    from_date = today - datetime.timedelta(days=lookback_days)
    next_48h_end = now + datetime.timedelta(hours=48)

    # ---- FEED ----
    feed = []
    json_ok = False
    try:
        feed = fetch_feeds_merged()
        json_ok = len(feed) > 0
    except Exception as e:
        print("JSON feeds failed:", e)

    # Sběr statistik
    seen = load_seen()
    per_currency = defaultdict(int)
    per_theme    = defaultdict(int)
    top_events   = []   # (score, dt, cur, title)
    upcoming     = []   # (dt, cur, title)

    def add_theme_counts(title: str):
        t = title.lower()
        for theme, keys in THEMES.items():
            if any(k in t for k in keys):
                per_theme[theme] += 1

    if json_ok:
        print("Merged items:", len(feed))
        for ev in feed:
            cur = (ev.get("country") or "").upper()
            if cur not in target:
                continue

            ts = ev.get("timestamp")
            if not ts:
                continue
            dt = to_local(ts)

            title_raw = (ev.get("title") or "").strip()
            if not title_raw:
                continue
            title = escape(title_raw)
            cur_disp = escape(cur)
            impact_raw = (ev.get("impact") or "").strip().lower()
            weight = IMPACT_WEIGHT.get(impact_raw, 1)

            # 1) lookback okno – vše počítáme (už bez nutnosti "actual")
            if from_date <= dt.date() <= today:
                per_currency[cur] += 1
                add_theme_counts(title_raw)

                # score pro "Top události"
                score = weight
                for keys in THEMES.values():
                    if any(k in title_raw.lower() for k in keys):
                        score += 1
                top_events.append((score, dt, cur_disp, title))

            # 2) nejbližších 48 h
            if now <= dt <= next_48h_end:
                upcoming.append((dt, cur_disp, title))
    else:
        # HTML fallback jen pro dnešek
        try:
            html = fetch_today_html_events()
            for ev in html:
                cur = (ev["cur"] or "").upper()
                if cur not in target: 
                    continue
                per_currency[cur] += 1
                title_raw = ev["title"]
                add_theme_counts(title_raw)
                title = escape(title_raw)
                cur_disp = escape(cur)
                # čas nemá TZ -> použijeme dnešní datum + řetězec času
                top_events.append((1, now, cur_disp, title))
                upcoming.append((now, cur_disp, title))
            print("HTML fallback used")
        except Exception as e:
            send_telegram(f"❗️Calendar fetch error (both JSON & HTML): {e}")
            sys.exit(2)

    # seřadit žebříčky
    top_events.sort(key=lambda x: (-x[0], x[1]))
    upcoming.sort(key=lambda x: x[0])

    # --- Kompozice zprávy ---
    window = f"{from_date.strftime('%Y-%m-%d')} → {today.strftime('%Y-%m-%d')}"
    lines = [
        "🔎 <b>Fundament souhrn (EUR/USD/JPY)</b>",
        f"Období: <code>{window}</code>",
        f"Sloučený feed items: <code>{len(feed) if json_ok else 'n/a'}</code>",
    ]

    # Souhrn po měnách
    if per_currency:
        cur_part = ", ".join(f"{c}: {n}" for c, n in sorted(per_currency.items()))
        lines.append(f"Počty relevantních událostí (okno): <code>{cur_part}</code>")

    # Tematický přehled
    if per_theme:
        nice_order = ["sazby/cb","inflace","pmi","hdp","nezaměstnanost","maloobchod","průmysl","bydlení"]
        parts = []
        for k in nice_order:
            if per_theme.get(k):
                parts.append(f"{k}: {per_theme[k]}")
        if parts:
            lines.append("Témata v titulcích: " + ", ".join(parts))

    # Top události v období
    if top_events:
        lines.append("\n📢 <b>Top události v okně</b>")
        for score, dt, cur_disp, title in top_events[:12]:
            lines.append(f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title}")

    # Nejbližších 48 h
    if upcoming:
        lines.append("\n⏳ <b>Nejbližších 48 h</b>")
        for dt, cur_disp, title in upcoming[:15]:
            lines.append(f"• {dt.strftime('%Y-%m-%d %H:%M')} <b>{cur_disp}</b> {title}")

    send_telegram("\n".join(lines))
    save_seen(seen)
    sys.exit(0)

if __name__ == "__main__":
    main()
