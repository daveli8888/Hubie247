"""Stage: composes the 1280x720 stream picture + audio in Python (numpy/PIL/cv2) and pipes it
into one ffmpeg encoder. No OBS, no browser: CPU-only and cheap.

Inputs (all files in HUBIE_RUN_DIR, written by the other processes):
  game.jpg   latest game crop (game process)             -> game area, "be right back" card if stale
  game.json  game heartbeat: label, adapter, state       -> "now playing", stand-in / switching state
  game_events.jsonl  rounds + game switches              -> session stats, "next up" card
  state.json caption, chat rows, speech queue, clip events (brain)
Outputs:
  ffmpeg -> recordings/*.ts (hour segments) or HUBIE_RTMP_URL (Kick)
  stage.json heartbeat: frames written, ffmpeg frame count, speech done id (watchdog + brain read it)

Video and audio are written by two threads paced off one clock, so the frame count and the sample
count always match wall time and A/V stays in sync for days.
"""
import os
import signal
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

from . import bus, overlay
from .clips import ENV_HZ, ClipPlayer, speech_envelope
from .config import (AUDIO_RATE, FPS, GAME_EVENTS, GAME_FRAME, GAME_STATUS, H, LOG_DIR, ROOT, RUN, STAGE_STATUS,
                     STATE, W, env, env_float)

# ---- look ------------------------------------------------------------------------------------
# All drawing lives in overlay.py; HUBIE_LAYOUT picks the layout (A = Broadcast, B = Lounge).


# ---- audio -----------------------------------------------------------------------------------
class Audio:
    CH = 2
    CHUNK = AUDIO_RATE // 50  # 20 ms

    def __init__(self):
        self.lock = threading.Lock()
        self.queue = []        # [(speech dict, pcm int16 array (n,2) or None, lip-sync envelope or None)]
        self.cur = None
        self.cur_pos = 0
        self.cur_end_t = 0.0
        self.cur_t0 = 0.0
        self.cur_env = None    # (mouth open, talk wanted) at 100 Hz for the line playing now
        self.level = 0.0
        self.speaking = False
        self.started = []      # speech ids that started (for chat row reveal + caption)
        self.done_id = None
        self.done_at = {}      # speech id -> wall time it finished (caption is cleared a bit later)
        self.timeline = []     # (event, speech id, wall time) for logs/stage_timeline.jsonl
        self.music = None
        self.music_pos = 0
        m = env("HUBIE_MUSIC")
        if m and os.path.exists(m):
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", m, "-f", "s16le", "-ac", "2", "-ar",
                                  str(AUDIO_RATE), "-"], capture_output=True).stdout
            self.music = np.frombuffer(raw, np.int16).reshape(-1, 2).astype(np.float32)
        self.music_gain = 10 ** (env_float("HUBIE_MUSIC_DB", -26) / 20)

    def progress(self):
        """(speech id, 0..1) of the line playing now, or (None, 1)."""
        with self.lock:
            if self.cur is None:
                return None, 1.0
            if self.cur_pcm is not None and len(self.cur_pcm):
                return self.cur["id"], min(1.0, self.cur_pos / len(self.cur_pcm))
            dur = max(0.1, self.cur_end_t - self.cur_t0)
            return self.cur["id"], min(1.0, 1 - (self.cur_end_t - time.monotonic()) / dur)

    def voice(self, t, lead=0.04):
        """(mouth open 0..1, talk wanted) at nominal time t, read from the playing line's envelope (the mouth
        leads the sound by ~1 frame, as in drawn lip-sync). Nothing playing -> (0, False)."""
        with self.lock:
            cur, env, t0 = self.cur, self.cur_env, self.cur_t0
        if cur is None:
            return 0.0, False
        if env is None:                                  # no audio file (text-only line): plain talking
            return 0.45 + 0.35 * abs(np.sin(t * 9.0)), True
        k = int((t - t0 + lead) * ENV_HZ)
        o, w = env
        if k < 0:
            return 0.0, bool(len(w) and w[0])
        if k >= len(o):
            return 0.0, False
        return float(o[k]), bool(w[k])

    def enqueue(self, speech):
        pcm = None
        p = speech.get("pcm")
        env = None
        if p and os.path.exists(p):
            pcm = np.fromfile(p, np.int16).reshape(-1, 2)
            env = speech_envelope(pcm, AUDIO_RATE)      # lip-sync: computed once per line, off the video thread
        with self.lock:
            self.queue.append((speech, pcm, env))

    def chunk(self, now):
        n = self.CHUNK
        out = np.zeros((n, 2), np.float32)
        with self.lock:
            if self.cur is None and self.queue:
                self.cur, pcm, self.cur_env = self.queue.pop(0)
                self.cur_pcm = pcm
                self.cur_pos = 0
                dur = (len(pcm) / AUDIO_RATE) if pcm is not None else max(2.5, len(self.cur["text"].split()) * 0.33)
                self.cur_end_t = now + dur
                self.cur_t0 = now
                self.started.append(self.cur["id"])
                self.timeline.append(("speech_start", self.cur, now))
                self.speaking = True
            if self.cur is not None:
                if self.cur_pcm is not None:
                    seg = self.cur_pcm[self.cur_pos:self.cur_pos + n]
                    self.cur_pos += n
                    out[: len(seg)] += seg
                    finished = self.cur_pos >= len(self.cur_pcm)
                else:
                    finished = now >= self.cur_end_t
                if finished:
                    self.done_id = self.cur["id"]
                    self.done_at[self.cur["id"]] = time.time()
                    if len(self.done_at) > 50:
                        self.done_at.pop(next(iter(self.done_at)))
                    self.timeline.append(("speech_end", self.cur, now))
                    self.cur = None
                    self.speaking = False
        rms = float(np.sqrt(np.mean(out ** 2))) / 6000.0
        self.level = min(1.0, rms)
        if self.music is not None:
            m = self.music
            idx = (np.arange(n) + self.music_pos) % len(m)
            self.music_pos = (self.music_pos + n) % len(m)
            out += m[idx] * self.music_gain * (0.35 if self.speaking else 1.0)
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


# ---- main ------------------------------------------------------------------------------------
class Stage:
    def __init__(self):
        self.stop = threading.Event()
        self.ov = overlay.make(env("HUBIE_LAYOUT", "A"))
        print("layout", self.ov.name, flush=True)
        self.clips = ClipPlayer(self.ov.CAM[2], self.ov.CAM[3], FPS)
        self.ov.hubie_still = self.clips.frames["idle"][0]
        self.audio = Audio()
        self.game_img = None
        self.game_mtime = 0
        self.game_label = None
        self.game_mode = "stale"
        self.stats = overlay.SessionStats()
        self.events = bus.Tail(GAME_EVENTS, from_start=True)
        self.switch = None
        self.state = {}
        self.seen_speech = set()
        self.seen_clip_ev = None
        self.frames = 0
        self.ff_frame = 0
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        # "on air" counts from the show's start, not this process's: survives stage restarts
        try:
            self.show_start = float((RUN / "show_start").read_text())
        except (OSError, ValueError):
            self.show_start = self.wall0
            (RUN / "show_start").write_text(str(self.wall0))
        self.fifo = str(RUN / "audio.fifo")
        self.caption_hold = env_float("HUBIE_CAPTION_HOLD", 4)
        self.round_start_t = 0.0

    def show_start_now(self, now):
        """The on-air clock's start, re-read every 10 s so it can be set (e.g. to Kick's session start)
        without restarting the stage."""
        if now - getattr(self, "_ss_read", 0) > 10:
            self._ss_read = now
            try:
                self.show_start = float((RUN / "show_start").read_text())
            except (OSError, ValueError):
                pass
        return self.show_start

    # -- ffmpeg --
    def ffmpeg_cmd(self):
        rtmp = env("HUBIE_RTMP_URL")
        vb = env("HUBIE_VIDEO_BITRATE", "2500k")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
               "-progress", "pipe:1", "-nostats",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
               "-thread_queue_size", "64", "-i", "pipe:0",
               "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "2", "-thread_queue_size", "256", "-i", self.fifo,
               "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", env("HUBIE_X264_PRESET", "veryfast"), "-tune", "zerolatency",
               "-pix_fmt", "yuv420p", "-b:v", vb, "-maxrate", vb, "-bufsize", "5000k",
               "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
               "-threads", env("HUBIE_X264_THREADS", "2"),
               "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE)]
        if rtmp and env("HUBIE_RECORD_LIVE", "0") == "1":
            # live AND a local copy of the exact same encode (no second encoder): the contest's unedited VOD,
            # safe even if Kick splits its own VOD. A failing file never stops the stream (onfail=ignore).
            out = ROOT / "recordings"
            out.mkdir(exist_ok=True)
            seg = (f"[f=segment:onfail=ignore:bsfs/v=dump_extra=freq=keyframe:segment_time="
                   f"{env('HUBIE_SEGMENT_SECS', '3600')}:segment_format=mpegts:reset_timestamps=1:strftime=1]"
                   f"{out / 'live_%Y%m%d_%H%M%S.ts'}")
            cmd += ["-flags", "+global_header", "-f", "tee", f"[f=flv]{rtmp}|{seg}"]
        elif rtmp:
            cmd += ["-f", "flv", rtmp]
        else:
            out = ROOT / "recordings"
            out.mkdir(exist_ok=True)
            cmd += ["-f", "segment", "-segment_time", env("HUBIE_SEGMENT_SECS", "3600"), "-segment_format", "mpegts",
                    "-reset_timestamps", "1", "-strftime", "1", str(out / "hubie_%Y%m%d_%H%M%S.ts")]
        return cmd

    def run(self):
        if os.path.exists(self.fifo):
            os.unlink(self.fifo)
        os.mkfifo(self.fifo)
        cmd = self.ffmpeg_cmd()
        print("ffmpeg:", " ".join(c if "rtmp" not in c else "<rtmp url hidden>" for c in cmd), flush=True)
        errlog = open(LOG_DIR / "stage_ffmpeg.log", "ab")
        self.ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog)
        self.t0 = time.monotonic()
        self.t0_wall = time.time()        # wall time of video frame 0 / audio sample 0 of this recording
        self.timeline({"ev": "stage_start", "t0_wall": self.t0_wall, "fps": FPS,
                       "output": "rtmp" if env("HUBIE_RTMP_URL") else "file"})
        threads = [threading.Thread(target=f, daemon=True) for f in (self.audio_loop, self.progress_loop,
                                                                       self.control_loop)]
        for t in threads:
            t.start()
        try:
            self.video_loop()
        except BrokenPipeError:
            print("ffmpeg pipe closed", flush=True)
        finally:
            self.stop.set()
            try:
                self.ff.stdin.close()
            except Exception:
                pass
            try:
                self.ff.wait(timeout=10)
            except Exception:
                self.ff.kill()
        rc = self.ff.returncode
        print("ffmpeg exited", rc, flush=True)
        # Non-zero exit makes pm2 restart us (a clean SIGTERM stop exits 0 via the handler).
        sys.exit(0 if self.stop_requested else 1)

    stop_requested = False

    def timeline(self, rec):
        bus.append_jsonl(LOG_DIR / "stage_timeline.jsonl", rec)

    def audio_loop(self):
        with open(self.fifo, "wb", buffering=0) as f:
            k = 0
            t_audio0 = self.t0
            while not self.stop.is_set():
                target = t_audio0 + k * 0.02
                d = target - time.monotonic()
                if d > 0:
                    time.sleep(d)
                try:
                    f.write(self.audio.chunk(target))     # nominal sample time (= video time) of this chunk
                except (BrokenPipeError, OSError):
                    self.stop.set()
                    return
                k += 1

    def progress_loop(self):
        for line in iter(self.ff.stdout.readline, b""):
            if line.startswith(b"frame="):
                try:
                    self.ff_frame = int(line.split(b"=")[1])
                except ValueError:
                    pass

    def control_loop(self):
        """10 Hz: read brain state + game frame, write heartbeat."""
        while not self.stop.is_set():
            with self.audio.lock:
                tl, self.audio.timeline = self.audio.timeline, []
            for ev, sp, mono in tl:      # mono = nominal monotonic time of the audio chunk
                vt = mono - self.t0
                self.timeline({"ev": ev, "id": sp["id"], "kind": sp.get("kind"), "text": sp.get("text", "")[:80],
                               "reply_to": sp.get("reply_to"), "ts": self.t0_wall + vt, "video_t": round(vt, 3)})
            st = bus.read_json(STATE, {}) or {}
            self.state = st
            for sp in st.get("speech", []):
                if sp["id"] not in self.seen_speech:
                    self.seen_speech.add(sp["id"])
                    if sp.get("ts", 0) > self.wall0 - 3:   # don't replay old lines after a restart
                        self.audio.enqueue(sp)
            ev = st.get("clip_event")
            if ev and ev.get("id") != self.seen_clip_ev:
                if self.seen_clip_ev is not None or time.time() - ev.get("ts", 0) < 5:
                    self.clips.trigger(ev["clip"])
                self.seen_clip_ev = ev.get("id")
            # game frame (fitted to the layout's game frame once per new picture, not per video frame)
            try:
                mt = os.path.getmtime(GAME_FRAME)
            except OSError:
                mt = 0
            if mt and mt != self.game_mtime:
                img = cv2.imread(str(GAME_FRAME), cv2.IMREAD_COLOR)
                if img is not None:
                    gx, gy, gw, gh = self.ov.GAME
                    self.game_img = overlay.fit_game(img, gw, gh)
                    self.game_mtime = mt
            stale = time.time() - (self.game_mtime or 0) > env_float("HUBIE_GAME_STALE_SECS", 8)
            # "now playing" comes straight from the game process, so it changes the moment the game switches
            # (the brain's copy can lag while it is busy writing/voicing a line)
            gs = bus.read_json(GAME_STATUS, {}) or {}
            fresh = time.time() - gs.get("ts", 0) < 10
            if fresh and gs.get("label"):
                self.game_label = gs["label"]
            self.game_mode = ("stale" if stale or self.game_img is None else
                              "placeholder" if fresh and gs.get("adapter") == "placeholder" else "live")
            self.track_events(gs if fresh else {})
            bus.write_json(STAGE_STATUS, {
                "ts": time.time(), "pid": os.getpid(), "frames": self.frames, "ffmpeg_frame": self.ff_frame,
                "speaking": self.audio.speaking, "speech_done": self.audio.done_id,
                "speech_started": self.audio.started[-20:], "clips": self.clips.available(),
                "output": "rtmp" if env("HUBIE_RTMP_URL") else "file"})
            time.sleep(0.1)

    def track_events(self, gs):
        """Session stats + the 'next up' card: shown from the switch event until the new game is live
        (at least 4.5 s so it reads, at most 25 s)."""
        now = time.time()
        for e in self.events.read():
            self.stats.feed(e)
            if e.get("type") == "round_start":
                self.round_start_t = now        # a new round: an older, finished caption goes away
            if e.get("type") == "game_state" and e.get("state") == "switching" and e.get("to"):
                if now - e.get("ts", now) < 20:
                    self.switch = {"to": e["to"], "since": now, "expect": 6.0, "until": None}
                    self.timeline({"ev": "switch_card", "to": e["to"], "ts": now,
                                   "video_t": round(now - getattr(self, "t0_wall", now), 3)})
        sw = self.switch
        if sw:
            if sw["until"] is None:
                arrived = gs.get("label") == sw["to"] and gs.get("state") == "live"
                if (arrived and now - sw["since"] > 4.5) or now - sw["since"] > 25:
                    sw["until"] = now + 0.4
            elif now > sw["until"]:
                self.switch = None

    def video_loop(self):
        frame = np.empty((H, W, 3), np.uint8)
        while not self.stop.is_set():
            target = self.t0 + self.frames / FPS
            d = target - time.monotonic()
            if d > 0:
                time.sleep(d)
            st = self.state
            started = set(self.audio.started)
            cap = st.get("caption") or {}
            shown_cap = cap if (not cap.get("speech_id") or cap.get("speech_id") in started) else st.get("prev_caption") or {}
            ended = self.audio.done_at.get(shown_cap.get("speech_id"))
            if ended and self.audio.cur is None and (time.time() - ended > self.caption_hold
                                                     or self.round_start_t > ended):
                shown_cap = {}
            reveal = None
            sid, prog = self.audio.progress()
            if sid is not None and shown_cap.get("speech_id") == sid and shown_cap.get("text"):
                n = len(overlay.clean(shown_cap["text"]).split())
                reveal = min(n, int(n * min(1.0, prog * 1.15 + 0.08)) + 1)
            rows = [r for r in st.get("chat", []) if not r.get("speech_id") or r["speech_id"] in started]
            now = time.time()
            voice = self.audio.voice(target)            # this frame's nominal time = the audio chunk clock
            ctx = {"game": self.game_img, "game_mode": self.game_mode,
                   "cam": self.clips.frame(self.audio.speaking, self.audio.level, voice),
                   "speaking": self.audio.speaking, "level": self.audio.level,
                   "caption": {"text": shown_cap.get("text", ""), "reply_to": shown_cap.get("reply_to"),
                               "reveal": reveal},
                   "chat": rows[-14:], "uptime": now - self.show_start_now(now),
                   "game_label": self.game_label or (st.get("now_playing") or "").replace("now playing: ", ""),
                   "stats": self.stats.snapshot(), "switch": self.switch, "t": now}
            self.ov.compose(frame, ctx)
            self.ff.stdin.write(frame.data)
            self.frames += 1


def main():
    st = Stage()

    def term(*_):
        st.stop_requested = True
        st.stop.set()
        # The video thread may be stuck writing to a hung encoder: wake ffmpeg (SIGCONT in case it was
        # stopped), ask it to finish, and SIGKILL it after 5 s so no frozen encoder is left behind.
        ff = getattr(st, "ff", None)
        if ff and ff.poll() is None:
            for sig in (signal.SIGCONT, signal.SIGTERM):
                try:
                    os.kill(ff.pid, sig)
                except OSError:
                    pass
            threading.Timer(5, lambda: ff.poll() is None and ff.kill()).start()

    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGINT, term)
    st.run()


if __name__ == "__main__":
    main()
