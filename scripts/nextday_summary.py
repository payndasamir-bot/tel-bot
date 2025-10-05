#!/usr/bin/env python3
import os, sys, json, argparse, datetime, time
import requests
import re
from html import escape
from zoneinfo import ZoneInfo

FORCE_PROBE = False  # přepni na True, když chceš poslat syrové ukázky z feedu

# ============ Konfigurace ============
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")   or os.getenv("TG_CHAT_ID")

TZ_NAME  = os.getenv("TZ", "Europe/Prague")
TZ_LOCAL = ZoneInfo(TZ_NAME)

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
def _to_float(x: str | float | int) -> float | None:
    """'3.4', '3,4', '3.4%', '65K', '0.2B' -> float | None"""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if not s or s in {"—", "-", "N/A", "na", "NaN"}:
        return None
    s = s.replace(" ", "").replace(",", ".")
    if s.endswith("%"):
        s = s[:-1]
    m = re.match(r"^([-+]?\d*\.?\d*)([KMBT])$", s, flags=re.I)
    if m:
        base, suf = m.groups()
        try:
            v = float(base)
        except:
            return None
        mults = {"K":1e3, "M":1e6, "B":1e9, "T":1e12}
        return v * mults[suf.upper()]
    try:
        return float(s)
    except:
        return None

def _event_type(title: str) -> str:
    t = title.lower()
    if "cpi" in t or "inflation" in t: return "inflation"
    if "rate" in t or "interest" in t: return "rates"
    if "unemployment" in t or "jobless" in t or "claims" in t: return "jobs"
    if "gdp" in t: return "gdp"
    if "retail sales" in t: return "retail"
    if "pmi" in t or "ism" in t: return "pmi"
    if "industrial production" in t or "factory" in t or "orders" in t: return "production"
    if "trade balance" in t or "current account" in t: return "trade"
    if "sentiment" in t or "confidence" in t or "expectations" in t or "optimism" in t: return "sentiment"
    if "housing" in t or "building permits" in t or "pending home" in t: return "housing"
    return "other"

_HIGHER_IS_BETTER = {
    "inflation": True,
    "rates": True,
    "jobs": False,
    "gdp": True,
    "retail": True,
    "pmi": True,
    "production": True,
    "trade": True,
    "sentiment": True,
    "housing": True,
    "other": None,
}

def eval_signal(title_raw: str, actual_raw: str, forecast_raw: str) -> int:
    """+1 (bull), -1 (bear), 0 (neutrální/nelze) podle actual vs forecast a typu metriky."""
    a = _to_float(actual_raw)
    f = _to_float(forecast_raw)
    if a is None or f is None:
        return 0
    typ = _event_type(title_raw)
    hib = _HIGHER_IS_BETTER.get(typ, None)
    if hib is None:
        return 0
    return (+1 if a > f else -1) if hib else (+1 if a < f else -1)

def _impact_weight(impact_raw: str) -> int:
    """High = 2, jinak 1 (velmi jednoduché váhování dopadu)."""
    s = (impact_raw or "").strip().lower()
    if "high" in s:   return 2
    if "med"  in s:   return 1
    if "low"  in s:   return 1
    return 1

def _fmt_score_one(cur: str, val: int) -> str:
    if val > 0:  return f"{cur}: +{val} 🟢↑"
    if val < 0:  return f"{cur}: {val} 🔴↓"
    return f"{cur}: +0 ⚪️→"

def _score_comment(scores: dict[str, int]) -> str:
    parts = []
    for cur, v in scores.items():
        if v > 0:   parts.append(f"{cur} posiluje")
        elif v < 0: parts.append(f"{cur} oslabuje")
        else:       parts.append(f"{cur} neutrální")
    main = " | ".join(parts)
    strongest_cur, strongest_val = max(scores.items(), key=lambda kv: abs(kv[1]))
    if abs(strongest_val) == 0:
        detail = "Zatím bez zveřejněných hodnot; čeká se na data."
    else:
        dir_word = "bullish" if strongest_val > 0 else "bearish"
        detail = f"Nejsilnější signál: {strongest_cur} ({'+' if strongest_val > 0 else ''}{strongest_val}, {dir_word})."
    return f"{main}. {detail}"

def to_local(ts: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).astimezone(TZ_LOCAL)

def pairs_to_currencies(pairs_list):
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
    last_err = None
    for host in FEED_HOSTS:
        url = host.rstrip("/") + "/" + path.lstrip("/")
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, params={"_": int(time.time())}, timeout=20)
                if r.status_code >= 400:
                    raise requests.HTTPError(f"{r.status_code} {r.reason}")
                data = r.json()
                return data if isinstance(data, list) else []
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
    parser.add_argument("--from", dest="from_date", type=str, default=None)
    parser.add_argument("--to", dest="to_date", type=str, default=None)
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided."); sys.exit(2)

    target = pairs_to_currencies(pairs)
    print("Cílové měny:", sorted(target))

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
        horizon_end = datetime.datetime.combine(to_date, datetime.time(23, 59), tzinfo=TZ_LOCAL)
    else:
        from_date   = today_local - datetime.timedelta(days=LOOKBACK_DAYS)
        to_date     = today_local
        horizon_end = now_local + datetime.timedelta(hours=AHEAD_HOURS)

    # ---- načtení feedů ----
    feed_merged = []
    for path in FEED_PATHS:
        data = fetch_json_from_hosts(path)
        if isinstance(data, list):
            feed_merged.extend(data)
    print("Feed items merged:", len(feed_merged))

    if FORCE_PROBE:
        countries = {}
        examples = []
        for ev in feed_merged:
            cur = (ev.get("country") or "").upper()
            countries[cur] = countries.get(cur, 0) + 1
            ts = ev.get("timestamp")
            tstr = to_local(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"
            if len(examples) < 10:
                examples.append(
                    f"• {tstr} <b>{escape(cur)}</b> {escape((ev.get('title') or '').strip())} | "
                    f"act=<b>{escape(str(ev.get('actual') or '').strip())}</b> "
                    f"fcst={escape(str(ev.get('forecast') or '').strip())}"
                )
        top = sorted(countries.items(), key=lambda x: x[1], reverse=True)[:10]
        top_str = ", ".join([f"{c}:{n}" for c, n in top]) if top else "—"
        msg = ["🧪 <b>PROBE: syrový výpis</b>", f"Feedů sloučeno: {len(feed_merged)}", f"Top země: {top_str}"]
        msg += examples
        send_telegram("\n".join(msg))
        print("PROBE done, exiting early.")
        return

    # ---- zpracování ----
    # vyber jen události pro cílové měny
    relevant = [ev for ev in feed_merged if (ev.get("country") or "").upper() in target]

    # skóre pro měny (jen co jsou v target)
    scores = {cur: 0 for cur in sorted(target)}

    published: list[str] = []
    upcoming:  list[str] = []

    def _ts_to_str(ts):
        if ts is None:
            return "—"
        try:
            return to_local(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return "—"

    for ev in relevant:
        cur          = (ev.get("country")  or "").upper()
        ts           = ev.get("timestamp")
        tstr         = _ts_to_str(ts)

        title_raw    = (ev.get("title")    or "").strip()
        actual_raw   = str(ev.get("actual")   or "").strip()
        forecast_raw = str(ev.get("forecast") or "").strip()
        previous_raw = str(ev.get("previous") or "").strip()
        impact_raw   = str(ev.get("impact")   or "").strip()

        has_actual = actual_raw not in {"", "-", "—", "N/A", "na", "NaN"}

        if has_actual:
            # zveřejněné položky
            published.append(
                "• "
                f"{tstr} <b>{escape(cur)}</b> {escape(title_raw)} — "
                f"Actual: <b>{escape(actual_raw)}</b> | "
                f"Fcst: {escape(forecast_raw)} | "
                f"Prev: {escape(previous_raw)} "
                f"(Impact: {escape(impact_raw)})"
            )
            # skóre (signál * váha dopadu)
            sig    = eval_signal(title_raw, actual_raw, forecast_raw)   # -1 / 0 / +1
            weight = _impact_weight(impact_raw)                         # 1 / 2
            scores[cur] = scores.get(cur, 0) + sig * weight
        else:
            # ještě nepřišlo – zobraz čas a forecast
            line = f"• {tstr} <b>{escape(cur)}</b> {escape(title_raw)}"
            if forecast_raw:
                line += f" (Fcst: {escape(forecast_raw)})"
            upcoming.append(line)

    # ---- zpráva ----
    header = "🔎 <b>Fundament souhrn (EUR/USD/JPY)</b>"

    # řádek se skóre + krátký koment
    ordered = ["EUR", "USD", "JPY"] + [c for c in sorted(scores.keys()) if c not in {"EUR","USD","JPY"}]
    score_line = "📈 <b>Směrové skóre</b> — " + " | ".join(_fmt_score_one(c, scores.get(c, 0)) for c in ordered if c in scores)
    score_hint = _score_comment(scores)

    meta = [
        f"Sloučený feed items: {len(feed_merged)}",
        f"Relevantních (EUR/USD/JPY): {len(relevant)} | Zveřejněno: {len(published)} | Čeká: {len(upcoming)}",
    ]

    lines: list[str] = [header, score_line, score_hint] + meta

    if published:
        lines.append("\n📢 <b>Zveřejněno</b>")
        lines.extend(published[:25])
        if len(published) > 25:
            lines.append(f"… a dalších {len(published) - 25}")

    if upcoming:
        lines.append("\n⏳ <b>V kalendáři (čeká)</b>")
        lines.extend(upcoming[:25])
        if len(upcoming) > 25:
            lines.append(f"… a dalších {len(upcoming) - 25}")

    if not published and not upcoming:
        lines.append("\n⚠️ Ve feedu nebyly nalezeny žádné položky.")

    send_telegram("\n".join(lines))
    print("Hotovo.")
    sys.exit(0)

if __name__ == "__main__":
    main()
