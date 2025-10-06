#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fundament souhrn pro vybrané páry (např. EURUSD, USDJPY) z FF JSON feedu.
- Bere publikované události z posledních X dní (default 7) a udělá "kurátorovaný výběr".
- Barvy podle směru (🟢 bullish, 🔴 bearish, ⚪️ neutral).
- Komentáře "podle PDF" (inflace, HDP, PMI, trh práce, retail, sazby…).
- Váha podle impactu i stáří (recency).
- Výstup do Telegramu (bez ohledu na víkend – posílá KAŽDÝ den).

ENV:
  TELEGRAM_BOT_TOKEN / TG_BOT_TOKEN
  TELEGRAM_CHAT_ID   / TG_CHAT_ID
  TZ (default Europe/Prague)
  PAIRS (např. "EURUSD,USDJPY")

  WINDOW_DAYS       (default 7)  – kolik dní zpět hodnotit
  LOOKAHEAD_DAYS    (default 7)  – informativní horizont "čeká"
  MIN_IMPACT        (default medium) – 'low' | 'medium' | 'high'
  MIN_SURPRISE_PCT  (default 0.10)   – 10% překvapení proti forecastu
  TOP_N             (default 12)     – kolik highlightů poslat
"""

import os, sys, argparse, time, datetime, re
import requests
from html import escape
from zoneinfo import ZoneInfo

# === KONFIGURACE / ENV =======================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")   or os.getenv("TG_CHAT_ID")

TZ_NAME  = os.getenv("TZ", "Europe/Prague")
TZ_LOCAL = ZoneInfo(TZ_NAME)

PAIRS_ENV = os.getenv("PAIRS", "EURUSD,USDJPY")

FEED_PATHS = ["ff_calendar_thisweek.json", "ff_calendar_lastweek.json"]
FEED_HOSTS = ["https://nfs.faireconomy.media/", "https://cdn-nfs.faireconomy.media/"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.forexfactory.com/calendar",
    "Cache-Control": "no-cache",
}

# --- kurátorování / okna / thr ---
WINDOW_DAYS       = int(os.getenv("WINDOW_DAYS", "7"))
LOOKAHEAD_DAYS    = int(os.getenv("LOOKAHEAD_DAYS", "7"))
MIN_IMPACT        = os.getenv("MIN_IMPACT", "medium").lower()
MIN_SURPRISE_PCT  = float(os.getenv("MIN_SURPRISE_PCT", "0.10"))
TOP_N_HIGHLIGHTS  = int(os.getenv("TOP_N", "12"))

# === UI ======================================================================
def impact_badge(impact_raw: str) -> str:
    s = (impact_raw or "").strip().lower()
    if "high" in s:   return "🔴 High"
    if "med"  in s:   return "🟠 Medium"
    if "low"  in s:   return "🟢 Low"
    return "⚪︎"

def _arrow(sig: int) -> str:
    return "🟢↑" if sig > 0 else ("🔴↓" if sig < 0 else "⚪️→")

def _verdict(sig: int) -> str:
    return "Bullish" if sig > 0 else ("Bearish" if sig < 0 else "Neutral")

def fmt_pair_score(pair: str, val: float) -> str:
    v = f"{val:+.1f}"
    if val > 0:  return f"{pair}: {v} 🟢↑"
    if val < 0:  return f"{pair}: {v} 🔴↓"
    return f"{pair}: +0.0 ⚪️→"

# === parsování čísel a typů ==================================================
def _to_float(x) -> float | None:
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    s = str(x).strip()
    if not s or s in {"—", "-", "N/A", "na", "NaN"}: return None
    s = s.replace(" ", "").replace(",", ".")
    if s.endswith("%"): s = s[:-1]
    m = re.match(r"^([-+]?\d*\.?\d*)([KMBT])$", s, re.I)
    if m:
        base, suf = m.groups()
        try: v = float(base)
        except: return None
        mult = {"K":1e3,"M":1e6,"B":1e9,"T":1e12}[suf.upper()]
        return v * mult
    try:
        return float(s)
    except:
        return None

def _event_type(title: str) -> str:
    t = title.lower()
    if "cpi" in t or "inflation" in t: return "inflation"
    if "rate" in t or "interest" in t or "press conference" in t or "monetary" in t: return "rates"
    if any(k in t for k in ["unemployment","jobless","payroll","nfp","claims"]): return "jobs"
    if "gdp" in t: return "gdp"
    if "retail sales" in t: return "retail"
    if "pmi" in t or "ism" in t: return "pmi"
    if any(k in t for k in ["industrial production","factory","orders"]): return "production"
    if "trade balance" in t or "current account" in t: return "trade"
    if any(k in t for k in ["sentiment","confidence","expectations","optimism"]): return "sentiment"
    if any(k in t for k in ["housing","building permits","pending home"]): return "housing"
    return "other"

# "z PDF": vyšší je pro měnu lepší?
_HIGHER_IS_BETTER = {
    "inflation": True,   # vyšší CPI/PCE = jestřábí (měna ↑, XAU ↓)
    "rates":     True,   # jestřábí guidance = měna ↑
    "jobs":      False,  # nižší nezaměstnanost / vyšší NFP = měna ↑
    "gdp":       True,
    "retail":    True,
    "pmi":       True,   # 50+ expanze býčí
    "production":True,
    "trade":     True,
    "sentiment": True,
    "housing":   True,
    "other":     None,
}

def eval_signal(title_raw: str, actual_raw: str, forecast_raw: str) -> int:
    a = _to_float(actual_raw); f = _to_float(forecast_raw)
    if a is None or f is None: return 0
    typ = _event_type(title_raw); hib = _HIGHER_IS_BETTER.get(typ, None)
    if hib is None: return 0
    return (+1 if a > f else -1) if hib else (+1 if a < f else -1)

def _impact_weight(impact_raw: str) -> float:
    s = (impact_raw or "").lower()
    if "high" in s:   return 2.0
    if "med"  in s:   return 1.3
    return 1.0

def _recency_weight(ts) -> float:
    try:
        age_h = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc)
                 ).total_seconds() / 3600.0
    except: 
        return 1.0
    if age_h <= 6:   return 1.6
    if age_h <= 24:  return 1.25
    if age_h <= 72:  return 1.0
    return 0.75

# --- překlad impactu + překvapení ---
CORE_TYPES = {"inflation", "rates", "jobs", "gdp", "pmi", "retail"}

def impact_level(s: str) -> int:
    s = (s or "").lower()
    if "high" in s:   return 2
    if "med"  in s:   return 1
    if "low"  in s:   return 0
    return 0

def min_impact_level() -> int:
    m = MIN_IMPACT
    if m.startswith("h"): return 2
    if m.startswith("m"): return 1
    return 0

def surprise_ratio(actual_raw, forecast_raw) -> float:
    a = _to_float(actual_raw); f = _to_float(forecast_raw)
    if a is None or f is None or f == 0:
        return 0.0
    return abs(a - f) / abs(f)

# --- komentář "podle PDF" ---
def _comment_for_event(title: str, typ: str, actual, forecast, cur: str) -> str:
    a = _to_float(actual); f = _to_float(forecast)
    if a is None or f is None:
        base = {
            "inflation": "Inflace: vyšší => jestřábí (měna ↑, XAU ↓), nižší => holubičí (měna ↓, XAU ↑).",
            "rates":     "Sazby/řeč: jestřábí rétorika podpírá měnu; holubičí ji oslabuje.",
            "jobs":      "Trh práce: nižší nezaměstnanost / silné NFP býčí pro měnu.",
            "gdp":       "HDP: silnější růst býčí (měna/akcie ↑).",
            "retail":    "Maloobchod: silnější spotřeba býčí.",
            "pmi":       "PMI: 50+ expanze (býčí), <50 kontrakce (medvědí).",
            "production":"Průmysl/objednávky: silnější býčí.",
            "trade":     "Bilance: zlepšení býčí; zhoršení medvědí.",
            "sentiment": "Sentiment/konf.: vyšší = risk-on.",
            "housing":   "Bydlení: vyšší povolenky/prodeje býčí.",
            "other":     "Vliv dle překvapení vs. forecast.",
        }
        return base.get(typ, "Vliv dle překvapení vs. forecast.")
    if typ == "inflation":
        return f"Inflace nad oček. → jestřábí: {cur} ↑, XAU ↓" if a > f else f"Inflace pod oček. → {cur} ↓, XAU ↑"
    if typ == "gdp":
        return f"HDP nad oček. → {cur} ↑, akcie ↑" if a > f else f"HDP pod oček. → {cur} ↓"
    if typ == "jobs":
        return f"Trh práce silnější vs. fcst → {cur} ↑" if a < f else f"Trh práce slabší → {cur} ↓"
    if typ == "retail":
        return f"Spotřeba nad oček. → {cur} ↑, akcie ↑" if a > f else f"Spotřeba pod oček. → {cur} ↓"
    if typ == "pmi":
        if a >= 50 and (f is None or a >= f): return f"PMI expanze → {cur} ↑"
        if a < 50  and (f is None or a <= f): return f"PMI kontrakce → {cur} ↓"
        return "PMI vs. fcst smíšené → vliv mírný"
    if typ == "rates":
        return f"Sazby/řeč: jestřábí = {cur} ↑, holubičí = {cur} ↓"
    return "Překvapení vs. forecast určuje směr."

# === pomocné ================================================================
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
    """Pošli text do Telegramu. Pokud je delší než limit, rozešli ho na části."""
    if not BOT_TOKEN or not CHAT_ID:
        print("DEBUG: TELEGRAM env missing; skip send.")
        return

    MAX = 3800  # rezerva pod 4096 (kvůli HTML)
    def _send(part_text: str, prefix: str = ""):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": str(CHAT_ID),
            "text": (prefix + part_text),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        try:
            r = requests.post(url, data=payload, timeout=25)
            print("Telegram HTTP:", r.status_code, r.text[:200])
        except Exception as e:
            print("Telegram exception:", e)

    lines = text.splitlines()
    parts, buf = [], ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > MAX:
            parts.append(buf); buf = ln
        else:
            buf = (buf + "\n" + ln) if buf else ln
    if buf: parts.append(buf)

    total = len(parts)
    for i, part in enumerate(parts, 1):
        prefix = f"({i}/{total}) " if total > 1 else ""
        _send(part, prefix=prefix)
        time.sleep(0.4)

def fetch_json_from_hosts(path: str):
    last_err = None
    for host in FEED_HOSTS:
        url = host.rstrip("/") + "/" + path.lstrip("/")
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, params={"_": int(time.time())}, timeout=25)
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

# === main ===================================================================
def _fmt_score_one(cur: str, val: float) -> str:
    if val > 0:   return f"{cur}: +{val:.1f} 🟢↑"
    if val < 0:   return f"{cur}: {val:.1f} 🔴↓"
    return f"{cur}: +0.0 ⚪️→"

def _score_comment(scores: dict[str, float]) -> str:
    if not scores:
        return "Bez dat."
    parts = []
    for cur, val in scores.items():
        if val > 0:   parts.append(f"{cur} posiluje")
        elif val < 0: parts.append(f"{cur} oslabuje")
        else:         parts.append(f"{cur} neutrální")
    main = " | ".join(parts)
    strongest_cur, strongest_val = max(scores.items(), key=lambda kv: abs(kv[1]))
    detail = ("Zatím bez zveřejněných hodnot; čeká se na data."
              if abs(strongest_val) == 0 else
              f"Nejsilnější signál: {strongest_cur} ({strongest_val:+.1f}).")
    return f"{main}. {detail}"

def _pair_bias_sentence(pr: str, val: float) -> str:
    if val > 0:  return f"{pr}: bias <b>UP</b> (base silnější) – spíše kupovat pullbacky."
    if val < 0:  return f"{pr}: bias <b>DOWN</b> (quote silnější) – spíše prodávat rally."
    return f"{pr}: bias <b>NEUTRAL</b> – čeká se na nové katalyzátory."

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=PAIRS_ENV)             # "EURUSD,USDJPY"
    parser.add_argument("--from", dest="from_date", type=str, default=None) # volitelný filtr data
    parser.add_argument("--to",   dest="to_date",   type=str, default=None)
    args = parser.parse_args()

    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("No pairs provided."); sys.exit(2)

    target   = pairs_to_currencies(pairs)
    pair_list = [p for p in pairs if len(p) == 6]
    print("Cílové měny:", sorted(target))

    def _parse_date(s: str) -> datetime.date:
        return datetime.date.fromisoformat(s)

    # okna pro výběr
    LOOKBACK_DAYS = WINDOW_DAYS
    AHEAD_HOURS   = 24 * LOOKAHEAD_DAYS

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

    # načtení feedů
    feed_merged = []
    for path in FEED_PATHS:
        feed_merged.extend(fetch_json_from_hosts(path))
    print("Feed items merged:", len(feed_merged))

    # výběr jen cílových měn
    relevant = [ev for ev in feed_merged if (ev.get("country") or "").upper() in target]

    # skóre měn/párů
    scores: dict[str, float]     = {cur: 0.0 for cur in sorted(target)}
    pair_scores: dict[str, float]= {p: 0.0 for p in pair_list}

    published: list[str] = []
    upcoming:  list[str] = []
    highlights: list[tuple[float, str]] = []  # (síla, řádek)

    def _ts_to_str(ts):
        if ts is None: return "—"
        try: return to_local(ts).strftime("%Y-%m-%d %H:%M")
        except: return "—"

    for ev in relevant:
        cur          = (ev.get("country")  or "").upper()
        ts           = ev.get("timestamp")
        tstr         = _ts_to_str(ts)

        title_raw    = (ev.get("title")    or "").strip()
        actual_raw   = str(ev.get("actual")   or "").strip()
        forecast_raw = str(ev.get("forecast") or "").strip()
        previous_raw = str(ev.get("previous") or "").strip()
        impact_raw   = str(ev.get("impact")   or "").strip()

        typ = _event_type(title_raw)
        has_actual = actual_raw not in {"", "-", "—", "N/A", "na", "NaN"}

        if has_actual:
            # ---- kurátorovací filtr (jen relevantní/velké věci) ----
            lvl   = impact_level(impact_raw)
            surpr = surprise_ratio(actual_raw, forecast_raw)
            if not (
                (lvl >= min_impact_level() and (typ in CORE_TYPES))  # core téma a min. impact
                or (lvl == 2)                                        # cokoliv s High impact
                or (surpr >= MIN_SURPRISE_PCT)                       # nebo velké překvapení
            ):
                continue
            # --------------------------------------------------------

            # směrový signál + váhy
            sig    = eval_signal(title_raw, actual_raw, forecast_raw)   # -1/0/+1
            w_imp  = _impact_weight(impact_raw)
            w_rec  = _recency_weight(ts)
            cur_gain = float(sig) * w_imp * w_rec
            scores[cur] = scores.get(cur, 0.0) + cur_gain

            for pr in pair_list:
                base, quote = pr[:3], pr[3:]
                if cur == base:      pair_scores[pr] += cur_gain
                elif cur == quote:   pair_scores[pr] -= cur_gain

            pdf_note = _comment_for_event(title_raw, typ, actual_raw, forecast_raw, cur)
            arrow = "🟢" if sig > 0 else ("🔴" if sig < 0 else "⚪️")

            line = (
                "• "
                f"{tstr} <b>{escape(cur)}</b> {escape(title_raw)} — "
                f"Actual: <b>{escape(actual_raw)}</b> | "
                f"Fcst: {escape(forecast_raw)} | "
                f"Prev: {escape(previous_raw)} "
                f"(Impact: {impact_badge(impact_raw)}) {arrow}\n"
                f"   ↳ {escape(pdf_note)}  <i>{_verdict(sig)} {_arrow(sig)}</i>"
            )
            published.append(line)
            highlights.append((abs(cur_gain), line))

        else:
            # budoucí události – také probereme (jen core + min. impact)
            lvl = impact_level(impact_raw)
            if not (lvl >= min_impact_level() and typ in CORE_TYPES):
                continue

            hint = {
                "inflation": "Nad fcst = 🟢 (jestřábí), pod fcst = 🔴",
                "jobs":      "Nižší nezam. / vyšší NFP vs. fcst = 🟢, slabší = 🔴",
                "gdp":       "Nad fcst = 🟢, pod fcst = 🔴",
                "retail":    "Nad fcst = 🟢, pod fcst = 🔴",
                "pmi":       "PMI >50 býčí; pod 50 medvědí",
                "rates":     "Jestřábí = 🟢, holubičí = 🔴",
            }.get(typ, "Směr dle překvapení vs. fcst")
            line = (
                f"• {tstr} <b>{escape(cur)}</b> {escape(title_raw)}"
                + (f" (Fcst: {escape(forecast_raw)})" if forecast_raw else "")
                + (f" — {impact_badge(impact_raw)} ⚪️" if impact_raw else " — ⚪️")
                + f"\n   ↳ {hint}"
            )
            upcoming.append(line)

    # zpráva
    header = "🔎 <b>Fundament souhrn ({})</b>".format("/".join(sorted(target)))

    order_hint = ["EUR","USD","JPY","GBP","CAD","AUD","NZD","CHF","CNY"]
    ordered = [c for c in order_hint if c in scores] + [c for c in scores.keys() if c not in order_hint]
    score_line = "📈 <b>Směrové skóre (měny)</b> — " + " | ".join(_fmt_score_one(c, scores.get(c, 0.0)) for c in ordered)
    score_hint = _score_comment(scores)

    lines: list[str] = [header, score_line, score_hint]

    if pair_scores:
        pairs_pretty = " | ".join(fmt_pair_score(p, v) for p, v in pair_scores.items())
        lines.append("💱 <b>Skóre párů</b> — " + pairs_pretty)

        lines.append("\n🧭 <b>Směrové shrnutí párů</b>")
        for pr, v in pair_scores.items():
            lines.append("• " + _pair_bias_sentence(pr, v))

    meta = [
        f"Sloučený feed items: {len(feed_merged)}",
        f"Relevantních ({'/'.join(sorted(target))}): {len(relevant)} | Zveřejněno: {len(published)} | Čeká: {len(upcoming)}",
        f"Okno: posledních {WINDOW_DAYS} dní | Výhled do {LOOKAHEAD_DAYS} dnů",
    ]
    lines += meta

    if published:
        lines.append(f"\n📢 <b>Kurátorovaný výběr (posledních {WINDOW_DAYS} dní)</b>")
        top = sorted(highlights, key=lambda x: x[0], reverse=True)[:TOP_N_HIGHLIGHTS]
        lines += [row for _, row in top]

    if upcoming:
        lines.append("\n⏳ <b>V kalendáři (čeká – s datem & časem)</b>")
        # seřadíme podle času
        upcoming_sorted = sorted(upcoming, key=lambda x: re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", x).group(0) if re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", x) else "9999-99-99 99:99")
        lines.extend(upcoming_sorted)

    if not published and not upcoming:
        lines.append("\n⚠️ Ve feedu nebyly nalezeny žádné položky.")

    send_telegram("\n".join(lines))
    print("Hotovo.")
    sys.exit(0)

if __name__ == "__main__":
    main()
