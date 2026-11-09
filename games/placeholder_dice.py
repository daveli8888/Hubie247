"""Fallback scene while the Housebets table is unavailable (Cloudflare check, logged out, page down).

Drawn in Python (no browser, no extra Chrome): a clearly labelled stand-in dice so Hubie keeps a game to talk
about. Rolls are local random numbers, flagged adapter="placeholder" in game_events.jsonl.
"""
import random
import time

import numpy as np
from PIL import ImageDraw

W, H = 900, 612          # same shape as the Housebets game panel, so the stage frame fits it without bars

from hubie247 import overlay as ov  # noqa: E402  (fonts, palette and drawing helpers shared with the stage)


class PlaceholderDice:
    label = "Dice (placeholder)"

    def __init__(self):
        self.target, self.roll, self.win, self.rolling_until = 50.5, None, None, 0
        self.hist = []
        self.rounds = 0
        self.note = "the Housebets table is reconnecting"

    def play_round(self):
        self.target = random.choice([50.5, 50.5, 66, 75, 25, 90])
        self.roll = round(random.random() * 100, 2)
        self.win = self.roll > self.target
        self.rolling_until = time.time() + 1.0
        self.hist = ([(self.roll, self.win)] + self.hist)[:8]
        self.rounds += 1
        mult = round(99 / (100 - self.target), 2)
        return {"game": "dice", "roll": self.roll, "target": self.target, "condition": "over",
                "multiplier": mult, "win": self.win, "round": self.rounds, "placeholder": True}

    def _static(self):
        """Everything that does not change between frames, drawn once."""
        img = ov.vgrad(W, H, (12, 30, 39), (6, 17, 23))
        d = ImageDraw.Draw(img)
        # stand-in ribbon
        ov.shape_fill(img, (18, 16, W - 18, 74), 14, ov.vgrad(W - 36, 58, (64, 48, 18), (40, 30, 12)))
        ov.ring_grad(img, (18, 16, W - 18, 74), 14, ov.GOLD_LIGHT, ov.GOLD_DEEP, 1.4)
        img.alpha_composite(ov.tophat(34, ov.GOLD), (36, 28))
        ov.tracked(d, (82, 36), "STAND-IN TABLE", ov.F("Figtree-Black", 18), ov.GOLD_LIGHT, 3.0, "lm")
        d.text((82, 59), "Housebets is reconnecting  •  drawn dice, nothing at stake",
               font=ov.F("Figtree-Bold", 18), fill=(236, 218, 176), anchor="lm")
        # side panel
        ov.shape_fill(img, (18, 90, 268, H - 18), 16, (*ov.PANEL, 255))
        ov.shape_ring(img, (18, 90, 268, H - 18), 16, (*ov.LINE, 255), 1.0)
        # board
        ov.shape_fill(img, (284, 90, W - 18, H - 18), 16, ov.vgrad(W - 302, H - 108, (16, 36, 47), (10, 25, 33)))
        ov.shape_ring(img, (284, 90, W - 18, H - 18), 16, (*ov.LINE, 255), 1.0)
        return img

    def frame(self):
        if getattr(self, "_base", None) is None:
            self._base = self._static()
        img = self._base.copy()
        d = ImageDraw.Draw(img)
        F = ov.F
        mult = 99 / (100 - self.target)
        y = 118
        for lab, val, col in (("MODE", "Demo", ov.CYAN_SOFT), ("BET", "0.00", ov.WHITE),
                              ("ROLL OVER", f"{self.target:.2f}", ov.WHITE), ("PAYOUT", f"{mult:.2f}×", ov.GOLD)):
            ov.tracked(d, (40, y), lab, F("Figtree-Black", 12), ov.DIM, 1.6, "lm")
            d.text((40, y + 28), val, font=F("Oxanium-Bold", 28), fill=col, anchor="lm")
            y += 74
        ov.tracked(d, (40, y), "LAST ROLLS", F("Figtree-Black", 12), ov.DIM, 1.6, "lm")
        x, yy = 40, y + 16
        for r, w in self.hist[:6]:
            t = f"{r:.2f}"
            f = F("Oxanium-Bold", 16)
            tw = d.textlength(t, font=f) + 16
            if x + tw > 250:
                x, yy = 40, yy + 32
            c = ov.WIN if w else ov.LOSS
            ov.shape_fill(img, (x, yy, x + tw, yy + 26), 8, (*ov.mix(ov.PANEL, c, 0.18), 255))
            d.text((x + tw / 2, yy + 14), t, font=f, fill=c, anchor="mm")
            x += tw + 6
        cx = (284 + W - 18) // 2
        now = time.time()
        if self.roll is None:
            num, col, msg = "--.--", ov.TXT, "Hubie is warming up..."
        elif now < self.rolling_until:
            num, col, msg = f"{random.random() * 100:.2f}", ov.TXT, "rolling..."
        else:
            num = f"{self.roll:.2f}"
            col = ov.WIN if self.win else ov.LOSS
            msg = "Win (demo)" if self.win else "No luck this time"
        d.text((cx, 250), num, font=F("Oxanium-ExtraBold", 132), fill=col, anchor="mm")
        bx0, bx1, by = 330, W - 64, 388
        split = bx0 + (bx1 - bx0) * self.target / 100
        ov.shape_fill(img, (bx0, by, split + 8, by + 16), 8, ov.vgrad(int(split + 8 - bx0), 16, (250, 92, 112), (200, 40, 66)))
        ov.shape_fill(img, (split - 8, by, bx1, by + 16), 8, ov.vgrad(int(bx1 - split + 8), 16, (70, 240, 190), (30, 180, 140)))
        for v in (0, 25, 50, 75, 100):
            d.text((bx0 + (bx1 - bx0) * v / 100, by + 38), str(v), font=F("Figtree-SemiBold", 14), fill=ov.DIM2,
                   anchor="mm")
        if self.roll is not None and now >= self.rolling_until:
            mx = bx0 + (bx1 - bx0) * self.roll / 100
            ov.shape_fill(img, (mx - 5, by - 14, mx + 5, by + 30), 5, (*ov.WHITE, 255))
        d.text((cx, 480), msg, font=F("Figtree-ExtraBold", 28), fill=ov.WHITE, anchor="mm")
        d.text((cx, 516), f"roll over {self.target:.2f} to win", font=F("Figtree-SemiBold", 17), fill=ov.DIM,
               anchor="mm")
        return np.array(img.convert("RGB"))[:, :, ::-1].copy()   # BGR for cv2.imwrite
