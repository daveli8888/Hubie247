"""Render overlay stills (1280x720 PNG) for both layouts, no ffmpeg stream, no browser.

Uses the same Layout.compose() the stage runs, fed with real Hubie clip frames and, when present, real
Housebets game crops in qa/layout/src/ (dice.jpg, plinko.jpg, dice_before_switch.jpg + game_events.jsonl,
copied from the runner's game.jpg / game_events.jsonl during a zero-bet demo run; not shipped in the repo).
Without them every scene uses the drawn placeholder table, so it runs offline with no browser.

  python3 tools/render_layout.py                 # all scenes, layouts A and B -> qa/layout/
  python3 tools/render_layout.py --layout B --scene plinko
  python3 tools/render_layout.py --layout A --scene fallback --out docs/img
Writes qa/layout/<layout>_<n>_<scene>.png, a 2x2 contact sheet per layout (contact_<layout>.png) and
phone-size previews (phone/<...>.png, 400 px wide) for the readability check.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hubie247 import overlay  # noqa: E402
from games.placeholder_dice import PlaceholderDice  # noqa: E402

SRC = ROOT / "qa" / "layout" / "src"
OUT = ROOT / "qa" / "layout"
CLIPS = ROOT / "assets" / "clips"


def clip_frame(name, t, w, h):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(CLIPS / name), "-frames:v", "1",
                          "-vf", f"scale={w}:{h}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).copy()


def stats_until(n_rounds, games=None):
    st = overlay.SessionStats()
    k = 0
    ev = SRC / "game_events.jsonl"
    lines = ev.read_text().splitlines() if ev.exists() else []
    for line in lines:
        e = json.loads(line)
        if e.get("type") == "round":
            if k >= n_rounds:
                break
            k += 1
        st.feed(e)
    return st.snapshot()


CHAT_DICE = [
    {"user": "mila_rose", "text": "hubie what game is this?"},
    {"user": "Hubie", "hubie": True, "text": "Mila_rose, Housebets Dice. I pick a number, the dice do the rest. "
                                            "Very low effort. My favourite kind."},
    {"user": "nightowl", "text": "love the hat hubie 🎩"},
]

CHAT_PLINKO = [
    {"user": "mila_rose", "text": "hubie what game is this?"},
    {"user": "Hubie", "hubie": True, "text": "Mila_rose, it's Plinko. I drop a ball, gravity does the work. "
                                            "My kind of game."},
    {"user": "nightowl", "text": "love the hat hubie"},
    {"user": "dex_77", "text": "is that real housebets?"},
    {"user": "Hubie", "hubie": True, "text": "Dex_77, the real Housebets table, demo bets of zero. "
                                            "Nothing at stake but my dignity."},
    {"user": "sunny.k", "text": "go for the 30x corner 🙏"},
]

SCENES = {
    "dice": dict(n=1, game="dice.jpg", label="Housebets Dice", mode="live", rounds=7, clip=("hubie_talk.mp4", 3.2),
                 speaking=True, level=0.6,
                 caption={"text": "Seventy-three, over sixty-six. A win, and I didn't even have to sit up.",
                          "reply_to": None},
                 chat=CHAT_DICE),
    "plinko": dict(n=2, game="plinko.jpg", label="Housebets Plinko", mode="live", rounds=19,
                   clip=("hubie_talk.mp4", 6.0), speaking=True, level=0.5,
                   caption={"text": CHAT_PLINKO[4]["text"], "reply_to": "dex_77"}, chat=CHAT_PLINKO),
    "switch": dict(n=3, game="dice_before_switch.jpg", label="Housebets Dice", mode="live", rounds=10,
                   clip=("hubie_idle.mp4", 1.0), speaking=True, level=0.4,
                   caption={"text": "Right, enough dice. Fancy a swing to Plinko? Same zero stakes, more bounce.",
                            "reply_to": None},
                   chat=CHAT_DICE[:2], switch={"to": "Housebets Plinko", "age": 2.6}),
    "fallback": dict(n=4, game=None, label="Dice (placeholder)", mode="placeholder", rounds=19,
                     clip=("hubie_idle.mp4", 5.0), speaking=False, level=0.0,
                     caption={"text": "The Housebets table is having a little lie-down. Relatable. We roll the "
                                      "stand-in dice till it's back.", "reply_to": None},
                     chat=[{"user": "nightowl", "text": "love the hat hubie"},
                           {"user": "dex_77", "text": "where did the game go?"},
                           {"user": "Hubie", "hubie": True, "text": "Dex_77, the Housebets table stepped out "
                            "for a moment. Stand-in dice till it's back, same zero stakes."}]),
}


def placeholder_frame():
    import random
    random.seed(7)
    ph = PlaceholderDice()
    for _ in range(6):
        ph.play_round()
    ph.rolling_until = 0
    ph.note = "the Housebets table is back in a moment"
    return ph.frame()


def render(layout_name, scene, now=None):
    lay = overlay.make(layout_name)
    lay.hubie_still = clip_frame("hubie_idle.mp4", 1.0, 640, 360)
    sc = SCENES[scene]
    now = now or time.time()
    gx, gy, gw, gh = lay.GAME
    src = SRC / sc["game"] if sc["game"] else None
    bgr = cv2.imread(str(src)) if src is not None and src.exists() else placeholder_frame()
    game = overlay.fit_game(bgr, gw, gh)
    cx, cy, cw, ch = lay.CAM
    cam = clip_frame(sc["clip"][0], sc["clip"][1], cw, ch)
    ctx = {"game": game, "game_mode": sc["mode"], "cam": cam, "speaking": sc["speaking"], "level": sc["level"],
           "caption": sc["caption"], "chat": sc["chat"], "uptime": 3 * 3600 + 27 * 60 + 41,
           "game_label": sc["label"], "stats": stats_until(sc["rounds"]), "t": now}
    if sc.get("switch"):
        ctx["switch"] = {"to": sc["switch"]["to"], "since": now - sc["switch"]["age"], "expect": 6.0}
    frame = np.empty((overlay.H, overlay.W, 3), np.uint8)
    lay.stamp_rows(sc["chat"], now - 5)       # rows already settled (no entry animation in a still)
    lay.compose(frame, ctx)
    return frame, sc["n"]


def contact(paths, out, title):
    tiles = [Image.open(p).resize((640, 360), Image.Resampling.LANCZOS) for p in paths]
    sheet = Image.new("RGB", (1280 + 12, 720 + 12 + 40), (6, 14, 18))
    d = ImageDraw.Draw(sheet)
    d.text((10, 22), title, font=overlay.F("Figtree-ExtraBold", 22), fill=(230, 240, 244), anchor="lm")
    for i, t in enumerate(tiles):
        sheet.paste(t, (4 + (i % 2) * 644, 44 + (i // 2) * 364))
    sheet.save(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", default="A,B")
    ap.add_argument("--scene", default=",".join(SCENES))
    ap.add_argument("--out", default=str(OUT), help="output folder (default qa/layout)")
    a = ap.parse_args()
    out = Path(a.out)
    (out / "phone").mkdir(parents=True, exist_ok=True)
    names = {"A": "Layout A - Broadcast", "B": "Layout B - Lounge"}
    for ln in a.layout.split(","):
        paths = []
        for sc in a.scene.split(","):
            frame, n = render(ln, sc)
            p = out / f"{ln}_{n}_{sc}.png"
            Image.fromarray(frame).save(p)
            Image.fromarray(frame).resize((400, 225), Image.Resampling.LANCZOS).save(out / "phone" / p.name)
            paths.append(p)
            print(p)
        if len(paths) == 4:
            contact(paths, out / f"contact_{ln}.png", names.get(ln, ln) + "  (dice, plinko + chat, game switch, fallback)")
            print(out / f"contact_{ln}.png")


if __name__ == "__main__":
    main()
