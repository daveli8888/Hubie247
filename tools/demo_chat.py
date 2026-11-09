#!/usr/bin/env python3
"""Test chat for demo recordings: sends each message ~1 s after a round has STARTED (so it lands mid-round),
no earlier than its scheduled time. Reads game_events.jsonl in HUBIE_RUN_DIR; writes fake_chat.jsonl.
  tools/demo_chat.py "25|mila_rose|hubie what game is this?" "60|nightowl|love the hat hubie" "+2|benny_b|hi"
A time starting with "+" means: that many seconds after the previous message (same round, no waiting).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hubie247 import bus  # noqa: E402
from hubie247.config import FAKE_CHAT, GAME_EVENTS  # noqa: E402

t0 = time.time()
plan = []
for a in sys.argv[1:]:
    at, user, text = a.split("|", 2)
    plan.append((at, user, text))
tail = bus.Tail(GAME_EVENTS)
for at, user, text in plan:
    if at.startswith("+"):
        time.sleep(float(at[1:]))
        bus.append_jsonl(FAKE_CHAT, {"user": user, "text": text})
        print(f"{time.time():.2f} sent {user}: {text} (right after the previous one)", flush=True)
        continue
    at = float(at)
    while time.time() - t0 < at:
        tail.read()
        time.sleep(0.2)
    start = None
    while start is None and time.time() - t0 < at + 40:       # next round start (or give up and just send)
        for e in tail.read():
            if e.get("type") == "round_start":
                start = e
        time.sleep(0.1)
    time.sleep(1.0)
    bus.append_jsonl(FAKE_CHAT, {"user": user, "text": text})
    print(f"{time.time():.2f} sent {user}: {text} (round {start and start.get('rid')})", flush=True)
