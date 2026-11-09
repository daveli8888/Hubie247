"""Brain loop: chat + game events -> moderation -> LLM (or stub) -> output filter -> edge-tts -> stage.

What Hubie says has to match what is on screen, so the brain and the game take turns (2026-11-09):

  game: round result (rid) -----> brain: builds a reaction to THAT round (LLM + TTS in a worker thread)
  game: holds the next round      brain: speaks it while the result is still on screen, then releases rid
  game: next round  <------------ (turn.json "released" = rid, plus a short pause)

  game: rotation due -> "switch_pending" (to=X) -> brain says "next up: X" -> turn.json "switch_ack"
  game: switches (card + navigation) only once the announcement has started (or after a cap)

Lines are built in worker threads, so the loop itself never blocks (it used to wait out LLM + edge-tts, up
to ~20 s, while rounds and switches went on without it). Each line is checked right before it is spoken:
  reaction   dropped if a newer round started or ended, the game changed, or it is HUBIE_REACT_STALE s old
  switch     dropped once the game is no longer about to switch / switching to that game
  idle       dropped if it is HUBIE_IDLE_STALE s old, a round is running, or the game changed
  chat       spoken in arrival order, never dropped unless HUBIE_CHAT_MAX_AGE s old (default 90)
Order when the stage is free: safety replies > switch announcement > reaction > chat > idle.
The game also holds its next round while Hubie is talking (chat answers included), capped so the show never
freezes; see Runner.turn_gate in housebets.py.
"""
import collections
import concurrent.futures
import hashlib
import itertools
import os
import queue
import random
import threading
import time

from . import bus, moderation, tts
from .chat_sources import start_sources
from .config import (BRAIN_STATUS, GAME_EVENTS, GAME_STATUS, LOG_DIR, RUN, STAGE_STATUS, STATE, TURN, env_float,
                     env_int)
from .llm import make_brain

LOG = LOG_DIR / "brain_events.jsonl"
CHAT_KINDS = ("chat", "greet", "canned")

# Voiced once at start (cached by text + voice in RUN/voice_cache): when edge-tts is too slow for a reaction,
# Hubie still says something true about the result instead of a silent caption.
FALLBACK = {
    ("win", "dice"): ["There it is, a win. Fun credits, but my heart rate went up a whole beat.",
                      "A win. I'll tip the hat in a moment. Several moments."],
    ("loss", "dice"): ["That one missed. No harm done, it's a zero bet.",
                       "No luck that roll. We carry on, slowly and with dignity."],
    ("win", "plinko"): ["Nice drop, that one paid. Fun credits, but still lovely to watch.",
                        "The ball found a good slot. I'll celebrate at sloth speed."],
    ("loss", "plinko"): ["The ball had other plans. Zero bet, zero worries.",
                         "That drop missed the good slots. On we go, nice and slow."],
    ("win", "mines"): ["Every tile a gem, and I cashed out like a gentleman. Fun credits, but lovely.",
                       "Gems only, then straight to the exit. Knowing when to leave is very sloth of me."],
    ("loss", "mines"): ["Found a mine. Zero bet, so the only damage is to my composure.",
                        "Boom. That tile was spoken for. Hat stays on, we go again."],
}


def game_of(text):
    """'dice' / 'plinko' / 'mines' from a game name or label (anything else counts as dice)."""
    low = (text or "").lower()
    return next((g for g in ("plinko", "mines") if g in low), "dice")


def log(**kw):
    bus.append_jsonl(LOG, kw)
    print(kw, flush=True)


class Brain:
    def __init__(self):
        self.llm = make_brain()
        self.q = queue.Queue()
        self.status = {}
        self.sources = start_sources(self.q, self.status)
        self.events = bus.Tail(GAME_EVENTS)
        self.ids = itertools.count(int(time.time() * 1000))
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="build")
        self.chat_rows = []        # shown in the chat panel
        self.speech = []           # last few speech items (stage picks up new ids)
        self.caption, self.prev_caption = {}, {}
        self.chat_q = collections.deque()   # chat waiting for a reply, oldest first
        self.jobs = {}             # slot ("chat"/"react"/"switch"/"idle") -> {"fut", "meta"} being built
        self.ready = {}            # slot -> built line waiting for the stage
        self.cur = None            # committed line (queued/playing on the stage)
        self.last_spoke = time.time()
        self.last_game_line = 0
        self.last_win_clip = 0
        self.last_loss_clip, self.rounds_since_loss_clip = 0, 0
        # the game as the brain knows it
        self.now_playing = "warming up"
        self.game_label = None     # last live Housebets game
        self.screen = None         # what is on screen now (a Housebets game or the placeholder)
        self.game_state = None     # ready / blocked / switching / switch_pending
        self.ready_ts = 0
        self.switch_req = None     # {"sw", "to", "ts"}: announce this before the game switches
        self.announced_sw = None
        self.switch_ack, self.switch_ack_at = None, 0
        self.switching_to = None
        self.announced = None      # label the last announcement named
        self.round_inflight, self.round_inflight_ts = None, 0   # a round that started and has no result yet
        self.last_round = None     # newest round result (with rid)
        self.released, self.released_at = 0, 0.0
        gs = bus.read_json(GAME_STATUS, {}) or {}     # game already running when the brain (re)starts
        if gs.get("label") and time.time() - gs.get("ts", 0) < 30:
            self.now_playing, self.game_label = f"now playing: {gs['label']}", gs["label"]
            self.screen = self.announced = gs["label"]
        self.clip_event = None
        self.greeted = set()
        self.idle_secs = env_float("HUBIE_IDLE_SECS", 25)
        self.game_gap = env_float("HUBIE_GAME_LINE_GAP", 20)
        self.react_stale = env_float("HUBIE_REACT_STALE", 7)     # a reaction must start within this of the result
        self.idle_stale = env_float("HUBIE_IDLE_STALE", 8)
        self.chat_max_age = env_float("HUBIE_CHAT_MAX_AGE", 90)
        self.backlog = env_int("HUBIE_CHAT_BACKLOG", 12)          # flood guard only; age is the real limit
        self.since = time.time()
        self.fallback = {}         # (kind, game) -> [(text, pcm)] once voiced; static last resort
        self.pool_lines = collections.defaultdict(collections.deque)   # (kind, game) -> fresh pre-voiced lines
        self.react_tts = env_float("HUBIE_REACT_TTS_SECS", 2.5)   # edge-tts time a reaction may take
        self.pool_used = []        # (pcm path, time used): deleted a minute later
        self.said = collections.deque(maxlen=40)   # recent lines, so a pre-voiced one is never a repeat
        (RUN / "voice_cache").mkdir(exist_ok=True)
        threading.Thread(target=self.voice_fallbacks, daemon=True).start()
        threading.Thread(target=self.voice_pool, daemon=True).start()
        self.stats = {"chat_in": 0, "dropped": 0, "canned": 0, "replied": 0, "out_blocked": 0, "lines": 0,
                      "stale_dropped": 0, "tts_failed": 0}

    # ---- state for the stage + game ----
    def publish(self):
        bus.write_json(STATE, {"ts": time.time(), "chat": self.chat_rows[-30:], "speech": self.speech[-6:],
                               "caption": self.caption, "prev_caption": self.prev_caption,
                               "now_playing": self.now_playing, "clip_event": self.clip_event})
        cur = self.cur
        speaking = bool(cur and not cur.get("done_at"))
        react_rid = self.last_round["rid"] if self.last_round and self.released < self.last_round["rid"] else None
        chat_busy = bool(self.chat_q or "chat" in self.jobs or "chat" in self.ready
                         or (speaking and cur["kind"] in CHAT_KINDS))
        bus.write_json(TURN, {
            "ts": time.time(), "since": self.since, "released": self.released, "released_at": self.released_at, "react_rid": react_rid,
            "react_speaking": bool(speaking and react_rid and cur.get("rid") == react_rid),
            "speaking": speaking, "speaking_kind": cur["kind"] if speaking else None, "chat_busy": chat_busy,
            "switch_ack": self.switch_ack, "switch_ack_at": self.switch_ack_at})
        bus.write_json(BRAIN_STATUS, {"ts": time.time(), "pid": os.getpid(), "llm": self.llm.name,
                                      "sources": self.sources, "status": self.status, "stats": self.stats,
                                      "pending": len(self.chat_q), "jobs": sorted(self.jobs),
                                      "ready": sorted(self.ready)})

    def release(self, rid, why):
        """Tell the game it may play the next round (Hubie is done with round rid)."""
        if rid and rid > self.released:
            self.released, self.released_at = rid, time.time()
            log(ev="release", rid=rid, why=why)

    def voice_fallbacks(self):
        d = RUN / "voice_cache"
        d.mkdir(exist_ok=True)
        for key, lines in FALLBACK.items():
            for text in lines:
                h = hashlib.sha1(f"{text}|{tts.VOICE}|{tts.RATE}|{tts.PITCH}".encode()).hexdigest()[:16]
                pcm = d / f"{h}.pcm"
                if not pcm.exists():
                    tmp = str(d / f"{h}.tmp.pcm")
                    if not tts.synth(text, tmp, total=30):
                        continue
                    os.replace(tmp, pcm)
                self.fallback.setdefault(key, []).append((text, str(pcm)))
        log(ev="fallback_voices", ready=sum(len(v) for v in self.fallback.values()))

    def voice_pool(self):
        """Keep two fresh, number-free reactions voiced per result for the game on screen, so a reaction never
        waits on a stalled edge-tts: if its own line is not voiced within HUBIE_REACT_TTS_SECS, one of these
        (true for any win/loss of that game) is said instead, each only once."""
        n = 0
        while True:
            game = game_of(self.screen)
            todo = [k for k in ("loss", "win") if len(self.pool_lines[(k, game)]) < 2]
            if not todo:
                time.sleep(1)
                continue
            kind = todo[0]
            try:
                text = self.llm.line(kind, game=game, generic=True)
                ok, res = moderation.check_outgoing(text) if text else (False, "")
                pooled = [t for dq in list(self.pool_lines.values()) for t, _ in list(dq)]
                if ok and len(res.split()) <= 24 and res not in self.said and res not in pooled:
                    n += 1
                    pcm = tts.synth(res, str(RUN / "voice_cache" / f"pool{os.getpid()}_{n}.pcm"), total=20)
                    if pcm:
                        self.pool_lines[(kind, game)].append((res, pcm))
                        continue
            except Exception as e:
                print("voice pool:", repr(e)[:200], flush=True)
            time.sleep(3)

    def take_pooled(self, kind, game):
        dq = self.pool_lines.get((kind, game))
        if dq:
            got = dq.popleft()
            self.pool_used.append((got[1], time.time()))
            return got, "pool"
        fb = [f for f in self.fallback.get((kind, game), []) if f[0] not in list(self.said)[-6:]] \
            or self.fallback.get((kind, game))
        return (random.choice(fb), "static") if fb else (None, None)

    # ---- building lines (worker threads) ----
    def build(self, sid, text, reply_to=None, trusted=False, kind="line", tts_total=18):
        """Filter + TTS (the slow part, ~1-10 s)."""
        if not trusted:
            ok, res = moderation.check_outgoing(text)
            if not ok:
                self.stats["out_blocked"] += 1
                log(ev="out_blocked", text=text, reason=res)
                text = moderation.SAFE_FALLBACK
            else:
                text = res
        pcm = tts.synth(text, str(RUN / "speech" / f"{sid}.pcm"), total=tts_total)
        if pcm is None:
            self.stats["tts_failed"] += 1
        return {"id": sid, "text": text, "pcm": pcm, "reply_to": reply_to, "kind": kind}

    def start(self, slot, fn, **meta):
        sid = next(self.ids)
        meta.update(sid=sid, started=time.time())
        self.jobs[slot] = {"fut": self.pool.submit(fn, sid), "meta": meta}

    def job_line(self, kind, tts_total=18, **kw):
        def fn(sid):
            item = self.build(sid, self.llm.line(kind, **kw), kind=kind, tts_total=tts_total)
            if item["pcm"] is None and kind in ("win", "loss"):
                # edge-tts too slow: a pre-voiced line that is still true for this result
                (got, src) = self.take_pooled(kind, game_of(kw.get("game")))
                if got:
                    log(ev="fallback_voice", kind=kind, game=kw.get("game"), src=src, text=got[0])
                    item.update(text=got[0], pcm=got[1], fallback=src)
            return item
        return fn

    def job_chat(self, p):
        game, last, r = self.screen or "", "", self.last_round
        if r and r.get("game") == "dice" and r.get("roll") is not None:
            last = f"rolled {r['roll']} ({'win' if r.get('win') else 'loss'})"
        elif r and r.get("multiplier") is not None:
            last = f"{r['multiplier']}x"

        def fn(sid):
            if p["kind"] == "canned":
                return self.build(sid, p["canned"], reply_to=p["user"], trusted=True, kind="canned")
            user, text = p["user"], p["text"]
            k = "greet" if user not in self.greeted and len(text.split()) <= 4 else "chat"
            self.greeted.add(user)
            line = self.llm.line(k, user=user, text=text, game=game, last=last)
            if user.lower() not in line.lower():
                line = f"{user}, {line}"
            self.stats["replied"] += 1
            return self.build(sid, line, reply_to=user, kind=k)
        return fn

    def collect(self):
        """Finished builds -> ready; a reaction that is taking too long is given up (the game goes on)."""
        now = time.time()
        for slot, job in list(self.jobs.items()):
            m = job["meta"]
            if job["fut"].done():
                del self.jobs[slot]
                try:
                    item = job["fut"].result()
                except Exception as e:
                    log(ev="build_failed", slot=slot, err=repr(e)[:200])
                    if slot == "react":
                        self.release(m.get("rid"), "build failed")
                    continue
                item.update({k: v for k, v in m.items() if k not in ("sid", "kind")})
                item["built"] = now
                self.ready[slot] = item
            elif slot == "react" and now - m["result_ts"] > self.react_stale:
                del self.jobs[slot]          # the thread finishes on its own; its line is thrown away
                self.stats["stale_dropped"] += 1
                log(ev="stale", slot=slot, why="reaction not ready in time", rid=m.get("rid"),
                    waited=round(now - m["result_ts"], 1))
                self.release(m.get("rid"), "reaction too slow")

    # ---- inputs ----
    def take_chat(self):
        while True:
            try:
                m = self.q.get_nowait()
            except queue.Empty:
                break
            self.stats["chat_in"] += 1
            v = moderation.check_incoming(m.user, m.text)
            log(ev="chat_in", user=m.user, text=m.text, source=m.source, action=v.action, reason=v.reason)
            if v.action == "drop":
                self.stats["dropped"] += 1
                continue
            if v.show:
                self.chat_rows.append({"user": m.user, "text": m.text})
            item = {"kind": "canned" if v.action == "canned" else "chat", "user": m.user, "text": m.text,
                    "canned": v.canned, "ts": m.ts}
            if v.action == "canned":
                self.stats["canned"] += 1
                n = sum(1 for p in self.chat_q if p["kind"] == "canned")   # safety first, in order among them
                self.chat_q.insert(n, item)
            else:
                self.chat_q.append(item)
        now = time.time()
        for p in list(self.chat_q):
            if p["kind"] == "chat" and now - p["ts"] > self.chat_max_age:
                self.chat_q.remove(p)
                self.stats["stale_dropped"] += 1
                log(ev="stale", slot="chat", why="chat too old", user=p["user"], text=p["text"])
        chats = [p for p in self.chat_q if p["kind"] == "chat"]
        for p in chats[: max(0, len(chats) - self.backlog)]:
            self.chat_q.remove(p)
        self.chat_rows = self.chat_rows[-40:]

    def take_game(self):
        for e in self.events.read():
            typ, now = e.get("type"), time.time()
            if typ == "game_state":
                st, label = e.get("state"), e.get("label")
                if st == "switch_soon":         # voice the "next up" line now, speak it when the switch is due
                    if e.get("to") and not self.switch_req:
                        self.jobs.pop("switch", None)
                        self.ready.pop("switch", None)
                        self.start("switch", self.job_line("switch", game=e["to"]), kind="switch", sw=None,
                                   label=e["to"], soon=True)
                elif st == "switch_pending":
                    self.switch_req = {"sw": e.get("sw"), "to": e.get("to"), "ts": e.get("ts", now)}
                    self.game_state = "switch_pending"
                elif st == "switching":
                    self.game_state, self.switching_to = "switching", e.get("to")
                elif st == "ready":
                    if label:
                        self.now_playing = f"now playing: {label}"
                    changed = label and label != self.game_label
                    self.game_state, self.ready_ts, self.switching_to = "ready", now, None
                    if self.switch_req and self.switch_req["to"] == label:
                        self.switch_req = None
                    if changed and label != self.announced and str(e.get("adapter", "")).startswith("housebets_"):
                        # the game changed without a "next up" (stream start, a game that would not load)
                        self.start("switch", self.job_line("now", game=label), kind="now", label=label)
                        self.announced = label
                    if label:
                        self.game_label = self.screen = label
                elif st == "blocked":
                    self.game_state, self.screen = "blocked", label or self.screen
                    if label:
                        self.now_playing = f"now playing: {label}"
                log(ev="game_state", **{k: v for k, v in e.items() if k != "ts"})
            elif typ == "round_start":
                self.round_inflight, self.round_inflight_ts = e.get("rid"), now
            elif typ == "round_void":           # the round produced no result (skipped, refused, timed out)
                if self.round_inflight == e.get("rid"):
                    self.round_inflight = None
            elif typ == "round":
                if self.round_inflight == e.get("rid"):
                    self.round_inflight = None
                e.setdefault("rid", next(self.ids))
                self.last_round = e
                log(ev="round", rid=e["rid"], game=e.get("game"), win=e.get("win"), roll=e.get("roll"),
                    mult=e.get("multiplier"), result_ts=e.get("ts"))
                if (e.get("win") and (e.get("multiplier") or 0) >= (env_float("HUBIE_WIN_CLIP_MIN_MULT_MINES", 1.25) if e.get("game") == "mines"
                                                 else env_float("HUBIE_WIN_CLIP_MIN_MULT", 1.5))
                        and now - self.last_win_clip > env_float("HUBIE_WIN_CLIP_GAP", 30)):
                    self.last_win_clip = now
                    e["clip"] = "win"
                    self.clip_event = {"id": next(self.ids), "clip": "win", "ts": now}
                    log(ev="clip", clip="win", roll=e.get("roll"), mult=e.get("multiplier"))
                self.rounds_since_loss_clip += 1
                if (e.get("win") is False and "clip" not in e      # a short "oh well" beat, now and then only
                        and self.rounds_since_loss_clip >= env_float("HUBIE_LOSS_CLIP_ROUNDS", 4)
                        and now - self.last_loss_clip > env_float("HUBIE_LOSS_CLIP_GAP", 60)
                        and now - self.last_win_clip > 12):
                    self.last_loss_clip, self.rounds_since_loss_clip = now, 0
                    self.clip_event = {"id": next(self.ids), "clip": "loss", "ts": now}
                    log(ev="clip", clip="loss", roll=e.get("roll"))
                self.decide_reaction(e)

    def decide_reaction(self, r):
        rid, now = r["rid"], time.time()
        old = self.jobs.pop("react", None)        # a newer result makes an older reaction stale
        if old:
            log(ev="stale", slot="react", why="newer round", rid=old["meta"].get("rid"))
        if self.ready.pop("react", None):
            self.stats["stale_dropped"] += 1
            log(ev="stale", slot="react", why="newer round (built)")
        if not (r.get("clip") or now - self.last_game_line > self.game_gap):
            return self.release(rid, "no reaction this round")
        age = now - r.get("ts", now)
        if age > self.react_stale:
            return self.release(rid, "result already old")
        self.last_game_line = now
        kind = "win" if r.get("win") else "loss"
        kw = dict(roll=r.get("roll"), target=r.get("target"), mult=r.get("multiplier"),
                  game=r.get("game", "dice"), condition=r.get("condition"),
                  picks=r.get("picks"), mines=r.get("mines"), gems=r.get("gems"))
        budget = max(1.5, min(self.react_tts, self.react_stale - age - 1.0))   # edge-tts stall -> pre-voiced line
        self.start("react", self.job_line(kind, tts_total=budget, **kw), kind=kind, rid=rid,
                   result_ts=r.get("ts", now), label=self.screen)

    # ---- scheduling ----
    def schedule(self):
        now = time.time()
        if self.round_inflight and now - self.round_inflight_ts > 40:
            self.round_inflight = None           # lost its result (game restarted mid-round)
        sr = self.switch_req
        if sr and self.announced_sw != sr["sw"]:
            self.announced_sw, self.announced = sr["sw"], sr["to"]
            pre = self.ready.get("switch") or (self.jobs.get("switch") or {}).get("meta")
            if pre and pre.get("soon") and pre.get("label") == sr["to"]:
                pre["sw"] = sr["sw"]                  # the line voiced at the heads-up: use it
            else:
                self.jobs.pop("switch", None)
                self.ready.pop("switch", None)
                self.start("switch", self.job_line("switch", game=sr["to"]), kind="switch", sw=sr["sw"],
                           label=sr["to"])
        if self.chat_q and "chat" not in self.jobs and "chat" not in self.ready:
            p = self.chat_q.popleft()        # one at a time keeps the replies in order
            self.start("chat", self.job_chat(p), kind=p["kind"], user=p["user"], msg_ts=p["ts"])
        busy = self.jobs or self.ready or self.chat_q or not self.stage_free()
        if (not busy and now - self.last_spoke > self.idle_secs and self.round_inflight is None
                and self.game_state != "switch_pending"):
            self.start("idle", self.job_line("idle", game=game_of(self.screen)), kind="idle", label=self.screen)
            self.last_spoke = now            # don't start another while this one builds

    def valid(self, slot, it):
        """(ok, why) right before speaking: does the line still match what is on screen?"""
        now = time.time()
        if slot == "react":
            r = self.last_round
            if not r or r["rid"] != it["rid"]:
                return False, "newer round"
            if self.round_inflight and self.round_inflight != it["rid"]:
                return False, "next round already running"
            if it.get("label") != self.screen or self.game_state == "switching":
                return False, "game changed"
            if now - it["result_ts"] > self.react_stale:
                return False, f"result is {now - it['result_ts']:.1f} s old"
            return True, ""
        if slot == "switch":
            if it["kind"] == "now":
                ok = self.screen == it["label"] and self.game_state == "ready" and now - self.ready_ts < 15
                return ok, "" if ok else "game changed again"
            sr = self.switch_req
            if sr and sr["sw"] == it.get("sw"):
                return True, ""
            if self.game_state == "switching" and self.switching_to == it["label"]:
                return True, ""              # the "next up" card shows that game: still true
            return False, "switch already over"
        if slot == "idle":
            if now - it["started"] > self.idle_stale:
                return False, "idle line too old"
            if self.round_inflight or (self.last_round and self.released < self.last_round["rid"]):
                return False, "a round is on"
            if it.get("label") != self.screen or self.game_state in ("switching", "switch_pending"):
                return False, "game changed"
            return True, ""
        if slot == "chat" and it["kind"] != "canned" and now - it["msg_ts"] > self.chat_max_age:
            return False, "chat too old"
        return True, ""

    def pick(self):
        """The stage is free: speak the most urgent line that still matches the screen."""
        # a running round, or a reaction still being voiced, goes first; chat and idle wait for it
        inflight = (self.round_inflight and time.time() - self.round_inflight_ts < 15) or "react" in self.jobs
        for slot in ("canned", "switch", "react", "chat", "idle"):
            s = "chat" if slot == "canned" else slot
            it = self.ready.get(s)
            if not it or (slot == "canned" and it["kind"] != "canned"):
                continue
            if s == "switch" and it.get("soon") and not it.get("sw"):
                if time.time() - it["started"] < 120:
                    continue          # voiced ahead of a switch: wait for the switch to be due
            ok, why = self.valid(s, it)
            if not ok:
                del self.ready[s]
                self.stats["stale_dropped"] += 1
                log(ev="stale", slot=s, why=why, text=it["text"], age=round(time.time() - it["started"], 1))
                if s == "react":
                    self.release(it.get("rid"), "stale")
                continue
            if s in ("chat", "idle") and inflight:
                return      # its result gets the reaction first, chat right after
            del self.ready[s]
            return self.commit(it)

    def commit(self, item):
        sid, text, reply_to, kind = item["id"], item["text"], item["reply_to"], item["kind"]
        item["ts"] = time.time()
        self.speech.append({k: item[k] for k in ("id", "text", "pcm", "reply_to", "kind", "ts")})
        self.said.append(text)
        self.prev_caption = self.caption
        self.caption = {"text": text, "reply_to": reply_to, "speech_id": sid}
        if reply_to:      # the stage shows this bubble only once the line starts playing
            self.chat_rows.append({"user": "Hubie", "text": text, "hubie": True, "speech_id": sid})
        self.cur = item
        self.last_spoke = time.time()
        self.stats["lines"] += 1
        lag = {}
        if item.get("result_ts"):
            lag.update(rid=item["rid"], since_result=round(item["ts"] - item["result_ts"], 2))
        if item.get("msg_ts"):
            lag.update(user=item.get("user"), since_msg=round(item["ts"] - item["msg_ts"], 2))
        if item.get("sw"):
            lag.update(sw=item["sw"])
        log(ev="say", kind=kind, text=text, reply_to=reply_to, tts=bool(item["pcm"]), id=sid,
            built_in=round(item["built"] - item["started"], 2), **lag)
        self.cleanup_speech()
        self.publish()

    def track_stage(self):
        """Follow the committed line on the stage: started -> switch ack; done -> release its round."""
        cur = self.cur
        if cur is None or cur.get("done_at"):
            return
        st = bus.read_json(STAGE_STATUS, {}) or {}
        now = time.time()
        if not cur.get("started_at") and cur["id"] in (st.get("speech_started") or []):
            cur["started_at"] = now
            if cur["kind"] == "switch" and cur.get("sw"):
                self.switch_ack, self.switch_ack_at = cur["sw"], now
                log(ev="switch_ack", sw=cur["sw"], to=cur.get("label"))
        waited = now - cur["ts"]
        done = (st.get("speech_done") == cur["id"]
                or (not cur.get("started_at") and waited > 10)    # stage restarted and skipped it
                or waited > 60)                                     # stuck: don't wait forever
        if done:
            cur["done_at"] = self.last_spoke = now
            if cur["kind"] == "switch" and cur.get("sw") and self.switch_ack != cur["sw"]:
                self.switch_ack, self.switch_ack_at = cur["sw"], now
            if cur.get("rid"):
                self.release(cur["rid"], "reaction spoken")

    def stage_free(self):
        return self.cur is None or bool(self.cur.get("done_at"))

    def cleanup_speech(self):
        while self.pool_used and time.time() - self.pool_used[0][1] > 60:
            try:
                os.unlink(self.pool_used.pop(0)[0])
            except OSError:
                pass
        keep = {str(s["id"]) for s in self.speech[-6:]}
        keep |= {str(it["id"]) for it in self.ready.values()}
        keep |= {str(j["meta"]["sid"]) for j in self.jobs.values()}
        for f in (RUN / "speech").glob("*"):
            if f.name.split(".")[0] not in keep:
                try:
                    f.unlink()
                except OSError:
                    pass

    # ---- main ----
    def run(self):
        self.publish()
        log(ev="start", llm=self.llm.name, sources=self.sources)
        while True:
            self.step()
            time.sleep(0.1)

    def step(self):
        self.take_chat()
        self.take_game()
        self.collect()
        self.track_stage()
        self.schedule()
        if self.stage_free():
            self.pick()
        self.publish()


def main():
    try:
        Brain().run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
