"""Chat sources. Each runs in its own thread and puts ChatMsg into a shared queue.

FakeChat   tails fake_chat.jsonl (tools/fake_chat.py writes it) - for local testing
KickChat   Kick's public chat websocket (Pusher), read-only, no login needed. Needs the channel's
           chatroom id: KICK_CHATROOM_ID, or KICK_CHANNEL (slug) to look it up via kick.com's public API.
Hubie speaks his replies on stream; posting text back into Kick chat would need the Kick API with a
bot token (optional, not required by the contest).
"""
import json
import queue
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from . import bus
from .config import FAKE_CHAT, env


@dataclass
class ChatMsg:
    user: str
    text: str
    source: str
    ts: float = field(default_factory=time.time)


class FakeChat(threading.Thread):
    def __init__(self, q):
        super().__init__(daemon=True)
        self.q = q
        self.tail = bus.Tail(FAKE_CHAT)

    def run(self):
        while True:
            for m in self.tail.read():
                if m.get("user") and m.get("text"):
                    self.q.put(ChatMsg(m["user"][:25], m["text"][:500], "fake"))
            time.sleep(0.3)


# Kick's public chat socket. The key/cluster below were Kick's for years but were rejected when tested on
# 2026-11-08 ("App key not in this cluster"): read the current ones from kick.com's web bundle and set
# KICK_PUSHER_KEY / KICK_PUSHER_HOST. The official alternative is the Kick API webhook chat.message.sent.
KICK_WS = "wss://{host}/app/{key}?protocol=7&client=js&version=8.4.0-rc2&flash=false"
KICK_PUSHER_KEY = env("KICK_PUSHER_KEY", "32cbd69e4b950bf97679")
KICK_PUSHER_HOST = env("KICK_PUSHER_HOST", "ws-us2.pusher.com")


def kick_chatroom_id(slug):
    req = urllib.request.Request(f"https://kick.com/api/v2/channels/{slug}",
                                 headers={"Accept": "application/json", "User-Agent": "hubie247/0.1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["chatroom"]["id"]


class KickChat(threading.Thread):
    def __init__(self, q, status):
        super().__init__(daemon=True)
        self.q, self.status = q, status

    def run(self):
        import asyncio
        asyncio.run(self.loop())

    async def loop(self):
        import websockets
        room = env("KICK_CHATROOM_ID")
        backoff = 2
        while True:
            try:
                if not room:
                    room = str(kick_chatroom_id(env("KICK_CHANNEL")))
                async with websockets.connect(KICK_WS.format(host=KICK_PUSHER_HOST, key=KICK_PUSHER_KEY), ping_interval=None) as ws:
                    await ws.send(json.dumps({"event": "pusher:subscribe",
                                              "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
                    self.status["kick"] = f"connected room {room}"
                    backoff = 2
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=150)
                        m = json.loads(raw)
                        ev = m.get("event", "")
                        if ev == "pusher:ping":
                            await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                        elif ev.endswith("ChatMessageEvent"):
                            d = json.loads(m["data"]) if isinstance(m.get("data"), str) else m.get("data", {})
                            user = (d.get("sender") or {}).get("username", "")
                            if user and d.get("content"):
                                self.q.put(ChatMsg(user[:25], d["content"][:500], "kick"))
            except Exception as e:
                self.status["kick"] = f"reconnecting: {e!r}"[:200]
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)


class KickHookChat(threading.Thread):
    """Official Kick API chat: tails what kick_webhook.py writes (chat.message.sent webhooks)."""

    def __init__(self, q, status):
        super().__init__(daemon=True)
        from .kick_webhook import KICK_CHAT
        self.q, self.status = q, status
        self.tail = bus.Tail(KICK_CHAT)

    def run(self):
        self.status["kick"] = "webhook"
        while True:
            for m in self.tail.read():
                if m.get("user") and m.get("text"):
                    self.q.put(ChatMsg(m["user"][:25], m["text"][:500], "kick"))
            time.sleep(0.3)


def start_sources(q, status):
    srcs = [s.strip() for s in env("HUBIE_CHAT_SOURCES", "fake").split(",") if s.strip()]
    threads = []
    if "fake" in srcs:
        threads.append(FakeChat(q))
    if "kickhook" in srcs:
        threads.append(KickHookChat(q, status))
    if "kick" in srcs and (env("KICK_CHATROOM_ID") or env("KICK_CHANNEL")):
        threads.append(KickChat(q, status))
    for t in threads:
        t.start()
    return [type(t).__name__ for t in threads]
