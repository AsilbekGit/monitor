"""
Chat-id finder. Run this, then send a message IN THE GROUP (mention the bot:
'@yourbot hi'). It prints every chat it hears from, with the id you need.

Usage:
    export TG_TOKEN="123456:ABC..."
    python find_chat_id.py
"""
import os
import time
import requests

TOKEN = os.environ["TG_TOKEN"]
BASE = f"https://api.telegram.org/bot{TOKEN}"

# 1. Clear any backlog so we only see fresh messages.
r = requests.get(f"{BASE}/getUpdates", params={"offset": -1}, timeout=20).json()
offset = None
if r.get("result"):
    offset = r["result"][-1]["update_id"] + 1

print("\n=== Listening. Now send a message in your GROUP (e.g. '@yourbot hi'). ===")
print("=== Mention the bot if privacy mode might still be on. Ctrl+C to stop. ===\n")

seen = set()
while True:
    try:
        params = {"timeout": 30}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(f"{BASE}/getUpdates", params=params, timeout=40).json()
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            # a message can arrive under several keys
            msg = (upd.get("message") or upd.get("channel_post")
                   or upd.get("my_chat_member") or {})
            chat = msg.get("chat", {})
            if not chat:
                # my_chat_member nests chat differently
                chat = upd.get("my_chat_member", {}).get("chat", {})
            if not chat:
                print("Got an update with no chat info:", upd)
                continue
            cid = chat.get("id")
            ctype = chat.get("type")
            title = chat.get("title") or chat.get("first_name") or ""
            tag = (cid, ctype)
            if tag not in seen:
                seen.add(tag)
                print(f">>> chat id: {cid}   type: {ctype}   name: {title}")
                if ctype in ("group", "supergroup", "channel"):
                    print("    ^ THIS is your group id. Use it as TG_CHAT_ID "
                          "(keep the minus sign).\n")
    except KeyboardInterrupt:
        print("\nStopped.")
        break
    except Exception as e:
        print("error:", e)
        time.sleep(2)
