"""Register Telegram webhook using server environment; never print secrets."""
import argparse
import json
import os
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

URL = "https://bot-1791005537-5362-tenax.bothost.tech/telegram/webhook"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args()
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("API_TOKEN") or os.getenv("BOT_TOKEN")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        raise SystemExit("Missing token or invalid webhook secret; values omitted")
    try:
        try:
            with urlopen(Request(URL, data=b"{}", headers={"Content-Type":"application/json"}), timeout=20):
                raise SystemExit("FAIL: unauthenticated webhook accepted")
        except HTTPError as exc:
            if exc.code != 403:
                raise SystemExit("HTTPS route not ready: HTTP "+str(exc.code))
        print("HTTPS reachable; unauthenticated request rejected", flush=True)
        def api(method, body=None):
            req = Request("https://api.telegram.org/bot"+token+"/"+method,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type":"application/json"})
            with urlopen(req, timeout=20) as response:
                result = json.load(response)
            if not result.get("ok"):
                raise RuntimeError()
            return result.get("result")
        if args.register:
            api("setWebhook", {"url":URL,"secret_token":secret,"allowed_updates":["message"],
                               "drop_pending_updates":False})
            print("Webhook registered", flush=True)
        result = api("getWebhookInfo")
        print("URL matches:", result.get("url") == URL,
              "pending:", result.get("pending_update_count"),
              "delivery error present:", bool(result.get("last_error_message")), flush=True)
        print("Bot username:", api("getMe").get("username"), flush=True)
    except Exception:
        raise SystemExit("Check failed: network or API error; secret values omitted") from None

if __name__ == "__main__":
    main()
