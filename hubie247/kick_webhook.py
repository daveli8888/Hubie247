"""Kick chat via the official API: a tiny webhook receiver for `chat.message.sent`.

Kick POSTs every chat message of the channel to KICK_WEBHOOK_PUBLIC_URL (set in the Kick app settings).
nginx terminates HTTPS and forwards to 127.0.0.1:KICK_WEBHOOK_PORT. Each delivery is checked against
Kick's RSA signature (public key fetched from Kick, never hardcoded), de-duplicated by message id and
appended to KICK_CHAT (jsonl). The brain's KickHookChat source tails that file.

Run:  python3 -m hubie247.kick_webhook            (pm2: hubie247-kickhook)
      python3 -m hubie247.kick_webhook subscribe  (one-off: subscribe the app to chat.message.sent)
"""
import base64
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .config import RUN, env

KICK_CHAT = RUN / "kick_chat.jsonl"
PORT = int(env("KICK_WEBHOOK_PORT", "8108"))
API = "https://api.kick.com/public/v1"
UA = {"User-Agent": "hubie247/0.1 (+kick webhook)", "Accept": "application/json"}

_key = {"obj": None, "ts": 0}
_seen = OrderedDict()


def public_key(force=False):
    if force or _key["obj"] is None or time.time() - _key["ts"] > 3600:
        with urllib.request.urlopen(urllib.request.Request(API + "/public-key", headers=UA), timeout=10) as r:
            pem = json.load(r)["data"]["public_key"]
        _key["obj"] = serialization.load_pem_public_key(pem.encode())
        _key["ts"] = time.time()
    return _key["obj"]


def verify(msg_id, ts, body, sig_b64):
    data = f"{msg_id}.{ts}.".encode() + body
    sig = base64.b64decode(sig_b64)
    for force in (False, True):          # retry once with a fresh key (Kick may rotate it)
        try:
            public_key(force).verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
            return True
        except Exception:
            continue
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/kick/health" else 404)
        self.end_headers()
        self.wfile.write(b"ok\n" if self.path == "/kick/health" else b"")

    def do_POST(self):
        n = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        body = self.rfile.read(n)
        if self.path != "/kick/webhook":
            self.send_response(404); self.end_headers(); return
        h = self.headers
        mid, ts, sig = h.get("Kick-Event-Message-Id"), h.get("Kick-Event-Message-Timestamp"), h.get("Kick-Event-Signature")
        if not (mid and ts and sig) or not verify(mid, ts, body, sig):
            self.send_response(401); self.end_headers(); return
        self.send_response(200); self.end_headers()          # ack fast, Kick retries otherwise
        if mid in _seen:
            return
        _seen[mid] = 1
        while len(_seen) > 2000:
            _seen.popitem(last=False)
        if h.get("Kick-Event-Type") != "chat.message.sent":
            return
        try:
            ev = json.loads(body)
            user = (ev.get("sender") or {}).get("username") or ""
            text = ev.get("content") or ""
            if user and text:
                KICK_CHAT.parent.mkdir(parents=True, exist_ok=True)
                with open(KICK_CHAT, "a") as f:
                    f.write(json.dumps({"user": user, "text": text, "ts": time.time()}) + "\n")
        except Exception as e:
            print("bad payload", repr(e)[:200], flush=True)


def app_token():
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": env("KICK_CLIENT_ID"),
                                   "client_secret": env("KICK_CLIENT_SECRET")}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://id.kick.com/oauth/token", data=data, headers=UA), timeout=15) as r:
        return json.load(r)["access_token"]


def api(method, path, tok, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={**UA, "Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def subscribe():
    tok = app_token()
    slug = env("KICK_CHANNEL")
    bid = api("GET", f"/channels?slug={urllib.parse.quote(slug)}", tok)["data"][0]["broadcaster_user_id"]
    print("channel", slug, "broadcaster_user_id", bid)
    print(api("POST", "/events/subscriptions", tok,
              {"broadcaster_user_id": bid, "method": "webhook", "events": [{"name": "chat.message.sent", "version": 1}]}))
    print(api("GET", "/events/subscriptions", tok))


def main():
    if sys.argv[1:] == ["subscribe"]:
        return subscribe()
    public_key()
    print(f"kick webhook listening on 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
