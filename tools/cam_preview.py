"""Render Hubie's cam alone from a scripted timeline (real edge-tts voice, win/loss beats), for QA.

    python3 tools/cam_preview.py out.mp4 [--secs 20] [--compare-git d6472a1] [--frames qa/cam_frames]

--compare-git REV  puts the cam as it was at REV (its clips.py + clip files, taken from git) on the left and the
                   current one on the right, same voice and events: a before/after.
--frames DIR       also writes PNG stills around every state change and inside speech pauses.
The timeline is fixed (see SCRIPT) so runs are comparable; the voice is synthesised once and cached in
/dev/shm/hubie_cam_preview/.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hubie247 import tts  # noqa: E402
from hubie247.clips import ENV_HZ, ClipPlayer, speech_envelope  # noqa: E402
from hubie247.config import AUDIO_RATE  # noqa: E402

FPS = 30
CAM = (476, 268)          # layout A cam size
SCRIPT = [                 # (time s, event, text)
    (1.5, "say", "Hey mila_rose, welcome in! This is Housebets Dice. I pick a target, call over or under, "
                 "and the roll decides. Zero-amount bets, so it's all for fun."),
    (15.0, "win", None),
    (15.8, "say", "Oh, look at that! A win. Nicely done, dice."),
    (24.0, "loss", None),
    (26.4, "say", "Missed that one. No harm done, it's a zero bet."),
    (33.5, "say", "Chat's quiet. Tell me, nightowl... do you like the hat?"),
]
SHORT = [
    (1.0, "say", "Hey mila_rose, welcome in! This is Housebets Dice. Zero-amount bets, so it's all for fun."),
    (7.0, "win", None),
    (7.8, "say", "Oh, look at that! A win. Nicely done, dice."),
    (14.2, "loss", None),
    (15.8, "say", "Missed that one. No harm done, it's a zero bet."),
]
CACHE = Path("/dev/shm/hubie_cam_preview")


def voice_lines(script):
    CACHE.mkdir(exist_ok=True)
    out = {}
    for t, ev, text in script:
        if ev != "say":
            continue
        p = CACHE / (hashlib.md5(text.encode()).hexdigest()[:12] + ".pcm")
        if not p.exists():
            if not tts.synth(text, str(p)):
                raise SystemExit("edge-tts failed; try again")
        out[t] = np.fromfile(p, np.int16).reshape(-1, 2)
    return out


def old_player(rev, w, h):
    """The ClipPlayer of git revision `rev`, with its own clip files, in a temp dir."""
    d = Path(tempfile.mkdtemp(prefix="hubie_old_"))
    src = subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:hubie247/clips.py"], capture_output=True,
                         text=True, check=True).stdout
    cfg = json.loads(subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:assets/clips/clips.json"],
                                    capture_output=True, text=True, check=True).stdout)
    for name, c in cfg.items():
        f = c.get("file")
        if not f:
            continue
        r = subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:{f}"], capture_output=True)
        if r.returncode == 0:
            (d / Path(f).name).write_bytes(r.stdout)
            c["file"] = str(d / Path(f).name)
    (d / "clips.json").write_text(json.dumps(cfg))
    (d / "clips_old.py").write_text(src.replace("from .config import ROOT", "from hubie247.config import ROOT"))
    spec = importlib.util.spec_from_file_location("clips_old", d / "clips_old.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ClipPlayer(w, h, FPS, config_path=d / "clips.json")


def label(img, text):
    cv2.putText(img, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--secs", type=float, default=40)
    ap.add_argument("--short", action="store_true", help="the 20 s timeline (SHORT)")
    ap.add_argument("--compare-git")
    ap.add_argument("--frames")
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()
    w, h = CAM
    script = SHORT if a.short else SCRIPT
    lines = voice_lines(script)
    n_frames = int(a.secs * FPS)
    n_samp = int(a.secs * AUDIO_RATE)
    audio = np.zeros((n_samp, 2), np.int32)
    spans = []                                     # (t0, t1, open, talk)
    for t0, pcm in sorted(lines.items()):
        s0 = int(t0 * AUDIO_RATE)
        seg = pcm[: max(0, n_samp - s0)]
        audio[s0:s0 + len(seg)] += seg
        o, tk = speech_envelope(pcm, AUDIO_RATE)
        spans.append((t0, t0 + len(pcm) / AUDIO_RATE, o, tk))
    new = ClipPlayer(w, h, FPS, seed=a.seed)
    old = old_player(a.compare_git, w, h) if a.compare_git else None
    events = sorted((t, ev) for t, ev, _ in script if ev != "say")
    fdir = Path(a.frames) if a.frames else None
    if fdir:
        fdir.mkdir(parents=True, exist_ok=True)
    W = w * 2 + 8 if old else w
    np.clip(audio, -32768, 32767).astype(np.int16).tofile(CACHE / "mix.pcm")
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{h}",
                            "-r", str(FPS), "-i", "-", "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "2",
                            "-i", str(CACHE / "mix.pcm"), "-vf", "scale=iw*2:ih*2:flags=lanczos",
                            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", a.out],
                           stdin=subprocess.PIPE)
    marks = set()
    for i in range(n_frames):
        t = i / FPS
        while events and events[0][0] <= t:
            ev = events.pop(0)[1]
            new.trigger(ev)
            if old:
                old.trigger(ev)
        voice, speaking, level = (0.0, False), False, 0.0
        for t0, t1, o, tk in spans:
            if t0 <= t < t1:
                k = int((t - t0 + 0.04) * ENV_HZ)
                voice = (float(o[k]), bool(tk[k])) if 0 <= k < len(o) else (0.0, False)
                speaking, level = True, float(o[min(len(o) - 1, max(0, k))]) * 0.5
        cam = new.frame(speaking, level, voice)
        if old:
            before = old.frame(speaking, level)
            frame = np.zeros((h, W, 3), np.uint8)
            frame[:, :w] = before
            frame[:, w + 8:] = cam
            label(frame[:, :w], "before")
            label(frame[:, w + 8:], "after")
        else:
            frame = np.ascontiguousarray(cam)
        enc.stdin.write(frame.tobytes())
        if fdir:
            # stills: every 2nd frame for 0.6 s after each state change, and in silent stretches inside speech
            for fi, ev in new.events:
                if 0 <= i - fi <= 18 and (i - fi) % 3 == 0:
                    marks.add(i)
            if speaking and voice[0] < 0.02 and i % 4 == 0:      # pauses inside a line: mouth must be shut
                marks.add(i)
            if i in marks:
                cv2.imwrite(str(fdir / f"f{i:05d}_{t:06.2f}.png"), cv2.cvtColor(cam, cv2.COLOR_RGB2BGR))
    enc.stdin.close()
    enc.wait()
    (Path(a.out).with_suffix(".events.json")).write_text(json.dumps(new.events))
    print(a.out, "events:", new.events)


if __name__ == "__main__":
    main()
