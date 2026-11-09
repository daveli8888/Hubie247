"""Hubie's cam: clip states, voice-driven talking and small "alive" touches.

Clips (assets/clips/clips.json, built by tools/build_clips.py, all in one locked framing so any frame can blend
into any other):
  idle  played as a random walk, not a 10 s loop: forward or backward between calm points (where his motion is
        slow, so turning round reads as natural, easing down and back up), plus low-difference jumps hidden in
        a short blend. No fixed cycle, so it does not visibly repeat.
  talk  shown only while his voice is actually sounding (pauses under ~0.7 s keep it). There can be several
        talk loops ("role": "talk"); each line picks one (the one closest to the frame on screen, with some
        chance). The mouth is re-chosen 15x a second from that clip's own mouth shapes to match the voice
        loudness (closed in silence) and pasted onto the playing talk frame at the tracked nose position.
  win   one-shot: starts at the frame closest to what is on screen, blends in and back out. Several win clips
        ("role": "win", "weight") take turns at random, never the same one twice in a row.
  loss  one-shot built from idle frames: a slowed, eyes-closed "oh well" with a small head tilt and a sigh.
All state changes blend (0.3-0.5 s) and each blend lands on the closest-matching frame of the new state.
On top: slow breathing (body only), a soft warm light flicker and a few dust motes, all very small.
"""
import json
import math
import os
import random
import subprocess

import cv2
import numpy as np

from .config import ROOT


def decode(path, w, h, fps):
    """Decode a clip to a list of RGB frames at box size and stream fps (audio ignored)."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-an",
           "-vf", f"fps={fps},scale={w}:{h}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    n = len(raw) // (w * h * 3)
    arr = np.frombuffer(raw[: n * w * h * 3], np.uint8).reshape(n, h, w, 3)
    return [arr[i] for i in range(n)]


class FrameStore:
    """A clip's frames at cam size, kept in RAM as YUV 4:2:0 (half the bytes of RGB, so five clips fit in what
    three took before) and turned back into RGB on use, with a small cache (a frame is typically used a few
    times in a row: 24 fps clip in a 30 fps stream, blends, mouth patches). Decoded frame by frame, so loading
    never holds a whole raw clip at once. Also builds the matching thumbnails on the way."""

    CACHE = 6

    def __init__(self, path, w, h, fps, limit=None):
        self.w, self.h = w, h
        self.h2 = h + (h & 1)                         # I420 needs an even height
        cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-an",
               "-vf", f"fps={fps},scale={w}:{h}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        size = w * h * 3
        self.yuv, th = [], []
        while limit is None or len(self.yuv) < limit:
            raw = p.stdout.read(size)
            if len(raw) < size:
                break
            rgb = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
            th.append(thumb(rgb))
            if self.h2 != h:
                rgb = np.concatenate([rgb, rgb[-1:]], 0)
            self.yuv.append(cv2.cvtColor(rgb, cv2.COLOR_RGB2YUV_I420))
        p.kill()                                      # may stop early (limit): no need for the rest
        p.stdout.close()
        p.wait()
        self.th = np.stack(th)
        self.cache = {}

    def __len__(self):
        return len(self.yuv)

    def __getitem__(self, i):
        f = self.cache.get(i)
        if f is None:
            f = cv2.cvtColor(self.yuv[i], cv2.COLOR_YUV2RGB_I420)[: self.h]
            if len(self.cache) >= self.CACHE:
                self.cache.pop(next(iter(self.cache)))
            self.cache[i] = f
        return f

    def nbytes(self):
        return sum(y.nbytes for y in self.yuv)


def blend(a, b, k):
    """k=0 -> a, k=1 -> b."""
    if k <= 0:
        return a
    if k >= 1:
        return b
    return cv2.addWeighted(a, 1.0 - k, b, k, 0)


def ease(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


TW, TH = 64, 36


_yy, _xx = np.mgrid[0:TH, 0:TW].astype(np.float32)
TH_W = 0.35 + 0.65 * np.exp(-(((_xx - TW * 0.5) / (TW * 0.22)) ** 2 + ((_yy - TH * 0.5) / (TH * 0.55)) ** 2))


def thumb(f):
    """Small grey thumbnail, Hubie weighted over the room (for matching frames across clips)."""
    return (cv2.resize(cv2.cvtColor(f, cv2.COLOR_RGB2GRAY), (TW, TH), interpolation=cv2.INTER_AREA)
            .astype(np.float32) * TH_W).ravel()


def dist(a, b):
    return np.sqrt(np.maximum(0, (a * a).sum(1)[:, None] + (b * b).sum(1)[None, :] - 2 * a @ b.T) / a.shape[1])


# ---- speech envelope (the stage's Audio computes it once per line) ------------------------------
ENV_HZ = 100


def speech_envelope(pcm, rate):
    """int16 (n,2) -> (mouth open 0..1, talk wanted bool), both at 100 Hz.
    open: loudness with fast attack / slower release, normalised per line. talk: voiced, with pauses under
    0.7 s bridged, from 0.12 s before the first sound to 0.15 s after the last."""
    hop = rate // ENV_HZ
    x = pcm.astype(np.float32).mean(axis=1) if pcm.ndim == 2 else pcm.astype(np.float32)
    n = len(x) // hop
    if n == 0:
        return np.zeros(0, np.float32), np.zeros(0, bool)
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    loud = rms[rms > 150]
    ref = float(np.percentile(loud, 90)) if len(loud) > 10 else 3000.0
    e = np.clip(rms / max(ref, 400.0), 0, 1.3)
    sm = np.empty(n, np.float32)
    v = 0.0
    for i in range(n):                    # attack ~20 ms, release ~70 ms
        v += (e[i] - v) * (0.55 if e[i] > v else 0.14)
        sm[i] = v
    open_ = np.clip((sm - 0.10) / 0.75, 0, 1) ** 0.8
    voiced = sm > 0.12
    talk = voiced.copy()
    idx = np.flatnonzero(voiced)
    if len(idx):
        for g in np.flatnonzero(np.diff(idx) > 1):        # bridge short pauses
            a, b = idx[g], idx[g + 1]
            if (b - a) / ENV_HZ < 0.7:
                talk[a:b] = True
        pre, post = int(0.12 * ENV_HZ), int(0.15 * ENV_HZ)
        k = np.convolve(talk.astype(np.float32), np.ones(pre + post + 1), mode="full")
        talk = k[post:post + n] > 0                        # on from pre before to post after
    return open_.astype(np.float32), talk


# ---- loops --------------------------------------------------------------------------------------
class Walker:
    """Random walk through a loop clip: forward/backward, turning round only at calm frames (slowing into the
    turn and back up), and low-difference jumps hidden in a short blend."""

    TURN = 8              # stream frames to slow down, and again to speed back up
    GHOST = 6             # stream frames a jump is blended over

    def __init__(self, frames, th, step, fps, rng, wraps, cfg):
        self.f, self.n, self.v0, self.fps, self.rng, self.wraps = frames, len(frames), step, fps, rng, wraps
        n = self.n
        D = dist(th, th)
        med = float(np.median([D[i, i + 1] for i in range(n - 1)]))
        vel = np.array([D[(i - 1) % n, (i + 1) % n] for i in range(n)]) / (2 * med)
        if not wraps:
            vel[:4] = vel[-4:] = 99
        calm = float(cfg.get("calm", 0.85))
        self.nodes = [i for i in range(n) if vel[i] <= calm and vel[i] <= vel[(i - 1) % n] and vel[i] <= vel[(i + 1) % n]]
        self.node_set = set(self.nodes)
        # jumps: (i, dir) -> [(j, new dir, cost)]: the two frames after the jump look like the two it replaces
        thr = float(cfg.get("jump_cost", 3.0))
        self.jumps = {}
        idx = np.arange(n)
        for d in (1, -1):
            a1, a2 = (idx + d) % n, (idx + 2 * d) % n
            for d2 in (1, -1):
                C = (D[a1] + D[a2][:, (idx + d2) % n]) / (2 * med)
                for i in range(n):
                    if not wraps and not (0 <= i + 2 * d < n):
                        continue
                    for j in np.flatnonzero(C[i] < thr):
                        gap = abs(int(j) - i)
                        gap = min(gap, n - gap) if wraps else gap
                        if gap < 30 or (not wraps and not (6 <= j + 6 * d2 < n - 6)):
                            continue
                        self.jumps.setdefault((i, d), []).append((int(j), d2, float(C[i, j])))
        self.p_rev = float(cfg.get("p_reverse", 0.35))
        self.p_jump = float(cfg.get("p_jump", 0.12))          # per eligible frame
        self.min_gap = float(cfg.get("min_segment", 1.4))     # s between decisions
        self.pos = float(rng.choice(self.nodes)) if self.nodes else float(n // 2)
        self.dir = 1
        self.speed = step
        self.turn = None
        self.ghost = None
        self.since = 0
        self.log = []             # (stream frame, event) for QA

    def seat(self, i):
        """Continue from frame i (after a blend from another state); head towards the side with more room."""
        self.pos, self.turn, self.ghost, self.speed, self.since = float(i), None, None, self.v0, 0
        if not self.wraps:
            self.dir = 1 if i < self.n / 2 else -1

    def idx(self, p):
        return int(p) % self.n if self.wraps else int(min(self.n - 1, max(0, p)))

    def current(self):
        return self.idx(self.pos + 0.5)

    def _node_ahead(self, lim):
        for k in range(1, int(lim) + 2):
            if self.idx(self.pos + self.dir * k) in self.node_set:
                return k
        return None

    def step(self):
        self.since += 1
        v0 = self.v0
        if self.turn is not None:
            t = self.turn
            self.turn += 1
            if t < self.TURN:
                self.speed = v0 * (1 - (t + 1) / self.TURN)
            elif t == self.TURN:
                self.dir, self.speed = -self.dir, 0.0
            else:
                self.speed = v0 * min(1.0, (t - self.TURN) / self.TURN)
                if t >= 2 * self.TURN:
                    self.turn, self.speed = None, v0
        self.pos += self.dir * self.speed
        if self.wraps:
            self.pos %= self.n
        else:
            self.pos = min(self.n - 1.0, max(0.0, self.pos))
        if self.ghost:
            gp, gd, k = self.ghost
            self.ghost = (gp + gd * v0, gd, k + 1) if k + 1 < self.GHOST else None
        if self.turn is not None or self.ghost is not None:
            return
        brake = v0 * (self.TURN + 1) / 2              # frames travelled while slowing to a stop
        if not self.wraps:                            # a clip that does not loop: turn before its end
            ahead = (self.n - 1 - self.pos) if self.dir > 0 else self.pos
            later = [x for x in self.nodes if (x > self.pos + brake + 1.5 if self.dir > 0 else x < self.pos - brake - 1.5)]
            k = self._node_ahead(brake + 1)
            if (not later and k is not None and k >= brake - 1) or ahead < brake + 2:
                self.turn, self.since = 0, 0
                return
        if self.since < self.min_gap * self.fps:
            return
        opts = self.jumps.get((self.idx(self.pos), self.dir))
        if opts and self.rng.random() < self.p_jump:
            j, d2, _ = min(opts, key=lambda o: o[2] + self.rng.random())
            self.ghost = (self.pos, self.dir, 0)
            self.pos, self.dir, self.since = float(j), d2, 0
            return
        k = self._node_ahead(brake + 1)
        if k is not None and abs(k - brake) < 1.0 and self.rng.random() < self.p_rev:
            self.turn, self.since = 0, 0

    def frame(self):
        p = self.pos
        if self.speed < 0.6 * self.v0:                # slow (turning): blend neighbours so it glides, not steps
            i0 = math.floor(p)
            out = blend(self.f[self.idx(i0)], self.f[self.idx(i0 + 1)], p - i0)
        else:
            out = self.f[self.idx(p)]
        if self.ghost:
            gp, _, k = self.ghost
            out = blend(self.f[self.idx(gp)], out, ease((k + 1) / self.GHOST))
        return out


# ---- alive touches ------------------------------------------------------------------------------
class Alive:
    """Breathing (+ the loss beat's tilt and sigh) as one gentle remap, a warm light swell, dust motes."""

    def __init__(self, w, h, cfg, rng):
        self.w, self.h, self.rng = w, h, rng
        gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
        self.gx, self.gy = gx, gy
        # breathing: vertical stretch anchored at the bottom, Hubie's column only, zero at the frame edges
        wx = np.exp(-((gx - w * 0.5) / (w * 0.26)) ** 2)
        wy = np.clip(gy / (h * 0.18), 0, 1) ** 2
        self.B = ((h - gy) * wx * wy).astype(np.float32)
        # head tilt: rotation about the neck, head + hat only, soft edges
        px, py = w * 0.497, h * 0.56
        hm = np.zeros((h, w), np.float32)
        cv2.ellipse(hm, (int(px), int(h * 0.27)), (int(w * 0.16), int(h * 0.33)), 0, 0, 360, 1.0, -1)
        hm = cv2.GaussianBlur(hm, (0, 0), w * 0.035)
        self.Tx = ((gy - py) * hm).astype(np.float32)
        self.Ty = (-(gx - px) * hm).astype(np.float32)
        # warm lamp light from the top right (where the room's light rays come from)
        lm = np.exp(-(((gx - w * 0.92) / (w * 0.55)) ** 2 + ((gy + h * 0.1) / (h * 0.9)) ** 2))
        self.glow = np.clip(lm[..., None] * np.array([255, 178, 96], np.float32) / 255 * 40, 0, 255).astype(np.uint8)
        self.sprites = {}
        for r in (1.0, 1.4, 1.8):
            s = int(math.ceil(r * 3)) * 2 + 1
            yy, xx = np.mgrid[0:s, 0:s].astype(np.float32) - s // 2
            self.sprites[r] = np.exp(-(xx ** 2 + yy ** 2) / (2 * r * r))[..., None] * np.array([255, 236, 200], np.float32)
        self.breath_amp = float(cfg.get("breath", 0.009))
        self.breath_period = float(cfg.get("breath_period", 4.2))
        self.flicker = float(cfg.get("flicker", 1.0))
        self.n_motes = int(cfg.get("motes", 6))
        self.motes = []
        self.ph = [rng.uniform(0, 6.28) for _ in range(6)]

    def noise(self, t, k):
        """Slow, smooth value in about -1..1: three slow sines, nothing faster than ~0.8 Hz (never jittery)."""
        p = self.ph
        return (0.5 * math.sin(2 * math.pi * 0.11 * t * k + p[0]) + 0.3 * math.sin(2 * math.pi * 0.29 * t * k + p[1])
                + 0.2 * math.sin(2 * math.pi * 0.71 * t * k + p[2]))

    def _mote(self, t, fresh):
        rng = self.rng
        return {"x": rng.uniform(0.08, 0.92) * self.w, "y": rng.uniform(0.05, 0.8) * self.h,
                "vx": rng.uniform(-3, 3), "vy": rng.uniform(-1.0, 3.0), "t0": t - (0 if fresh else rng.uniform(0, 7)),
                "life": rng.uniform(7, 13), "r": rng.choice(list(self.sprites)), "a": rng.uniform(0.15, 0.35),
                "ph": rng.uniform(0, 6.28)}

    def apply(self, img, t, tilt=0.0, sink=0.0):
        ph = (t % self.breath_period) / self.breath_period
        a = self.breath_amp * (0.5 - 0.5 * math.cos(2 * math.pi * ph ** 0.85)) - sink   # in a bit quicker than out
        if abs(tilt) > 1e-5:
            img = cv2.remap(img, self.gx + tilt * self.Tx, self.gy + a * self.B + tilt * self.Ty, cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REPLICATE)
        elif abs(a) > 1e-5:
            img = cv2.remap(img, self.gx, self.gy + a * self.B, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        else:
            img = img.copy()
        if self.flicker:
            f = self.flicker * 0.10 * (self.noise(t, 1.0) + 0.3 * self.noise(t, 2.3))
            if abs(f) > 0.002:
                img = cv2.addWeighted(img, 1.0 + 0.02 * f, self.glow, f, 0)
        if self.n_motes:
            while len(self.motes) < self.n_motes:
                self.motes.append(self._mote(t, fresh=bool(self.motes)))
            keep = []
            for m in self.motes:
                age = t - m["t0"]
                if age > m["life"]:
                    continue
                keep.append(m)
                al = m["a"] * min(1.0, age / 2.5, (m["life"] - age) / 2.5)
                sp = self.sprites[m["r"]]
                s = sp.shape[0]
                x0 = int(m["x"] + m["vx"] * age + 3 * math.sin(age * 0.5 + m["ph"])) - s // 2
                y0 = int(m["y"] + m["vy"] * age) - s // 2
                if al <= 0 or x0 < 0 or y0 < 0 or x0 + s > self.w or y0 + s > self.h:
                    continue
                roi = img[y0:y0 + s, x0:x0 + s]
                roi[:] = np.minimum(255, roi + sp * al).astype(np.uint8)
            self.motes = keep
        return img


# ---- the player ---------------------------------------------------------------------------------
class ClipPlayer:
    def __init__(self, w, h, fps, config_path=None, seed=None):
        self.w, self.h, self.fps = w, h, fps
        # clips are 24 fps sources: kept at that rate in RAM and stepped through at cfps/fps per stream frame
        self.cfps = min(float(fps), float(os.environ.get("HUBIE_CLIP_FPS") or 24))
        self.step = self.cfps / fps
        cfg_path = config_path or ROOT / "assets" / "clips" / "clips.json"
        self.cfg = {k: v for k, v in json.loads(open(cfg_path).read()).items() if isinstance(v, dict)}
        self.rng = random.Random(seed)
        self.frames = {}
        for name, c in self.cfg.items():
            f = c.get("file")
            if f and (ROOT / f).exists():
                # one-shots only keep what they play (+1 s): "end" is in 24 fps source frames
                lim = int((int(c["end"]) + 25) * self.cfps / 24) if "end" in c else None
                self.frames[name] = FrameStore(ROOT / f, w, h, self.cfps, lim)
        if "idle" not in self.frames:
            raise SystemExit("idle clip missing: set clips.json idle.file")
        self.th = {k: v.th for k, v in self.frames.items()}
        self.talks = [n for n, c in self.cfg.items() if n in self.frames and (n == "talk" or c.get("role") == "talk")]
        self.loops = {name: Walker(self.frames[name], self.th[name], self.step, fps, self.rng,
                                   bool(self.cfg[name].get("wraps")), self.cfg[name])
                      for name in ["idle"] + self.talks}
        self.mouths = {}
        meta_p = ROOT / "assets" / "clips" / "anim.json"
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {"clips": {}}
        for name in self.talks:
            m = meta["clips"].get(name, {}).get("mouth")
            if m and len(m) == len(self.frames[name]):
                self.mouths[name] = self._mouth_setup(np.array(m, np.float32), float(meta.get("zoom", 1.22)),
                                                      meta["size"][0])
        alive = self.cfg.get("alive", {})
        self.alive = Alive(w, h, alive, self.rng) if alive.get("on", True) else None
        self.base = "idle"
        self.base_prev = None        # loop we are blending away from (it keeps playing during the blend)
        self.base_fade = 1.0
        self.base_dur = 0.4
        self.base_since = 0
        self.shot = None
        self.level = 0.0
        self.t = 0
        self.events = []             # (stream frame, event) for QA

    # --- mouth -------------------------------------------------------------------------------------
    def _mouth_setup(self, m, zoom, src_w):
        """Mouth patch geometry (cam px) around the nose; the mask reaches below a dropped jaw so a closed
        mouth fully covers an open one."""
        w, h = self.w, self.h
        u = w / src_w * zoom                       # pre-crop reference px -> cam px
        box = (int(-48 * u), int(48 * u), int(-6 * u), int(60 * u))
        pw, ph = box[1] - box[0], box[3] - box[2]
        mask = np.zeros((ph, pw), np.float32)
        cv2.ellipse(mask, (pw // 2, int(24 * u) - box[2]), (int(37 * u), int(26 * u)), 0, 0, 360, 1.0, -1)
        return {"nose": np.stack([m[:, 0] * w, m[:, 1] * h], 1), "open": m[:, 2], "box": box,
                "mask": cv2.GaussianBlur(mask, (0, 0), 3.5 * u)[..., None], "tick": 0, "cur": 0}

    def _pick_mouth(self, st, body, target):
        st["tick"] += 1
        if st["tick"] % 2:                            # 15 mouth changes a second, like drawn lip-sync
            return st["cur"]
        op, nose = st["open"], st["nose"]
        nxt = (st["cur"] + 1) % len(op)
        if abs(op[nxt] - target) < 0.12:              # keep playing the current mouth while it fits
            j = nxt
        else:
            pose = np.hypot(*(nose - nose[body]).T)
            jitter = np.fromiter((self.rng.random() for _ in range(len(op))), np.float32, len(op))
            j = int(np.argmin(np.abs(op - target) + 0.03 * pose + 0.05 * jitter))
        st["cur"] = j
        return j

    def _paste_mouth(self, img, name, st, body, j):
        if j == body:
            return img
        x0b, x1b, y0b, y1b = st["box"]
        nose = st["nose"]
        x0, y0 = int(round(nose[body][0])) + x0b, int(round(nose[body][1])) + y0b
        pw, ph = x1b - x0b, y1b - y0b
        if x0 < 0 or y0 < 0 or x0 + pw > self.w or y0 + ph > self.h:
            return img
        dx, dy = nose[body] - nose[j]
        M = np.float32([[1, 0, x0 - dx], [0, 1, y0 - dy]])
        patch = cv2.warpAffine(self.frames[name][j], M, (pw, ph), flags=cv2.WARP_INVERSE_MAP | cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)
        out = img.copy()
        dst = out[y0:y0 + ph, x0:x0 + pw]
        m = st["mask"]
        dst[:] = (dst * (1 - m) + patch * m).astype(np.uint8)
        return out

    # --- control -----------------------------------------------------------------------------------
    def trigger(self, name):
        """Start a one-shot (win/loss). False if that clip is not available."""
        wins = [n for n, c in self.cfg.items() if n in self.frames and (n == "win" or c.get("role") == "win")]
        if name == "win" and len(wins) > 1:
            pool = [n for n in wins if n != getattr(self, "last_win", None)] or wins
            name = self.rng.choices(pool, [float(self.cfg[n].get("weight", 1)) for n in pool])[0]
            self.last_win = name
        c = self.cfg.get(name) or {}
        if name in self.frames and name not in self.loops:
            n = len(self.frames[name])
            lead = int(float(c.get("match_first", 0.5)) * self.cfps)    # may skip a calm lead-in to match
            cur = self._visible_thumb()
            start = int(np.argmin(dist(cur[None], self.th[name][:lead + 1])[0])) if lead else 0
            self.shot = {"name": name, "src": name, "pos": float(start), "end": min(n - 1, int(c.get("end", n - 1))),
                         "speed": 1.0, "k": 0.0, "xin": float(c.get("xfade_in", 0.35)),
                         "xout": float(c.get("xfade_out", 0.5)), "out": None, "t": 0}
        elif name == "loss" and c.get("from") in self.frames:
            a, b = c.get("frames", [118, 146])
            self.shot = {"name": "loss", "src": c["from"], "pos": float(a), "end": int(b),
                         "speed": float(c.get("speed", 0.45)), "k": 0.0, "xin": float(c.get("xfade_in", 0.4)),
                         "xout": float(c.get("xfade_out", 0.45)), "out": None, "t": 0,
                         "tilt": math.radians(float(c.get("tilt_deg", 2.0))), "sink": float(c.get("sink", 0.010))}
            self.shot["total"] = (b - a) / (self.step * self.shot["speed"])
        else:
            return False
        self.events.append((self.t, "shot " + name))
        return True

    def available(self):
        names = set(self.frames)
        if (self.cfg.get("loss") or {}).get("from") in self.frames:
            names.add("loss")
        return sorted(names)

    # --- rendering ---------------------------------------------------------------------------------
    def _visible_thumb(self):
        sh = self.shot
        if sh and sh["k"] > 0.5:
            return self.th[sh["src"]][min(len(self.th[sh["src"]]) - 1, int(sh["pos"]))]
        return self.th[self.base][self.loops[self.base].current()]

    def _seat(self, name, thumb):
        wk = self.loops[name]
        d = dist(thumb[None], self.th[name])[0]
        if not wk.wraps:
            d[:8] = d[-8:] = 1e9                     # not right at the ends of a clip that does not loop
        wk.seat(int(np.argmin(d)))

    def _loop_frame(self, name, m_open):
        wk = self.loops[name]
        img = wk.frame()
        st = self.mouths.get(name)
        if st is not None:
            body = wk.current()
            img = self._paste_mouth(img, name, st, body, self._pick_mouth(st, body, m_open))
        return img

    def _pick_talk(self, thumb):
        """A talk loop for this line: the closer its best frame is to the screen, the likelier."""
        if len(self.talks) == 1:
            return self.talks[0]
        best = {n: float(dist(thumb[None], self.th[n]).min()) * self.rng.uniform(0.75, 1.3) for n in self.talks}
        return min(best, key=best.get)

    def frame(self, speaking, level, voice=None):
        """voice: (mouth open 0..1, talk wanted) for this video frame from the speech envelope.
        None -> taken from speaking/level (no envelope)."""
        self.t += 1
        t = self.t / self.fps
        if voice is None:
            voice = (min(1.0, level * 2.5), bool(speaking))
        m_open, talk_on = voice
        # --- base loop: talk while the voice sounds, idle otherwise
        talking = self.base in self.talks
        want = (self.base if talking else "talk") if talk_on and self.talks else "idle"
        self.base_since += 1
        if want != self.base and (want == "talk" or self.base_since > 0.4 * self.fps):
            cur = self._visible_thumb()
            if want == "talk":
                want = self.base_prev if self.base_prev in self.talks and self.base_fade < 1 else self._pick_talk(cur)
            self.base_prev = self.base
            self.base_fade = 0.0 if self.base_fade >= 1 else 1.0 - self.base_fade
            self.base_dur = 0.45 if want == "idle" else 0.3
            self.base, self.base_since = want, 0
            self._seat(want, cur)
            self.events.append((self.t, "base " + want))
        for name, wk in self.loops.items():
            if name == self.base or (name == self.base_prev and self.base_fade < 1):
                wk.step()
        out = self._loop_frame(self.base, m_open)
        if self.base_prev is not None and self.base_fade < 1.0:
            self.base_fade = min(1.0, self.base_fade + 1.0 / (self.base_dur * self.fps))
            out = blend(self._loop_frame(self.base_prev, m_open), out, ease(self.base_fade))
            if self.base_fade >= 1.0:
                self.base_prev = None
        # --- one-shot on top
        tilt = sink = 0.0
        sh = self.shot
        if sh:
            fr = self.frames[sh["src"]]
            sh["t"] += 1
            p = sh["pos"]
            if sh["speed"] < 1.0:                       # slow motion: blend neighbours
                i0 = int(p)
                img = blend(fr[i0], fr[min(len(fr) - 1, i0 + 1)], p - i0)
            else:
                img = fr[min(len(fr) - 1, int(p))]
            sh["pos"] = min(float(sh["end"]), p + self.step * sh["speed"])
            if sh["out"] is None:
                sh["k"] = min(1.0, sh["k"] + 1.0 / (sh["xin"] * self.fps))
                left = (sh["end"] - sh["pos"]) / (self.step * sh["speed"])
                cut_short = sh["name"] == "loss" and talk_on and sh["t"] > 0.5 * self.fps   # his line starts
                if left <= sh["xout"] * self.fps or cut_short:
                    sh["out"], sh["k0"] = 0, sh["k"]
                    # the loop under the shot continues from the frame closest to the shot's
                    self._seat(self.base, self.th[sh["src"]][min(len(fr) - 1, int(sh["pos"]))])
                    self.base_prev, self.base_fade = None, 1.0
            if sh["out"] is not None:
                sh["out"] += 1
                sh["k"] = max(0.0, sh["k0"] * (1 - sh["out"] / (sh["xout"] * self.fps)))
            k = ease(sh["k"])
            if sh["name"] == "loss":                   # tilt and sigh swell and settle with the beat
                env = math.sin(math.pi * min(1.0, sh["t"] / max(1.0, sh["total"]))) * k
                tilt, sink = sh["tilt"] * env, sh["sink"] * env
            out = blend(out, img, k)
            if sh["out"] is not None and sh["k"] <= 0:
                self.shot = None
                self.events.append((self.t, "shot end"))
        self.level += (level - self.level) * 0.35
        if self.alive is not None:
            return self.alive.apply(out, t, tilt=tilt, sink=sink)
        return out
