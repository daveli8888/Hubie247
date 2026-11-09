"""Housebets game source: the REAL site, played with zero-amount bets in the owner's logged-in browser.

Browser: a logged-in Chrome the operator runs (see docs/SETUP.md section 6), reached over CDP on
HUBIE_HB_CDP. Nothing new is launched. The runner works in the same tab the owner's remote viewer shows,
so a Cloudflare box or a logout is right there for him to deal with from his phone.

Every tick (~1.5 s) the runner checks the tab:
  challenge page ("Just a moment", "Verify you are human", ...) -> fallback scene + RUN/needs_owner.txt.
                                       The challenge is NEVER clicked, solved or worked around.
  logged out                           -> fallback scene + needs_owner.txt (log in via the viewer)
  error page / game missing            -> fallback scene, reload at most every 90 s; flag after 3 min
  cookie banner                        -> "Accept"
  popup over the game                  -> Escape, then the popup's own full-screen "Close" backdrop
  all good                             -> live: picture from the tab, one round every few seconds
It resumes on its own once the tab is healthy again, and removes needs_owner.txt.

Picture: CDP screencast of the tab (no screenshots with clip: that re-lays the page out at the clip size),
cropped to the game and written to game.jpg as is (the stage fits it into its game frame, whatever the layout);
HUBIE_HB_LETTERBOX=1 brings back the old fixed 16:9 letterbox over a blurred copy.
Rotation: HUBIE_HB_ROTATION (dice,plinko; mines and slot also available) every HUBIE_HB_ROTATE_MIN minutes, round boundaries only.

Turn-taking with Hubie (turn.json, written by the brain), so his lines match the picture:
  - every round has an id (rid): "round_start" then "round" in game_events.jsonl
  - the next round waits until the brain released the last rid (its reaction was spoken, or skipped), plus a
    short pause; if the brain never starts speaking it, the wait ends after HUBIE_TURN_REACT_WAIT s (8),
    and a reaction that is being spoken is waited out up to HUBIE_TURN_REACT_MAX s (20)
  - rounds also wait while Hubie is talking (chat answers, idle lines), at most HUBIE_TURN_TALK_MAX s (30)
    in a row, then one round goes ahead anyway
  - a rotation is announced first ("switch_pending", to=X); the card + navigation follow once his "next up"
    line has started (+HUBIE_SWITCH_LEAD s), or after HUBIE_SWITCH_WAIT s (8) without one
  - no brain heartbeat in turn.json for 5 s: no waiting at all
"""
import asyncio
import base64
import os
import sys
import time

import cv2
import numpy as np
from playwright.async_api import async_playwright

from .config import ROOT as _ROOT
sys.path.insert(0, str(_ROOT))          # games/ lives next to the package

from games.base import Unsafe  # noqa: E402
from games.housebets_dice import HousebetsDice
from games.housebets_mines import HousebetsMines
from games.housebets_plinko import HousebetsPlinko
from games.housebets_slot import HousebetsSlot
from games.placeholder_dice import PlaceholderDice

from . import bus
from .config import GAME_EVENTS, GAME_FRAME, GAME_STATUS, LOG_DIR, ROOT, TURN, env, env_float

OUT_W, OUT_H = 832, 468
NEEDS_OWNER = ROOT / "RUN" / "needs_owner.txt"
ROUND_LOG = LOG_DIR / "housebets_rounds.jsonl"
ADAPTERS = {"dice": HousebetsDice, "plinko": HousebetsPlinko, "mines": HousebetsMines, "slot": HousebetsSlot}

HEALTH = r"""
() => {
  const title = document.title || '';
  const body = (document.body ? document.body.innerText : '').slice(0, 4000);
  const low = (title + '\n' + body).toLowerCase();
  const marks = ['just a moment', 'verify you are human', 'performing security verification',
                 'verifying you are human', 'checking your browser', 'attention required'];
  const cfFrame = [...document.querySelectorAll('iframe')].some(f => (f.src || '').includes('challenges.cloudflare.com'));
  const challenge = marks.some(m => low.includes(m)) || cfFrame || location.pathname.includes('/cdn-cgi/challenge');
  const loggedIn = !!document.querySelector('[data-testid=header-currency-switcher-toggle],[data-testid=header-profile-menu-trigger]');
  const vis = e => e && e.offsetParent !== null;
  const loginBtn = [...document.querySelectorAll('button,a')].some(b => vis(b) && /^(log ?in|sign ?in|register|sign ?up)$/i.test((b.innerText || '').trim()));
  const cookie = [...document.querySelectorAll('button')].some(b => vis(b) && b.innerText.trim() === 'Accept' && /cookie/i.test(b.closest('div,section,aside')?.parentElement?.innerText || ''));
  const overlayClose = [...document.querySelectorAll('button[aria-label=Close]')].some(b => { const r = b.getBoundingClientRect();
        return getComputedStyle(b).position === 'fixed' && r.width >= innerWidth * 0.8 && r.height >= innerHeight * 0.8; });
  return {url: location.href, title, challenge, loggedIn, loginBtn, cookie, overlayClose,
          errorPage: location.href.startsWith('chrome-error://') || /this site can.t be reached|err_[a-z_]+/i.test(body.slice(0, 600))};
}"""

COVERED = r"""
(rect) => {
  const pts = [[0.3, 0.3], [0.7, 0.5], [0.2, 0.8]];
  for (const [fx, fy] of pts) {
    const x = rect.x + rect.width * fx, y = rect.y + rect.height * fy;
    const el = document.elementFromPoint(x, y);
    if (!el) continue;
    const inGame = el.closest('[data-housegames-direct], [data-testid^=housegames-direct]') || el.tagName === 'IFRAME';
    if (!inGame) return (el.getAttribute('aria-label') || el.tagName) + ' ' + (el.className || '').toString().slice(0, 40);
  }
  return '';
}"""

CLOSE_OVERLAY = r"""
() => { const b = [...document.querySelectorAll('button[aria-label=Close]')].find(b => { const r = b.getBoundingClientRect();
          return getComputedStyle(b).position === 'fixed' && r.width >= innerWidth * 0.8 && r.height >= innerHeight * 0.8; });
        if (b) { b.click(); return true; } return false; }"""

ACCEPT_COOKIES = r"""
() => { const b = [...document.querySelectorAll('button')].find(b => b.offsetParent && b.innerText.trim() === 'Accept'
          && /cookie/i.test(b.closest('div,section,aside')?.parentElement?.innerText || ''));
        if (b) { b.click(); return true; } return false; }"""


def letterbox(img, out_w=OUT_W, out_h=OUT_H):
    """Fit img into out_w x out_h; the bars are a dimmed, blurred copy of the picture."""
    h, w = img.shape[:2]
    s = min(out_w / w, out_h / h)
    nw, nh = max(1, int(w * s)), max(1, int(h * s))
    fg = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    bg = cv2.resize(cv2.resize(img, (32, 18), interpolation=cv2.INTER_AREA), (out_w, out_h),
                    interpolation=cv2.INTER_LINEAR)
    bg = (cv2.GaussianBlur(bg, (0, 0), 9) * 0.45).astype(np.uint8)
    x, y = (out_w - nw) // 2, (out_h - nh) // 2
    bg[y:y + nh, x:x + nw] = fg
    return bg


MAX_W = 1100


def prep(img):
    """Picture for the stage: the plain game crop (capped in size), or the old 16:9 letterbox if asked for."""
    if env("HUBIE_HB_LETTERBOX", "0") == "1":
        return letterbox(img)
    h, w = img.shape[:2]
    if w > MAX_W:
        img = cv2.resize(img, (MAX_W, int(h * MAX_W / w)), interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(img)


def log_round(**kw):
    bus.append_jsonl(ROUND_LOG, kw)
    print("round", kw, flush=True)


class Runner:
    def __init__(self):
        self.cdp_url = env("HUBIE_HB_CDP", "http://127.0.0.1:9241")
        self.base = env("HUBIE_HB_BASE", "https://www.housebets.com")
        self.fps = env_float("HUBIE_HB_FPS", 10)
        self.rotate_secs = env_float("HUBIE_HB_ROTATE_MIN", 20) * 60
        self.round_override = env("HUBIE_HB_ROUND_SECS")
        keys = [k.strip() for k in env("HUBIE_HB_ROTATION", "dice,plinko").split(",") if k.strip() in ADAPTERS]
        self.games = [ADAPTERS[k]() for k in (keys or ["dice"])]
        self.gi = 0
        self.game_since = time.time()
        self.bad_until = {}                    # adapter key -> time it may be retried after failing to load
        self.ph = PlaceholderDice()
        self.ph_last_round = 0
        self.state, self.reason = "starting", ""
        self.state_since = time.time()
        self.live_img, self.live_ts = None, 0.0
        self.rect, self.scale_hint = None, 1.0
        self.last_shot = 0.0
        self.health_ts = 0.0
        self.rounds = 0
        self.last_round_t = 0.0
        self.last_nav = 0.0
        self.last_fix = 0.0
        self.prepared = None
        self.flagged = False
        # turn-taking with the brain
        self.last_rid, self.last_result_t = 0, 0.0
        self.talk_hold_since = None
        self.switch_req = None
        self.switch_soon = None                # game_since of the period whose "next up" was pre-announced
        self.fast = False
        self.react_wait = env_float("HUBIE_TURN_REACT_WAIT", 8)
        self.react_max = env_float("HUBIE_TURN_REACT_MAX", 20)
        self.talk_max = env_float("HUBIE_TURN_TALK_MAX", 30)
        self.turn_pause = env_float("HUBIE_TURN_PAUSE", 0.8)
        self.switch_wait = env_float("HUBIE_SWITCH_WAIT", 8)
        self.switch_lead = env_float("HUBIE_SWITCH_LEAD", 1.2)
        self.last_gate = ""

    @property
    def game(self):
        return self.games[self.gi]

    # ---- turn-taking with Hubie ----
    def new_rid(self):
        self.rid_seq = max(getattr(self, "rid_seq", 0) + 1, int(time.time() * 1000))
        return self.rid_seq

    def turn_gate(self):
        """(ok, why): may the next round (or a switch request) go ahead, or is Hubie still on the last one?"""
        now = time.time()
        t = bus.read_json(TURN, None)
        if not t or now - t.get("ts", 0) > 5:
            return True, "no brain"
        if self.last_rid and (t.get("released") or 0) < self.last_rid and t.get("since", 0) < self.last_result_t:
            speaking = t.get("react_rid") == self.last_rid and t.get("react_speaking")
            cap = self.react_max if speaking else self.react_wait
            if now - self.last_result_t < cap:
                return False, "reaction speaking" if speaking else "waiting for reaction"
            return self._gate_log(True, f"reaction wait capped at {cap:.0f} s")
        if now - (t.get("released_at") or 0) < self.turn_pause:
            return False, "pause after reaction"
        if t.get("speaking") or t.get("chat_busy"):
            if self.talk_hold_since is None:
                self.talk_hold_since = now
            if now - self.talk_hold_since < self.talk_max:
                return False, "chat" if t.get("chat_busy") else f"talking ({t.get('speaking_kind')})"
            return self._gate_log(True, f"talk hold capped at {self.talk_max:.0f} s")
        return True, ""

    def _gate_log(self, ok, why):
        print("turn:", why, flush=True)
        return ok, why

    def gate(self):
        ok, why = self.turn_gate()
        if why != self.last_gate and not ok:
            print("turn: hold -", why, flush=True)
        self.last_gate = why
        self.fast = not ok
        return ok

    def round_started(self, rid, game):
        self.talk_hold_since = None
        bus.append_jsonl(GAME_EVENTS, {"type": "round_start", "rid": rid, "game": game})

    def round_result(self, rid, res, adapter):
        self.last_rid, self.last_result_t = rid, time.time()
        bus.append_jsonl(GAME_EVENTS, {"type": "round", "adapter": adapter, "rid": rid, **res})

    def request_switch(self, why):
        gi = self.pick_next()
        if self.games[gi] is self.game:
            self.game_since = time.time()
            return
        sw = self.new_rid()
        self.switch_req = {"sw": sw, "gi": gi, "ts": time.time(), "why": why}
        print(f"switch requested {self.game.key} -> {self.games[gi].key} (sw {sw}), waiting for Hubie", flush=True)
        bus.append_jsonl(GAME_EVENTS, {"type": "game_state", "state": "switch_pending", "sw": sw,
                                       "from": self.game.label, "to": self.games[gi].label, "reason": why})

    def switch_due(self):
        """True once Hubie's "next up" line has been on air for a moment (or he never got to it)."""
        sr, now = self.switch_req, time.time()
        t = bus.read_json(TURN, None) or {}
        if not t or now - t.get("ts", 0) > 5:
            return True
        if t.get("switch_ack") == sr["sw"] and now - (t.get("switch_ack_at") or now) >= self.switch_lead:
            return True
        if now - sr["ts"] > self.switch_wait and t.get("switch_ack") != sr["sw"]:
            print(f"switch: no announcement after {self.switch_wait:.0f} s, switching anyway", flush=True)
            return True
        return now - sr["ts"] > self.switch_wait + 10       # announcement started but stage never confirmed

    # ---- state + flag ----
    def set_state(self, state, reason="", owner=False):
        if state != self.state or reason != self.reason:
            print(f"state {self.state} -> {state} {reason}", flush=True)
            if state == "live" and self.state != "live":
                bus.append_jsonl(GAME_EVENTS, {"type": "game_state", "state": "ready", "adapter": "housebets_" +
                                               self.game.key, "game": self.game.kind, "label": self.game.label})
            elif state != "switching" and self.state in ("live", "switching", "starting"):
                bus.append_jsonl(GAME_EVENTS, {"type": "game_state", "state": "blocked", "reason": reason,
                                               "label": self.ph.label, "adapter": "placeholder"})
            if state != self.state:
                self.state_since = time.time()
            self.state, self.reason = state, reason
        long_down = state not in ("live", "switching") and time.time() - self.state_since > 180
        if owner or long_down:
            self.flag(reason or state)
        elif state == "live":
            self.unflag()

    def flag(self, reason):
        text = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}  Housebets stream needs the owner: {reason}\n"
                f"Open the browser ({env('HUBIE_VIEWER_URL', 'the logged-in Chrome')}) "
                "and fix it there: tick the Cloudflare box yourself, or log in. The stream shows the placeholder "
                "game meanwhile and goes back to Housebets on its own.\n")
        try:
            old = NEEDS_OWNER.read_text() if NEEDS_OWNER.exists() else ""
        except OSError:
            old = ""
        if reason not in old:
            NEEDS_OWNER.parent.mkdir(exist_ok=True)
            NEEDS_OWNER.write_text(text)
            print("needs_owner:", reason, flush=True)
        self.flagged = True

    def unflag(self):
        if self.flagged or NEEDS_OWNER.exists():
            try:
                NEEDS_OWNER.unlink()
                print("needs_owner cleared", flush=True)
            except OSError:
                pass
            self.flagged = False

    # ---- picture ----
    def on_frame(self, cdp, ev):
        asyncio.ensure_future(self._ack(cdp, ev["sessionId"]))
        now = time.time()
        if now - self.live_ts < 1.0 / self.fps or not self.rect:
            return
        try:
            buf = np.frombuffer(base64.b64decode(ev["data"]), np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            md = ev.get("metadata", {})
            s = img.shape[1] / max(1.0, float(md.get("deviceWidth") or img.shape[1]))
            r = self.rect
            x0, y0 = int(max(0, r["x"] * s)), int(max(0, r["y"] * s))
            x1, y1 = int(min(img.shape[1], (r["x"] + r["width"]) * s)), int(min(img.shape[0], (r["y"] + r["height"]) * s))
            if x1 - x0 < 100 or y1 - y0 < 100:
                return
            self.live_img = prep(img[y0:y1, x0:x1])
            self.live_ts = now
        except Exception as e:
            print("frame error:", e, flush=True)

    @staticmethod
    async def _ack(cdp, sid):
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": sid})
        except Exception:
            pass

    async def writer(self):
        tmp = f"{GAME_FRAME}.tmp.jpg"
        last_write = 0
        while True:
            t = time.time()
            live = self.state in ("live", "switching") and self.live_img is not None and t - self.health_ts < 10
            if live:
                img = self.live_img if (self.live_ts > last_write or t - last_write > 0.9) else None
            else:
                if (t - self.ph_last_round > 9 and t - self.state_since > 6 and self.state != "starting"
                        and self.gate()):
                    self.ph_last_round = t
                    rid = self.new_rid()
                    self.round_started(rid, "dice")
                    res = self.ph.play_round()
                    self.round_result(rid, res, "placeholder")
                self.ph.note = "the Housebets table is back in a moment"
                img = self.ph.frame()
            if img is not None:
                cv2.imwrite(tmp, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
                os.replace(tmp, GAME_FRAME)
                last_write = t
            bus.write_json(GAME_STATUS, {"ts": t, "pid": os.getpid(), "adapter": "housebets_" + self.game.key
                                         if live else "placeholder", "label": self.game.label if live else
                                         self.ph.label, "state": self.state, "reason": self.reason,
                                         "rounds": self.rounds, "health_age": round(t - self.health_ts, 1),
                                         "frame_age": round(t - self.live_ts, 1)})
            await asyncio.sleep(max(0.0, 1.0 / self.fps - (time.time() - t)))

    # ---- main loop ----
    async def run(self):
        asyncio.ensure_future(self.writer())
        while True:
            # a fresh Playwright driver per session: stopping it drops the CDP connection without closing
            # the owner's Chrome (Browser.close() is never called on it)
            try:
                async with async_playwright() as pw:
                    await self.session(pw)
            except Exception as e:
                print("session ended:", repr(e)[:300], flush=True)
            self.set_state("down", "remote browser not reachable", owner=time.time() - self.health_ts > 120)
            await asyncio.sleep(10)

    async def session(self, pw):
        browser = await pw.chromium.connect_over_cdp(self.cdp_url, timeout=20000)
        ctx = browser.contexts[0]
        # the tab the owner's viewer shows (it uses the first tab); never open another window
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        cdp.on("Page.screencastFrame", lambda ev: self.on_frame(cdp, ev))
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 80, "everyNthFrame": 2})
        print("connected; tab:", page.url, flush=True)
        fails = 0
        try:
            while browser.is_connected() and not page.is_closed():
                t = time.time()
                try:
                    await asyncio.wait_for(self.tick(page, cdp), timeout=40)
                    fails = 0
                except Exception as e:      # e.g. "execution context destroyed" while the tab navigates
                    fails += 1
                    print(f"tick failed ({fails}):", repr(e)[:200], flush=True)
                    if fails >= 8:
                        raise
                await asyncio.sleep(max(0.2, (0.3 if self.fast else 1.5) - (time.time() - t)))
        finally:
            try:
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
            except Exception:
                pass

    async def tick(self, page, cdp):
        h = await asyncio.wait_for(page.evaluate(HEALTH), timeout=10)
        self.health_ts = time.time()
        g = self.game
        # 1. Cloudflare: hands off, owner ticks it in the viewer
        if h["challenge"]:
            return self.set_state("challenge", "Cloudflare check is showing on the Housebets tab", owner=True)
        # 2. dead tab / error page: reload, rate limited
        if h["errorPage"] or not h["url"].startswith("http"):
            self.set_state("error", "Housebets page failed to load")
            return await self.navigate(page, g.url(self.base), every=60)
        if "housebets.com" not in h["url"]:
            self.set_state("loading", "tab is on another site")
            return await self.navigate(page, g.url(self.base), every=30)
        # 3. login
        if not h["loggedIn"] and h["loginBtn"]:
            return self.set_state("login", "logged out of Housebets (log in again in the viewer)", owner=True)
        # 4. cookie banner
        if h["cookie"]:
            await page.evaluate(ACCEPT_COOKIES)
            print("cookie banner accepted", flush=True)
        # 5. rotation (between rounds only): Hubie announces the next game first, then it switches
        if self.switch_req:
            if not self.switch_due():
                self.fast = True
                return
            gi, why = self.switch_req["gi"], self.switch_req["why"]
            self.switch_req = None
            self.next_game(why, gi=gi)
            g = self.game
        elif len(self.games) > 1 and self.state == "live":
            due_in = self.rotate_secs - (time.time() - self.game_since)
            if due_in < 25 and self.switch_soon != self.game_since:
                # heads-up so the brain can voice its "next up" line ahead of time (edge-tts can take seconds)
                self.switch_soon = self.game_since
                nxt = self.games[self.pick_next()]
                if nxt is not self.game:
                    bus.append_jsonl(GAME_EVENTS, {"type": "game_state", "state": "switch_soon", "to": nxt.label})
            if due_in <= 0 and self.gate():
                self.request_switch("rotation")
                self.fast = True
                return
        # 6. on the right game page?
        if not g.on_page(h["url"]):
            if self.state in ("live", "switching"):
                self.set_state("switching", f"opening {g.label}")
            return await self.navigate(page, g.url(self.base), every=20)
        ok, why = await g.ready(page)
        if not ok:
            if not (self.state == "switching" and time.time() - self.state_since < 20):
                self.set_state("loading", f"{g.label}: {why}")
            if time.time() - self.state_since > 90 and len(self.games) > 1:
                self.bad_until[g.key] = time.time() + 1800
                self.next_game(f"{g.label} would not load ({why})")
            elif time.time() - self.state_since > 30:
                await self.navigate(page, g.url(self.base), every=90, reload=True)
            return
        self.rect = await g.frame_rect(page)
        if not self.rect:
            return self.set_state("loading", f"{g.label}: game area not found")
        # 7. something covering the game?
        cover = await page.evaluate(COVERED, self.rect)
        if cover:
            if time.time() - self.last_fix > 4:
                self.last_fix = time.time()
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.4)
                if h["overlayClose"]:
                    await page.evaluate(CLOSE_OVERLAY)
                print("popup over the game:", cover, flush=True)
            return
        if self.prepared is not g:
            await g.prepare(page)
            self.prepared = g
        if self.state != "unsafe":
            self.set_state("live")
        if time.time() - self.live_ts > 3:      # static page: no screencast frames, take one plain screenshot
            await self.plain_shot(page)
        # 8. a round, once Hubie is done with the last one
        gap = float(self.round_override or g.round_secs)
        self.fast = False
        if time.time() - self.last_round_t >= gap and self.gate():
            self.last_round_t = time.time()
            await self.round(page, g)

    async def round(self, page, g):
        rid = self.new_rid()
        self.round_started(rid, g.kind)
        res = None
        try:
            res = await asyncio.wait_for(g.play_round(page), timeout=25)
        except Unsafe as e:
            log_round(game=g.key, refused=str(e))
            return self.set_state("unsafe", f"refused to bet: {e}", owner=True)
        finally:
            if not res or "skipped" in res:      # no result: tell the brain the round is off
                bus.append_jsonl(GAME_EVENTS, {"type": "round_void", "rid": rid})
        if not res:
            return
        if "skipped" in res:
            log_round(game=g.key, **res)
            return
        self.rounds += 1
        if self.state == "unsafe":
            self.set_state("live")
        log_round(rid=rid, **res)
        self.round_result(rid, res, "housebets_" + g.key)

    def pick_next(self):
        gi = self.gi
        for _ in range(len(self.games)):
            gi = (gi + 1) % len(self.games)
            if self.bad_until.get(self.games[gi].key, 0) < time.time():
                break
        return gi

    def next_game(self, why, gi=None):
        old = self.game
        self.gi = self.pick_next() if gi is None else gi
        self.switch_req = None
        self.game_since = time.time()
        self.prepared = None
        if self.game is not old:
            print(f"switching {old.key} -> {self.game.key}: {why}", flush=True)
            bus.append_jsonl(GAME_EVENTS, {"type": "game_state", "state": "switching", "from": old.label,
                                           "to": self.game.label, "reason": why})

    async def navigate(self, page, url, every=30, reload=False):
        if time.time() - self.last_nav < every:
            return
        self.last_nav = time.time()
        print(("reload " if reload else "goto ") + url, flush=True)
        try:
            if reload:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            else:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print("navigation:", repr(e)[:200], flush=True)

    async def plain_shot(self, page):
        if time.time() - self.last_shot < 1:
            return
        self.last_shot = time.time()
        try:
            buf = await page.screenshot(type="jpeg", quality=80, timeout=5000)   # full tab, no clip
            img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
            vw = await page.evaluate("innerWidth")
            s = img.shape[1] / max(1, vw)
            r = self.rect
            crop = img[int(r["y"] * s):int((r["y"] + r["height"]) * s), int(r["x"] * s):int((r["x"] + r["width"]) * s)]
            if crop.size:
                self.live_img, self.live_ts = prep(crop), time.time()
        except Exception as e:
            print("screenshot:", repr(e)[:200], flush=True)


def main():
    asyncio.run(Runner().run())


if __name__ == "__main__":
    main()
