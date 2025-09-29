#!/usr/bin/env python3
import os, sys, json, urllib.request, urllib.parse

def send(text: str):
    token = os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

    # Debug (bezpečný): neukazuje hodnoty, jen info, zda existují
    print("DEBUG: has_token=", bool(token), "has_chat_id=", bool(chat_id))

    if not token or not chat_id:
        print("Telegram secrets missing. Skipping send.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode()
            print("Telegram HTTP:", resp.status)
            # zobrazení ok:true/false
            try:
                j = json.loads(body)
                print("Telegram ok:", j.get("ok"), "desc:", j.get("description"))
            except Exception:
                print("Telegram raw:", body[:200])
    except Exception as e:
        print("Telegram exception:", e)

if __name__ == "__main__":
    text = " ".join(sys.argv[1:]).strip() or "✅ Daily summary proběhlo."
    send(text)
