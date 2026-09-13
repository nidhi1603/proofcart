#!/usr/bin/env python3
"""Post the seeded supplier negotiations into the live Slack channel so the demo
channel holds real threads for ProofCart to read.

A Slack bot cannot impersonate users, so the bot posts every message with a
speaker prefix (``*Acme:* ...`` / ``*Owner:* ...``); the collector's prefix mode
reconstructs role + supplier from that. LIVE only -- needs SLACK_BOT_TOKEN and
SLACK_CHANNEL_ID in .env, and the bot must be invited to the channel.
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from proofcart.config import get_settings  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEAKER = {
    "U_OWNER": "Owner",
    "U_ACME": "Acme",
    "U_BOLT": "Bolt Supply",
    "U_CIRRO": "Cirro Parts",
    "U_DELTA": "Delta Gear",
}


def main() -> int:
    st = get_settings()
    token, channel = st.slack_bot_token, st.slack_channel_id
    if not token or not channel:
        print("Set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in .env first.")
        return 1
    try:
        from slack_sdk import WebClient
    except ImportError:
        print("pip install slack_sdk")
        return 1

    client = WebClient(token=token)
    threads = json.loads((ROOT / "data" / "slack_threads.json").read_text())
    for i, thread in enumerate(threads, 1):
        parent_ts = None
        for m in thread:
            who = SPEAKER.get(m.get("user"), m.get("user", "?"))
            text = f"*{who}:* {m.get('text', '')}"
            resp = client.chat_postMessage(channel=channel, text=text, thread_ts=parent_ts)
            parent_ts = parent_ts or resp["ts"]
            time.sleep(0.4)
        print(f"  posted thread {i} ({len(thread)} messages)")
    print(f"Seeded {len(threads)} supplier threads into {channel}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
