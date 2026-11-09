"""Stream overlay: the look of the 1280x720 picture, in two switchable layouts.

HUBIE_LAYOUT=A  "Broadcast"  big game view, info strip on top, Hubie cam + chat in the right rail,
                             Hubie's line as a broadcast lower-third under the game.
HUBIE_LAYOUT=B  "Lounge"     Hubie first: big host cam top-left in a gold art-deco frame, his line in a
                             speech bubble under him, session card below; game top-right, chat under it.

Everything is drawn with Pillow + numpy (no browser, no GPU). The static parts (background, bezels, glows,
labels) are rendered once into `base`. Dynamic layers are rendered on a crop of `base`, so they are plain
opaque RGB blocks that are copied into the frame, and are only re-rendered when what they show changes
(or while a short animation runs). The per-frame cost is a handful of array copies.

Colours and fonts follow housebets.com itself (sampled from the live tab 2026-11-09): near-black teal
background, cyan "H" accent #02f7fd, pale-teal buttons, Figtree / Red Hat Display / Oxanium. Hubie's own
accent is gold (top hat badge, tux buttons) with a serif name plate.
"""
import math
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 720
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# ---- palette (housebets.com + Hubie gold) -------------------------------------------------------
BG0 = (3, 12, 16)
BG1 = (7, 22, 29)
PANEL = (10, 27, 35)
PANEL_HI = (15, 37, 48)
PANEL_HI2 = (20, 46, 59)
LINE = (25, 64, 80)
CYAN = (2, 247, 253)
CYAN_MID = (24, 190, 200)
CYAN_SOFT = (164, 229, 229)
TEAL_DEEP = (14, 98, 108)
GOLD = (246, 199, 86)
GOLD_LIGHT = (255, 226, 150)
GOLD_DEEP = (168, 118, 38)
WHITE = (238, 246, 248)
TXT = (214, 230, 236)
DIM = (128, 158, 172)
DIM2 = (88, 116, 130)
RED = (238, 56, 80)
WIN = (52, 232, 170)
LOSS = (240, 92, 108)
KICK = (83, 252, 24)

NAME_COLORS = [(255, 150, 112), (126, 206, 255), (166, 230, 120), (214, 160, 255),
               (255, 214, 92), (90, 232, 236), (255, 120, 170), (170, 150, 255), (255, 180, 90)]


def name_color(name):
    return NAME_COLORS[sum(name.encode()) % len(NAME_COLORS)]


_FONTS = {}
FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def F(name, size):
    """Font from assets/fonts (e.g. 'Figtree-Bold'), cached; DejaVu if the file is missing."""
    k = (name, size)
    if k not in _FONTS:
        try:
            _FONTS[k] = ImageFont.truetype(str(FONT_DIR / f"{name}.ttf"), size)
        except OSError:
            _FONTS[k] = ImageFont.truetype(FALLBACK, size)
    return _FONTS[k]


# ---- drawing helpers ---------------------------------------------------------------------------
SS = 3   # supersampling for anti-aliased shapes


def rgba(c, a=255):
    return (c[0], c[1], c[2], a)


def mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def vgrad(w, h, top, bot, alpha=255):
    t = np.linspace(0, 1, max(1, h))[:, None, None]
    arr = np.array(top, float)[None, None] * (1 - t) + np.array(bot, float)[None, None] * t
    arr = np.repeat(arr, w, axis=1)
    a = np.full((h, w, 1), alpha, float)
    return Image.fromarray(np.concatenate([arr, a], 2).astype(np.uint8), "RGBA")


def hgrad(w, h, left, right, alpha=255):
    return vgrad(h, w, left, right, alpha).transpose(Image.Transpose.ROTATE_270).transpose(
        Image.Transpose.FLIP_LEFT_RIGHT)


from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=512)
def rr_mask(w, h, r):
    """Anti-aliased rounded-rect mask (L)."""
    m = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), radius=r * SS, fill=255)
    return m.resize((w, h), Image.Resampling.LANCZOS)


@lru_cache(maxsize=256)
def rr_ring(w, h, r, width):
    m = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), radius=r * SS, outline=255,
                                        width=max(1, int(width * SS)))
    return m.resize((w, h), Image.Resampling.LANCZOS)


def shape_fill(img, box, r, fill):
    """Paste an anti-aliased rounded rect: fill is a colour or an RGBA image of the box size."""
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    m = rr_mask(w, h, r)
    if isinstance(fill, Image.Image):
        src = fill.convert("RGBA")
        if src.size != (w, h):
            src = src.resize((w, h), Image.Resampling.BILINEAR)
        a = np.array(src.split()[3], float) * (np.array(m, float) / 255.0)
        src.putalpha(Image.fromarray(a.astype(np.uint8)))
    else:
        c = fill if len(fill) == 4 else rgba(fill)
        src = Image.new("RGBA", (w, h), c)
        if c[3] < 255:
            m = Image.fromarray((np.array(m, float) * c[3] / 255).astype(np.uint8))
        src.putalpha(m)
    img.alpha_composite(src, (x0, y0))


def shape_ring(img, box, r, color, width=1.0):
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    m = rr_ring(w, h, r, width)
    c = color if len(color) == 4 else rgba(color)
    if c[3] < 255:
        m = Image.fromarray((np.array(m, float) * c[3] / 255).astype(np.uint8))
    src = Image.new("RGBA", (w, h), c)
    src.putalpha(m)
    img.alpha_composite(src, (x0, y0))


def ring_grad(img, box, r, c_top, c_bot, width=1.5):
    """Rounded outline with a vertical colour gradient (metallic rim)."""
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    src = vgrad(w, h, c_top, c_bot)
    src.putalpha(rr_ring(w, h, r, width))
    img.alpha_composite(src, (x0, y0))


def glow(img, box, r, color, blur=14, strength=0.8, width=6):
    """Soft light around a rounded rect."""
    x0, y0, x1, y1 = [int(v) for v in box]
    pad = blur * 3
    w, h = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
    layer = Image.new("L", (w, h), 0)
    ImageDraw.Draw(layer).rounded_rectangle((pad, pad, w - pad, h - pad), radius=r, outline=255, width=width)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    a = (np.array(layer, float) * strength).clip(0, 255).astype(np.uint8)
    src = Image.new("RGBA", (w, h), rgba(color))
    src.putalpha(Image.fromarray(a))
    img.alpha_composite(src, (x0 - pad, y0 - pad))


def shadow(img, box, r, blur=16, alpha=170, dy=6):
    x0, y0, x1, y1 = [int(v) for v in box]
    pad = blur * 3
    w, h = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
    layer = Image.new("L", (w, h), 0)
    ImageDraw.Draw(layer).rounded_rectangle((pad, pad, w - pad, h - pad), radius=r, fill=alpha)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    src = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    src.putalpha(layer)
    img.alpha_composite(src, (x0 - pad, y0 - pad + dy))


def radial(img, cx, cy, rx, ry, color, alpha):
    """Big soft radial light (background ambience)."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
    a = (np.exp(-d * 2.2) * alpha).clip(0, 255).astype(np.uint8)
    src = Image.new("RGBA", (W, H), rgba(color))
    src.putalpha(Image.fromarray(a))
    img.alpha_composite(src)


def tracked(d, xy, text, font, fill, tracking=0.0, anchor="ls"):
    """Text with letter spacing (tracking in px). anchor: l/m/r + s/m (baseline/middle)."""
    widths = [d.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * max(0, len(text) - 1)
    x, y = xy
    if anchor[0] == "m":
        x -= total / 2
    elif anchor[0] == "r":
        x -= total
    va = anchor[1]
    for ch, cw in zip(text, widths):
        d.text((x, y), ch, font=font, fill=fill, anchor="l" + va)
        x += cw + tracking
    return total


def tracked_len(d, text, font, tracking=0.0):
    return sum(d.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)


def wrap(d, text, fnt, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=fnt) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
            while d.textlength(cur, font=fnt) > width and len(cur) > 4:   # very long word: hard cut
                cut = len(cur) - 1
                while cut > 1 and d.textlength(cur[:cut] + "-", font=fnt) > width:
                    cut -= 1
                lines.append(cur[:cut] + "-")
                cur = cur[cut:]
    if cur:
        lines.append(cur)
    return lines


def wrap_balanced(d, text, fnt, width):
    """Same number of lines as a greedy wrap, but with the lines evened out (no one-word last line)."""
    lines = wrap(d, text, fnt, width)
    n = len(lines)
    if n < 2:
        return lines
    lo, hi = width * 0.45, width
    for _ in range(12):
        mid = (lo + hi) / 2
        if len(wrap(d, text, fnt, mid)) <= n:
            hi = mid
        else:
            lo = mid
    return wrap(d, text, fnt, hi + 1)


def ellipsize(d, text, fnt, width):
    if d.textlength(text, font=fnt) <= width:
        return text
    while text and d.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text.rstrip() + "…"


_CMAP = None


def clean(text):
    """Drop characters the overlay fonts can't draw (emoji etc. would show as boxes)."""
    global _CMAP
    if _CMAP is None:
        try:
            from fontTools.ttLib import TTFont
            _CMAP = set(TTFont(str(FONT_DIR / "Figtree-SemiBold.ttf"))["cmap"].getBestCmap())
        except Exception:
            _CMAP = set(range(32, 0x2FFF))
    out = "".join(ch for ch in (text or "") if ord(ch) in _CMAP or ch in " \n")
    return " ".join(out.split())


def short_game(label):
    s = (label or "").replace("Housebets", "").replace("(placeholder)", "").strip()
    return s or "—"


def fmt_mult(m):
    if m is None:
        return "—"
    return f"{m:.2f}×" if m < 100 else f"{m:,.0f}×"


def ease(k):
    k = max(0.0, min(1.0, k))
    return 1 - (1 - k) ** 3


# ---- small drawn icons --------------------------------------------------------------------------
def icon_dice(size, color=CYAN, pip=BG0):
    s = size * SS
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((s * .08, s * .08, s * .92, s * .92), radius=s * .2, fill=rgba(color))
    r = s * .075
    for fx, fy in ((.3, .3), (.7, .3), (.5, .5), (.3, .7), (.7, .7)):
        d.ellipse((s * fx - r, s * fy - r, s * fx + r, s * fy + r), fill=rgba(pip))
    return im.resize((size, size), Image.Resampling.LANCZOS)


def icon_plinko(size, color=CYAN, ball=GOLD):
    s = size * SS
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = s * .055
    rows = 4
    for i in range(rows):
        n = i + 2
        y = s * (.3 + i * .19)
        for j in range(n):
            x = s * .5 + (j - (n - 1) / 2) * s * .2
            d.ellipse((x - r, y - r, x + r, y + r), fill=rgba(color))
    R = s * .1
    d.ellipse((s * .5 - R + s * .1, s * .12 - R, s * .5 + R + s * .1, s * .12 + R), fill=rgba(ball))
    return im.resize((size, size), Image.Resampling.LANCZOS)


def icon_mines(size, color=CYAN, shine=BG0):
    s = size * SS
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon([(s * .25, s * .2), (s * .75, s * .2), (s * .93, s * .4), (s * .5, s * .88), (s * .07, s * .4)],
              fill=rgba(color))
    d.line([(s * .07, s * .4), (s * .93, s * .4)], fill=rgba(shine), width=max(1, int(s * .05)))
    d.line([(s * .35, s * .21), (s * .5, s * .86), (s * .65, s * .21)], fill=rgba(shine), width=max(1, int(s * .04)))
    return im.resize((size, size), Image.Resampling.LANCZOS)


def game_icon(label, size, color=CYAN):
    lb = (label or "").lower()
    if "plinko" in lb:
        return icon_plinko(size, color)
    if "mines" in lb:
        return icon_mines(size, color)
    return icon_dice(size, color)


def tophat(size, color=GOLD):
    s = size * SS
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((s * .26, s * .12, s * .74, s * .72), radius=s * .05, fill=rgba(color))
    d.rounded_rectangle((s * .06, s * .68, s * .94, s * .84), radius=s * .08, fill=rgba(color))
    d.rectangle((s * .26, s * .56, s * .74, s * .64), fill=(0, 0, 0, 0))
    return im.resize((size, size), Image.Resampling.LANCZOS)


# ---- fitting the game picture -------------------------------------------------------------------
def fit_game(bgr, w, h):
    """Game crop (BGR) -> RGB w x h, never cutting anything off. Close aspect: fit and pad with the crop's own
    edge colour (the Housebets panel border is a flat dark colour, so the pad is invisible); far-off aspect
    (placeholder, odd window size): fit over a dimmed, blurred copy."""
    ih, iw = bgr.shape[:2]
    s = min(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * s))), max(1, int(round(ih * s)))
    fg = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    if abs((iw / ih) / (w / h) - 1) < 0.14:
        edge = np.concatenate([bgr[:3].reshape(-1, 3), bgr[-3:].reshape(-1, 3), bgr[:, :3].reshape(-1, 3),
                               bgr[:, -3:].reshape(-1, 3)])
        out = np.empty((h, w, 3), np.uint8)
        out[:] = np.median(edge, axis=0).astype(np.uint8)
    else:
        bg = cv2.resize(cv2.resize(bgr, (32, 18), interpolation=cv2.INTER_AREA), (w, h),
                        interpolation=cv2.INTER_LINEAR)
        out = (cv2.GaussianBlur(bg, (0, 0), 9) * 0.35).astype(np.uint8)
    x, y = (w - nw) // 2, (h - nh) // 2
    out[y:y + nh, x:x + nw] = fg
    return np.ascontiguousarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))


class RoundPaste:
    """Copy a w x h picture into the frame with anti-aliased rounded corners (only the 4 corner patches are
    blended per frame; the rest is a plain copy)."""

    def __init__(self, base, rect, r):
        x, y, w, h = rect
        self.rect, self.r = rect, r
        m = np.array(rr_mask(w, h, r), np.float32)[..., None] / 255.0
        self.patches = []
        for (cx, cy) in ((0, 0), (w - r, 0), (0, h - r), (w - r, h - r)):
            a = m[cy:cy + r, cx:cx + r]
            b = base[y + cy:y + cy + r, x + cx:x + cx + r].astype(np.float32)
            self.patches.append((cx, cy, a, b * (1 - a)))

    def paste(self, frame, img):
        x, y, w, h = self.rect
        dst = frame[y:y + h, x:x + w]
        dst[:] = img
        r = self.r
        for cx, cy, a, bterm in self.patches:
            sub = dst[cy:cy + r, cx:cx + r]
            sub[:] = (sub * a + bterm).astype(np.uint8)


class Sprite:
    """Pre-multiplied RGBA overlay blended into the frame (name plates, badges over live video)."""

    def __init__(self, img, x, y):
        a = np.array(img, np.float32)
        self.x, self.y = x, y
        self.alpha = a[..., 3:4] / 255.0
        self.color = a[..., :3] * self.alpha
        self.inv = 1 - self.alpha
        self.h, self.w = a.shape[:2]

    def blend(self, frame):
        dst = frame[self.y:self.y + self.h, self.x:self.x + self.w]
        dst[:] = (dst * self.inv + self.color).astype(np.uint8)


# ---- layouts ------------------------------------------------------------------------------------
class Layout:
    """Shared machinery; subclasses set geometry and draw their own pieces."""
    name = "?"
    GAME = CAM = CHAT = CAP = (0, 0, 1, 1)
    BOTTOM = (0, 690, 1280, 30)
    GAME_R = 14
    CAM_R = 14

    def __init__(self):
        img = self.background()
        self.draw_static(img)
        self.draw_bottom(img)
        self.base_img = img
        self.base = np.array(img.convert("RGB"))
        self.game_paste = RoundPaste(self.base, self.GAME, self.GAME_R)
        self.cam_paste = RoundPaste(self.base, self.CAM, self.CAM_R)
        self.sprites = self.make_sprites()
        self.cache = {}
        self.row_born = {}
        self.switch_bg = None
        self.switch_key = None
        self.hubie_still = None

    # -- static --
    def background(self):
        img = vgrad(W, H, (6, 20, 27), (2, 9, 13))
        radial(img, 160, -60, 760, 420, (10, 120, 130), 60)
        radial(img, 1240, 760, 640, 360, (120, 84, 20), 34)
        # faint diagonal pinstripes, very low contrast (texture, not pattern)
        stripes = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(stripes)
        for x in range(-H, W, 9):
            d.line((x, 0, x + H, H), fill=10, width=1)
        src = Image.new("RGBA", (W, H), (120, 220, 230, 255))
        src.putalpha(stripes)
        img.alpha_composite(src)
        # vignette
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        v = (((xx - W / 2) / (W * .62)) ** 2 + ((yy - H / 2) / (H * .7)) ** 2).clip(0, 1.4)
        a = (v ** 1.6 * 120).clip(0, 160).astype(np.uint8)
        dark = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        dark.putalpha(Image.fromarray(a))
        img.alpha_composite(dark)
        return img

    def draw_static(self, img):
        raise NotImplementedError

    def draw_bottom(self, img):
        """Compliance bar: 18+, play responsibly, demo mode, BeGambleAware. Always on screen."""
        x, y, w, h = self.BOTTOM
        img.alpha_composite(vgrad(w, h, (5, 15, 20), (2, 8, 11)), (x, y))
        img.alpha_composite(hgrad(w, 1, (20, 70, 80), (20, 70, 80)), (x, y))
        d = ImageDraw.Draw(img)
        cy = y + h // 2
        # 18+ roundel
        shape_fill(img, (14, cy - 11, 50, cy + 11), 11, RED)
        d.text((32, cy), "18+", font=F("Figtree-Black", 15), fill=WHITE, anchor="mm")
        xx = 62
        parts = [("PLAY RESPONSIBLY", WHITE), ("•", DIM2), ("DEMO MODE", CYAN_SOFT),
                 ("zero-amount bets, no real money", TXT), ("•", DIM2), ("AI-RUN STREAM", WHITE)]
        for t, c in parts:
            f = F("Figtree-ExtraBold", 15) if t.isupper() else F("Figtree-SemiBold", 15)
            if t == "•":
                f = F("Figtree-Bold", 15)
            tr = 1.2 if t.isupper() else 0
            xx += tracked(d, (xx, cy), t, f, c, tr, "lm") + 10
        tracked(d, (W - 16, cy), "BeGambleAware.org", F("Figtree-ExtraBold", 15), WHITE, 0.3, "rm")
        tw = tracked_len(d, "BeGambleAware.org", F("Figtree-ExtraBold", 15), 0.3)
        d.text((W - 26 - tw, cy), "help & support:", font=F("Figtree-SemiBold", 15), fill=DIM, anchor="rm")

    def game_bezel(self, img, color=CYAN, rim=(CYAN_MID, TEAL_DEEP), glow_a=0.55):
        x, y, w, h = self.GAME
        r = self.GAME_R
        shadow(img, (x, y, x + w, y + h), r + 4, blur=18, alpha=200, dy=8)
        glow(img, (x - 2, y - 2, x + w + 2, y + h + 2), r + 2, color, blur=16, strength=glow_a, width=5)
        shape_fill(img, (x - 6, y - 6, x + w + 6, y + h + 6), r + 6, vgrad(w + 12, h + 12, PANEL_HI2, PANEL))
        ring_grad(img, (x - 6, y - 6, x + w + 6, y + h + 6), r + 6, rim[0], rim[1], 1.6)
        shape_fill(img, (x - 1, y - 1, x + w + 1, y + h + 1), r + 1, BG0)
        ring_grad(img, (x - 2, y - 2, x + w + 2, y + h + 2), r + 2, (60, 120, 130), (20, 50, 60), 1.2)

    # -- sprites over live video (subclass) --
    def make_sprites(self):
        return []

    # -- layer cache --
    def layer(self, name, key, fn):
        c = self.cache.get(name)
        if c is not None and c[0] == key:
            return c[1]
        arr = fn()
        self.cache[name] = (key, arr)
        return arr

    def crop(self, rect):
        x, y, w, h = rect
        return self.base_img.crop((x, y, x + w, y + h))

    @staticmethod
    def put(frame, rect, arr):
        x, y = rect[0], rect[1]
        h, w = arr.shape[:2]
        frame[y:y + h, x:x + w] = arr

    # -- chat rows: first-seen time drives the entry animation --
    def stamp_rows(self, rows, now):
        keys = []
        for i, r in enumerate(rows):
            k = (r.get("user"), r.get("text"), r.get("speech_id"), r.get("hubie", False))
            if k not in self.row_born:
                # rows present on the first frame do not animate
                self.row_born[k] = now if self.row_born else now - 10
            keys.append(k)
        if len(self.row_born) > 300:
            live = set(keys)
            self.row_born = {k: v for k, v in self.row_born.items() if k in live}
        return keys

    # -- the frame --
    def compose(self, frame, ctx):
        """ctx: game (RGB at GAME size or None), game_mode ('live'|'placeholder'|'stale'), cam (RGB at CAM size),
        speaking, level, caption {text, reply_to, reveal}, chat (rows), uptime, game_label, stats, switch, t."""
        np.copyto(frame, self.base)
        t = ctx.get("t", time.time())
        gimg = ctx.get("game")
        if ctx.get("game_mode") == "stale" or gimg is None:
            gimg = self.layer("stale", int(t) // 2, self.render_stale)
        sw = ctx.get("switch")
        if sw:
            gimg = self.switch_frame(gimg, sw, t)
        self.game_paste.paste(frame, gimg)
        if ctx.get("cam") is not None:
            self.cam_paste.paste(frame, ctx["cam"])
        for s in self.sprites:
            s.blend(frame)
        self.draw_dynamic(frame, ctx, t)
        return frame

    def draw_dynamic(self, frame, ctx, t):
        raise NotImplementedError

    # -- shared dynamic pieces -------------------------------------------------------------------
    def eq_bars(self, frame, x, y, h, level, t, color=GOLD, n=4, bw=4, gap=3):
        """Little live equaliser while Hubie speaks (drawn straight into the frame)."""
        for i in range(n):
            k = 0.35 + 0.65 * abs(math.sin(t * (6.3 + i * 1.7) + i * 1.3))
            bh = max(3, int(h * min(1.0, 0.25 + level * 1.6) * k))
            x0 = x + i * (bw + gap)
            frame[y + h - bh:y + h, x0:x0 + bw] = color

    def chat_view(self, rect, rows, keys, font_size=20, pad=12, max_lines=4, gap=8):
        """Lay out chat bubbles bottom-up (whole messages only) on a canvas one 'newest row' taller than the
        panel, so the entry animation is just a moving window over it: rendered once per chat change."""
        x, y, w, h = rect
        meas = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
        fn = F("Figtree-ExtraBold", font_size - 1)
        ft = F("Figtree-SemiBold", font_size)
        lh = int(font_size * 1.28)
        inner = w - 2 * pad
        blocks = []
        for r in rows:
            hub = r.get("hubie")
            name = "Hubie" if hub else (clean(r["user"]) or "viewer")
            ncol = GOLD if hub else name_color(name)
            nw = meas.textlength(name, font=fn) + (26 if hub else 0)
            first_w = inner - 24 - nw - 8          # name inline, text wraps after it
            words = clean(r["text"]).split() or ["…"]
            lines, cur = [], ""
            for wd in words:
                lim = first_w if not lines else inner - 24
                tt = (cur + " " + wd).strip()
                if meas.textlength(tt, font=ft) <= lim:
                    cur = tt
                else:
                    if cur:
                        lines.append(cur)
                    cur = wd
            if cur:
                lines.append(cur)
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                lines[-1] = ellipsize(meas, lines[-1] + " …", ft, inner - 24)
            blocks.append((hub, name, ncol, nw, lines, len(lines) * lh + 12))
        hn = (blocks[-1][5] + gap) if blocks else 0
        ch = h + hn
        lay = Image.new("RGBA", (w, ch), (0, 0, 0, 0))
        dd = ImageDraw.Draw(lay)
        yb = ch - 6
        for hub, name, ncol, nw, lines, bh in reversed(blocks):
            y1, y0 = yb, yb - bh
            yb = y0 - gap
            if y0 < hn + 2:                      # must sit whole inside the final window
                break
            if hub:
                shape_fill(lay, (pad, y0, pad + inner, y1), 12, vgrad(int(inner), int(bh), (52, 44, 22), (34, 30, 18), 245))
                shape_ring(lay, (pad, y0, pad + inner, y1), 12, (*GOLD_DEEP, 255), 1.3)
            else:
                shape_fill(lay, (pad, y0, pad + inner, y1), 12, (*PANEL_HI, 235))
            shape_fill(lay, (pad + 6, y0 + 7, pad + 10, y1 - 7), 2, ncol)     # accent bar
            tx, ty = pad + 18, y0 + 6 + lh // 2
            if hub:
                lay.alpha_composite(tophat(20, GOLD), (int(tx), int(ty - 11)))
                dd.text((tx + 24, ty), name, font=fn, fill=ncol, anchor="lm")
            else:
                dd.text((tx, ty), name, font=fn, fill=ncol, anchor="lm")
            for j, ln in enumerate(lines):
                dd.text((tx + nw + 8 if j == 0 else tx, ty + j * lh), ln, font=ft,
                        fill=WHITE if not hub else (255, 244, 220), anchor="lm")
        base = np.array(self.crop(rect).convert("RGB"), np.float32)
        arr = np.array(lay, np.float32)
        alpha = arr[..., 3:4] / 255.0
        view = {"hn": hn, "base": base, "color": arr[..., :3] * alpha, "inv": 1 - alpha}
        final = (base * view["inv"][hn:] + view["color"][hn:]).astype(np.uint8)
        if not rows:
            img = Image.fromarray(final)
            d = ImageDraw.Draw(img)
            d.text((w // 2, h // 2 - 12), "Chat's quiet.", font=F("Figtree-Bold", 20), fill=TXT, anchor="mm")
            d.text((w // 2, h // 2 + 14), "Say hi, Hubie answers everyone by name",
                   font=F("Figtree-Medium", 16), fill=DIM, anchor="mm")
            final = np.array(img)
        view["final"] = final
        return view

    def chat_frame(self, frame, rect, rows, keys, t, **kw):
        """Copy the chat panel into the frame; the newest row slides in over 0.4 s."""
        view = self.layer("chat", tuple(keys), lambda: self.chat_view(rect, rows, keys, **kw))
        k = ease((t - self.row_born.get(keys[-1], 0)) / 0.4) if keys else 1.0
        if k >= 1 or not view["hn"]:
            self.put(frame, rect, view["final"])
            return
        off = int(round(view["hn"] * k))
        h = rect[3]
        out = view["base"] * view["inv"][off:off + h] + view["color"][off:off + h]
        self.put(frame, rect, out.astype(np.uint8))

    def caption_lines(self, d, text, width, sizes, font="Figtree-Bold"):
        text = clean(text)
        for size, maxl in sizes:
            f = F(font, size)
            lines = wrap(d, text or "…", f, width)
            if len(lines) <= maxl:
                return f, size, wrap_balanced(d, text or "…", f, width)
        lines = lines[:maxl]
        lines[-1] = ellipsize(d, lines[-1] + " …", f, width)
        return f, size, lines

    @staticmethod
    def reveal_lines(lines, reveal):
        """Word-by-word reveal without re-wrapping: keep the first `reveal` words of the laid-out lines."""
        if reveal is None:
            return lines
        out, left = [], reveal
        for ln in lines:
            ws = ln.split()
            if left <= 0:
                out.append("")
                continue
            out.append(" ".join(ws[:left]))
            left -= len(ws)
        return out

    def result_chips(self, d, img, x, y, results, h=24, maxw=300, size=15, gap=5):
        """Last rounds as small chips (newest first)."""
        f = F("Oxanium-Bold", size)
        xx = x
        for t, win in results:
            tw = d.textlength(t, font=f) + 14
            if xx + tw > x + maxw:
                break
            c = WIN if win else LOSS
            shape_fill(img, (xx, y, xx + tw, y + h), 7, (*mix(PANEL, c, 0.16), 255))
            shape_ring(img, (xx, y, xx + tw, y + h), 7, (*mix(PANEL, c, 0.55), 255), 1.0)
            d.text((xx + tw / 2, y + h / 2 + 1), t, font=f, fill=c, anchor="mm")
            xx += tw + gap
        return xx

    def live_pill(self, img, d, x, cy, t, h=26):
        """Red LIVE pill with a pulsing dot. Returns right edge."""
        f = F("Figtree-Black", 15)
        tw = tracked_len(d, "LIVE", f, 1.5)
        w = int(tw + 38)
        glow(img, (x, cy - h // 2, x + w, cy + h // 2), h // 2, RED, blur=7, strength=0.55, width=3)
        shape_fill(img, (x, cy - h // 2, x + w, cy + h // 2), h // 2, vgrad(w, h, (250, 76, 98), (206, 32, 58)))
        pulse = 1.0 if int(t) % 2 == 0 else 0.0
        dot = mix((255, 160, 170), WHITE, pulse)
        d.ellipse((x + 10, cy - 4, x + 18, cy + 4), fill=dot)
        tracked(d, (x + 25, cy + 1), "LIVE", f, WHITE, 1.5, "lm")
        return x + w

    @staticmethod
    def game_name(label, mode):
        if mode == "stale":
            return "Reconnecting"
        if mode == "placeholder":
            return "Stand-in dice"
        return short_game(label)

    @staticmethod
    def hms(s):
        s = int(max(0, s))
        if s >= 86400:          # a 24/7 run: days + hh:mm keeps the width of hh:mm:ss
            return f"{s // 86400}d {s % 86400 // 3600:02d}:{s % 3600 // 60:02d}"
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

    # -- game switch card --------------------------------------------------------------------------
    def switch_frame(self, gimg, sw, t):
        """'Next up' card over the game area. sw: {to, since, until (or None)}."""
        x, y, w, h = self.GAME
        key = (sw["to"], sw["since"])
        if self.switch_key != key:
            self.switch_key = key
            g = gimg if gimg is not None else np.zeros((h, w, 3), np.uint8)
            small = cv2.resize(g, (w // 8, h // 8), interpolation=cv2.INTER_AREA)
            bg = cv2.GaussianBlur(cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR), (0, 0), 6)
            tint = np.array(BG0, np.float32)
            self.switch_bg = (bg.astype(np.float32) * 0.28 + tint * 0.72).astype(np.uint8)
            self.cache.pop("switch", None)
        age = t - sw["since"]
        prog = min(1.0, age / max(1.0, sw.get("expect", 6.0)))
        card = self.layer("switch", (key, int(prog * 40)), lambda: self.render_switch(sw["to"], prog))
        k = ease(age / 0.35)
        if sw.get("until"):
            k = min(k, ease((sw["until"] - t) / 0.35))
        if k >= 1 or gimg is None:
            return card
        return cv2.addWeighted(gimg, 1 - k, card, k, 0)

    def render_switch(self, label, prog):
        x, y, w, h = self.GAME
        img = Image.fromarray(self.switch_bg).convert("RGBA")
        d = ImageDraw.Draw(img)
        cx, cy = w // 2, h // 2
        cw, ch = min(520, w - 80), 250
        box = (cx - cw // 2, cy - ch // 2, cx + cw // 2, cy + ch // 2)
        glow(img, box, 22, CYAN, blur=20, strength=0.5, width=6)
        shape_fill(img, box, 22, vgrad(cw, ch, (16, 44, 56), (8, 22, 30), 250))
        ring_grad(img, box, 22, CYAN_MID, TEAL_DEEP, 1.6)
        ic = game_icon(label, 76)
        img.alpha_composite(ic, (cx - 38, box[1] + 22))
        tracked(d, (cx, box[1] + 120), "NEXT UP", F("Figtree-Black", 17), CYAN, 4.0, "ms")
        name = short_game(label).upper()
        d.text((cx, box[1] + 168), name, font=F("RedHatDisplay-Black", 52), fill=WHITE, anchor="ms")
        d.text((cx, box[1] + 200), "Housebets original  •  demo, zero-amount bets", font=F("Figtree-SemiBold", 17),
               fill=DIM, anchor="ms")
        bx0, bx1, by = box[0] + 60, box[2] - 60, box[3] - 26
        shape_fill(img, (bx0, by, bx1, by + 6), 3, (*PANEL_HI2, 255))
        if prog > 0.02:
            shape_fill(img, (bx0, by, bx0 + max(8, (bx1 - bx0) * prog), by + 6), 3, CYAN)
        return np.array(img.convert("RGB"))

    # -- stale feed card (game process silent) ---------------------------------------------------
    def render_stale(self):
        x, y, w, h = self.GAME
        img = vgrad(w, h, (10, 30, 40), (4, 13, 18))
        d = ImageDraw.Draw(img)
        radial_small = Image.new("L", (w, h), 0)
        ImageDraw.Draw(radial_small).ellipse((w * .2, h * .1, w * .8, h * .9), fill=70)
        radial_small = radial_small.filter(ImageFilter.GaussianBlur(60))
        src = Image.new("RGBA", (w, h), rgba(CYAN_MID))
        src.putalpha(radial_small)
        img.alpha_composite(src)
        cx, cy = w // 2, h // 2
        R = 74
        if self.hubie_still is not None:
            hs = Image.fromarray(self.hubie_still)
            s = 2 * R / min(hs.size) * 1.35
            hs = hs.resize((int(hs.width * s), int(hs.height * s)), Image.Resampling.LANCZOS)
            ox, oy = hs.width // 2 - R, int(hs.height * 0.18)
            hs = hs.crop((ox, oy, ox + 2 * R, oy + 2 * R)).convert("RGBA")
            m = Image.new("L", (2 * R * SS, 2 * R * SS), 0)
            ImageDraw.Draw(m).ellipse((0, 0, 2 * R * SS - 1, 2 * R * SS - 1), fill=255)
            hs.putalpha(m.resize((2 * R, 2 * R), Image.Resampling.LANCZOS))
            glow(img, (cx - R, cy - 70 - R, cx + R, cy - 70 + R), R, GOLD, blur=12, strength=0.6, width=4)
            img.alpha_composite(hs, (cx - R, cy - 70 - R))
            d.ellipse((cx - R - 2, cy - 70 - R - 2, cx + R + 2, cy - 70 + R + 2), outline=GOLD, width=3)
        tracked(d, (cx, cy + 40), "BE RIGHT BACK", F("Figtree-Black", 16), CYAN, 4.0, "ms")
        d.text((cx, cy + 82), "Game feed reconnecting", font=F("RedHatDisplay-Black", 34), fill=WHITE, anchor="ms")
        dots = "." * (1 + int(time.time()) // 2 % 3)
        d.text((cx + d.textlength("Game feed reconnecting", font=F("RedHatDisplay-Black", 34)) / 2 + 2, cy + 82),
               dots, font=F("RedHatDisplay-Black", 34), fill=WHITE, anchor="ls")
        d.text((cx, cy + 114), "Hubie keeps the show going meanwhile", font=F("Figtree-SemiBold", 18), fill=DIM,
               anchor="ms")
        return np.array(img.convert("RGB"))


# =================================================================================================
class LayoutA(Layout):
    """Broadcast: top info strip, big game, right rail (host cam + chat), lower-third caption."""
    name = "A"
    TOP = (0, 0, 1280, 50)
    GAME = (20, 64, 744, 506)        # ~1.47, the shape of the Housebets game panel
    CAP = (20, 586, 744, 94)
    CAM = (784, 64, 476, 268)
    PLATE = (796, 306, 300, 50)      # over the cam's bottom edge
    CHAT = (784, 372, 476, 308)
    CHAT_TOP = 46

    def draw_static(self, img):
        d = ImageDraw.Draw(img)
        # top strip
        x, y, w, h = self.TOP
        img.alpha_composite(vgrad(w, h, (8, 24, 31), (4, 14, 19), 235), (0, 0))
        img.alpha_composite(hgrad(w, 2, (2, 120, 130), (2, 40, 50)), (0, h - 2))
        # wordmark
        f1 = F("RedHatDisplay-Black", 26)
        d.text((22, 26), "HUBIE", font=f1, fill=WHITE, anchor="lm")
        x1 = 22 + d.textlength("HUBIE", font=f1) + 8
        shape_fill(img, (x1, 13, x1 + 52, 39), 7, CYAN)
        d.text((x1 + 26, 26), "24/7", font=F("Oxanium-ExtraBold", 18), fill=BG0, anchor="mm")
        # game bezel
        self.game_bezel(img)
        # cam bezel: gold rim (his colour) with a cyan glow
        x, y, w, h = self.CAM
        r = self.CAM_R
        shadow(img, (x, y, x + w, y + h), r + 4, blur=18, alpha=200, dy=8)
        glow(img, (x - 3, y - 3, x + w + 3, y + h + 3), r + 3, GOLD, blur=16, strength=0.45, width=5)
        shape_fill(img, (x - 6, y - 6, x + w + 6, y + h + 6), r + 6, vgrad(w + 12, h + 12, (60, 48, 24), (24, 22, 16)))
        ring_grad(img, (x - 6, y - 6, x + w + 6, y + h + 6), r + 6, GOLD_LIGHT, GOLD_DEEP, 1.8)
        shape_fill(img, (x - 1, y - 1, x + w + 1, y + h + 1), r + 1, BG0)
        # chat panel
        x, y, w, h = self.CHAT
        shadow(img, (x, y, x + w, y + h), 16, blur=16, alpha=170, dy=6)
        shape_fill(img, (x, y, x + w, y + h), 16, vgrad(w, h, (12, 31, 40), (7, 20, 27), 245))
        ring_grad(img, (x, y, x + w, y + h), 16, (40, 92, 106), (18, 44, 54), 1.2)
        shape_fill(img, (x + 16, y + 17, x + 28, y + 29), 6, KICK)
        tracked(d, (x + 36, y + 24), "KICK CHAT", F("Figtree-Black", 16), WHITE, 2.0, "lm")
        d.text((x + w - 16, y + 24), "Hubie answers by name", font=F("Figtree-SemiBold", 15), fill=DIM, anchor="rm")
        d.line((x + 14, y + self.CHAT_TOP - 4, x + w - 14, y + self.CHAT_TOP - 4), fill=(26, 58, 70), width=1)
        # lower third
        x, y, w, h = self.CAP
        shadow(img, (x, y, x + w, y + h), 14, blur=14, alpha=190, dy=6)
        shape_fill(img, (x, y, x + w, y + h), 14, vgrad(w, h, (13, 34, 44), (6, 18, 24), 248))
        ring_grad(img, (x, y, x + w, y + h), 14, (44, 100, 112), (16, 40, 50), 1.2)
        # gold name block
        bw = 136
        shape_fill(img, (x, y, x + bw, y + h), 14, vgrad(bw, h, GOLD_LIGHT, GOLD_DEEP))
        img.alpha_composite(vgrad(14, h, GOLD, GOLD_DEEP), (x + bw - 14, y))     # square right edge
        img.alpha_composite(hgrad(3, h - 20, CYAN, CYAN_MID), (x + bw + 1, y + 10))
        d.text((x + bw // 2, y + 36), "Hubie", font=F("DMSerifDisplay-Regular", 34), fill=(30, 22, 8), anchor="ms")

    def make_sprites(self):
        x, y, w, h = self.PLATE
        pad = 30
        img = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
        box = (pad, pad, pad + w, pad + h)
        shadow(img, box, 12, blur=10, alpha=200, dy=4)
        shape_fill(img, box, 12, vgrad(w, h, (16, 38, 48), (6, 18, 24), 250))
        ring_grad(img, box, 12, GOLD_LIGHT, GOLD_DEEP, 1.6)
        d = ImageDraw.Draw(img)
        img.alpha_composite(tophat(30, GOLD), (pad + 12, pad + 9))
        d.text((pad + 50, pad + 34), "Hubie", font=F("DMSerifDisplay-Regular", 30), fill=GOLD_LIGHT, anchor="ls")
        tx = pad + 50 + d.textlength("Hubie", font=F("DMSerifDisplay-Regular", 30)) + 12
        tracked(d, (tx, pad + 20), "YOUR HOST", F("Figtree-Black", 12), CYAN, 2.0, "lm")
        d.text((tx, pad + 36), "the House sloth", font=F("Figtree-SemiBold", 14), fill=TXT, anchor="lm")
        return [Sprite(img, x - pad, y - pad)]

    def draw_dynamic(self, frame, ctx, t):
        # top strip (once a second)
        stats = ctx.get("stats") or {}
        key = (int(ctx.get("uptime", 0)), int(t) % 2, ctx.get("game_label"), ctx.get("game_mode"),
               stats.get("rounds"), stats.get("best"), tuple(stats.get("last", [])[:6]))
        self.put(frame, self.TOP, self.layer("top", key, lambda: self.render_top(ctx, t)))
        # caption
        cap = ctx.get("caption") or {}
        key = (cap.get("text"), cap.get("reply_to"), cap.get("reveal"), bool(ctx.get("speaking")))
        self.put(frame, (self.CAP[0] + 140, self.CAP[1]),
                 self.layer("cap", key, lambda: self.render_caption(cap, ctx.get("speaking"))))
        if ctx.get("speaking"):
            x, y, w, h = self.CAP
            self.eq_bars(frame, x + 48, y + 52, 22, ctx.get("level", 0), t, color=(48, 34, 10), n=6, bw=5, gap=3)
        # chat
        rows = ctx.get("chat") or []
        keys = self.stamp_rows(rows, t)
        x, y, w, h = self.CHAT
        rect = (x + 2, y + self.CHAT_TOP, w - 4, h - self.CHAT_TOP - 4)
        self.chat_frame(frame, rect, rows, keys, t)

    def render_top(self, ctx, t):
        x, y, w, h = self.TOP
        img = self.crop(self.TOP).convert("RGBA")
        d = ImageDraw.Draw(img)
        cy = 25
        xx = self.live_pill(img, d, 172, cy, t)
        up = self.hms(ctx.get("uptime", 0))
        d.text((xx + 12, cy + 1), up, font=F("Oxanium-Bold", 20), fill=WHITE, anchor="lm")
        xx += 12 + max(d.textlength("00:00:00", font=F("Oxanium-Bold", 20)), d.textlength(up, font=F("Oxanium-Bold", 20))) + 22
        d.line((xx, 12, xx, 38), fill=(30, 70, 84), width=1)
        xx += 22
        # now playing
        label = ctx.get("game_label") or ""
        mode = ctx.get("game_mode")
        tracked(d, (xx, cy + 1), "NOW PLAYING", F("Figtree-Black", 12), DIM, 1.6, "lm")
        xx += tracked_len(d, "NOW PLAYING", F("Figtree-Black", 12), 1.6) + 10
        name = self.game_name(label, mode)
        nf = F("Figtree-ExtraBold", 20)
        nw = d.textlength(name, font=nf)
        pw = int(nw + 50)
        shape_fill(img, (xx, cy - 15, xx + pw, cy + 15), 15, (*PANEL_HI2, 255))
        shape_ring(img, (xx, cy - 15, xx + pw, cy + 15), 15, (*(CYAN_MID if mode == "live" else GOLD_DEEP), 255), 1.2)
        img.alpha_composite(game_icon(label, 22, CYAN if mode == "live" else GOLD), (int(xx + 9), cy - 11))
        d.text((xx + 38, cy + 1), name, font=nf, fill=WHITE, anchor="lm")
        xx += pw + 22
        stats = ctx.get("stats") or {}
        # right side: rounds / best / last results; drop chips (oldest first) until it clears the left side
        lf, vf, cf = F("Figtree-Black", 12), F("Oxanium-Bold", 20), F("Oxanium-Bold", 15)
        vals = (("BEST", fmt_mult(stats.get("best")) if stats.get("best") else "—", GOLD),
                ("ROUNDS", f"{stats.get('rounds', 0):,}", WHITE))
        fixed = sum(d.textlength(v, font=vf) + 8 + tracked_len(d, lab, lf, 1.6) + 22 for lab, v, _ in vals)
        last = stats.get("last", [])[:5]
        while True:
            widths = [d.textlength(t_, font=cf) + 14 for t_, _ in last]
            chips = (sum(widths) + 5 * (len(widths) - 1) + 12 + tracked_len(d, "LAST", lf, 1.6) + 22) if last else 0
            if W - 18 - chips - fixed >= xx or not last:
                break
            last = last[:-1]
        rx = W - 18
        if last:
            cw = sum(widths) + 5 * (len(widths) - 1)
            self.result_chips(d, img, rx - cw, cy - 12, last, maxw=cw + 1)
            rx -= cw + 12
            tracked(d, (rx, cy + 1), "LAST", lf, DIM, 1.6, "rm")
            rx -= tracked_len(d, "LAST", lf, 1.6) + 22
        for lab, val, col in vals:
            d.text((rx, cy + 1), val, font=vf, fill=col, anchor="rm")
            rx -= d.textlength(val, font=vf) + 8
            tracked(d, (rx, cy + 1), lab, lf, DIM, 1.6, "rm")
            rx -= tracked_len(d, lab, lf, 1.6) + 22
        return np.array(img.convert("RGB"))

    def render_caption(self, cap, speaking):
        x, y, w, h = self.CAP
        rect = (x + 140, y, w - 140, h)
        img = self.crop(rect).convert("RGBA")
        d = ImageDraw.Draw(img)
        iw = rect[2] - 24
        text = cap.get("text") or ""
        if not text:
            d.text((14, h // 2), "…", font=F("Figtree-Bold", 28), fill=DIM, anchor="lm")
            return np.array(img.convert("RGB"))
        reply = cap.get("reply_to")
        f, size, lines = self.caption_lines(d, text, iw, ((28, 2), (24, 3), (21, 3), (19, 4)))
        lines = self.reveal_lines(lines, cap.get("reveal"))
        lh = int(size * 1.22)
        tot = lh * len(lines)
        y0 = (h - tot) // 2 + lh // 2
        col = WHITE if speaking else (205, 222, 228)
        for i, ln in enumerate(lines):
            d.text((14, y0 + i * lh), ln, font=f, fill=col, anchor="lm")
        out = np.array(img.convert("RGB"))
        if reply:
            # small "to @user" tag on the gold block (drawn there by compose via a separate layer)
            pass
        return out

    def compose(self, frame, ctx):
        super().compose(frame, ctx)
        # reply tag on the gold block (under the name)
        cap = ctx.get("caption") or {}
        x, y, w, h = self.CAP
        rect = (x + 8, y + 50, 124, 36)
        reply = cap.get("reply_to")
        if not ctx.get("speaking"):
            self.put(frame, rect, self.layer("reply", reply, lambda: self.render_reply(reply, rect)))
        return frame

    def render_reply(self, reply, rect):
        img = self.crop(rect).convert("RGBA")
        d = ImageDraw.Draw(img)
        if reply:
            f = F("Figtree-ExtraBold", 15)
            d.text((rect[2] // 2, 12), "replying to", font=F("Figtree-Bold", 12), fill=(64, 46, 14), anchor="mm")
            d.text((rect[2] // 2, 28), "@" + ellipsize(d, reply, f, rect[2] - 18), font=f, fill=(30, 22, 8), anchor="mm")
        else:
            tracked(d, (rect[2] // 2, 20), "ON AIR", F("Figtree-Black", 13), (64, 46, 14), 2.5, "mm")
        return np.array(img.convert("RGB"))


# =================================================================================================
class LayoutB(Layout):
    """Lounge: Hubie first. Big host cam top-left in a gold deco frame, speech bubble under him,
    session card, game top-right, chat strip under the game."""
    name = "B"
    CAM = (24, 24, 496, 279)
    PLATE = (128, 284, 288, 52)
    CAP = (24, 352, 496, 182)
    INFO = (24, 548, 496, 132)
    GAME = (544, 24, 712, 474)
    CHAT = (544, 512, 712, 168)
    GAME_R = 14
    CAM_R = 10

    def background(self):
        img = super().background()
        radial(img, 250, 200, 520, 420, (150, 110, 30), 34)     # warm light behind Hubie
        return img

    def deco_corner(self, d, x, y, sx, sy, c):
        """Little art-deco step corner."""
        for i, L in enumerate((26, 18, 10)):
            o = 6 + i * 5
            d.line((x + sx * o, y + sy * o, x + sx * (o + L), y + sy * o), fill=c, width=2)
            d.line((x + sx * o, y + sy * o, x + sx * o, y + sy * (o + L)), fill=c, width=2)

    def draw_static(self, img):
        d = ImageDraw.Draw(img)
        # host cam: double gold frame + deco corners
        x, y, w, h = self.CAM
        shadow(img, (x - 8, y - 8, x + w + 8, y + h + 8), 18, blur=20, alpha=210, dy=10)
        glow(img, (x - 10, y - 10, x + w + 10, y + h + 10), 18, GOLD, blur=18, strength=0.5, width=5)
        shape_fill(img, (x - 12, y - 12, x + w + 12, y + h + 12), 18, vgrad(w + 24, h + 24, (44, 36, 20), (14, 14, 12)))
        ring_grad(img, (x - 12, y - 12, x + w + 12, y + h + 12), 18, GOLD_LIGHT, GOLD_DEEP, 2.2)
        ring_grad(img, (x - 5, y - 5, x + w + 5, y + h + 5), 13, GOLD_DEEP, GOLD_LIGHT, 1.2)
        shape_fill(img, (x - 1, y - 1, x + w + 1, y + h + 1), self.CAM_R + 1, BG0)
        d = ImageDraw.Draw(img)
        for (cx, cy, sx, sy) in ((x - 12, y - 12, 1, 1), (x + w + 12, y - 12, -1, 1), (x - 12, y + h + 12, 1, -1),
                                 (x + w + 12, y + h + 12, -1, -1)):
            self.deco_corner(d, cx, cy, sx, sy, (*GOLD, 255))
        # speech bubble (tail up towards Hubie)
        x, y, w, h = self.CAP
        shadow(img, (x, y, x + w, y + h), 18, blur=16, alpha=190, dy=8)
        bub = Image.new("RGBA", (w, h + 30), (0, 0, 0, 0))
        shape_fill(bub, (0, 30, w, h + 30), 18, vgrad(w, h, (238, 246, 247), (206, 228, 230)))
        tail = Image.new("L", ((w) * SS, 40 * SS), 0)
        tx = 452
        ImageDraw.Draw(tail).polygon([((tx - 22) * SS, 40 * SS), ((tx + 14) * SS, 40 * SS), ((tx + 2) * SS, 8 * SS)],
                                     fill=255)
        tail = tail.resize((w, 40), Image.Resampling.LANCZOS)
        tl = Image.new("RGBA", (w, 40), (238, 246, 247, 255))
        tl.putalpha(tail)
        bub.alpha_composite(tl, (0, 0))
        img.alpha_composite(bub, (x, y - 30))
        # session card
        x, y, w, h = self.INFO
        shadow(img, (x, y, x + w, y + h), 16, blur=16, alpha=170, dy=6)
        shape_fill(img, (x, y, x + w, y + h), 16, vgrad(w, h, (13, 34, 44), (7, 20, 27), 248))
        ring_grad(img, (x, y, x + w, y + h), 16, (44, 100, 112), (16, 40, 50), 1.2)
        # game bezel
        self.game_bezel(img, glow_a=0.5)
        # chat strip
        x, y, w, h = self.CHAT
        shadow(img, (x, y, x + w, y + h), 16, blur=16, alpha=170, dy=6)
        shape_fill(img, (x, y, x + w, y + h), 16, vgrad(w, h, (12, 31, 40), (7, 20, 27), 245))
        ring_grad(img, (x, y, x + w, y + h), 16, (40, 92, 106), (18, 44, 54), 1.2)
        # vertical "CHAT" tab on the left of the strip
        tab = Image.new("RGBA", (h - 24, 30), (0, 0, 0, 0))
        td = ImageDraw.Draw(tab)
        tracked(td, ((h - 24) // 2, 15), "KICK CHAT", F("Figtree-Black", 14), WHITE, 2.4, "mm")
        tab = tab.rotate(90, expand=True)
        shape_fill(img, (x + 8, y + 8, x + 40, y + h - 8), 10, (*PANEL_HI2, 255))
        shape_fill(img, (x + 18, y + h - 30, x + 30, y + h - 18), 6, KICK)
        img.alpha_composite(tab, (x + 9, y + 12 - 18 + 6))

    def make_sprites(self):
        x, y, w, h = self.PLATE
        pad = 30
        img = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
        box = (pad, pad, pad + w, pad + h)
        shadow(img, box, h // 2, blur=10, alpha=210, dy=4)
        shape_fill(img, box, h // 2, vgrad(w, h, (20, 44, 54), (7, 20, 26), 252))
        ring_grad(img, box, h // 2, GOLD_LIGHT, GOLD_DEEP, 2.0)
        d = ImageDraw.Draw(img)
        img.alpha_composite(tophat(32, GOLD), (pad + 18, pad + 9))
        d.text((pad + 60, pad + 36), "Hubie", font=F("DMSerifDisplay-Regular", 34), fill=GOLD_LIGHT, anchor="ls")
        tx = pad + 60 + d.textlength("Hubie", font=F("DMSerifDisplay-Regular", 34)) + 14
        tracked(d, (tx, pad + 21), "YOUR HOST", F("Figtree-Black", 12), CYAN, 2.0, "lm")
        d.text((tx, pad + 37), "the House sloth", font=F("Figtree-SemiBold", 14), fill=TXT, anchor="lm")
        return [Sprite(img, x - pad, y - pad)]

    def draw_dynamic(self, frame, ctx, t):
        cap = ctx.get("caption") or {}
        key = (cap.get("text"), cap.get("reply_to"), cap.get("reveal"), bool(ctx.get("speaking")))
        x, y, w, h = self.CAP
        rect = (x + 4, y + 4, w - 8, h - 8)
        self.put(frame, rect, self.layer("cap", key, lambda: self.render_caption(cap, ctx.get("speaking"), rect)))
        if ctx.get("speaking"):
            self.eq_bars(frame, x + w - 52, y + 16, 18, ctx.get("level", 0), t, color=TEAL_DEEP, n=5, bw=4, gap=3)
        stats = ctx.get("stats") or {}
        key = (int(ctx.get("uptime", 0)), int(t) % 2, ctx.get("game_label"), ctx.get("game_mode"),
               stats.get("rounds"), stats.get("best"), tuple(stats.get("last", [])[:7]))
        x, y, w, h = self.INFO
        rect = (x + 4, y + 4, w - 8, h - 8)
        self.put(frame, rect, self.layer("info", key, lambda: self.render_info(ctx, t, rect)))
        rows = ctx.get("chat") or []
        keys = self.stamp_rows(rows, t)
        x, y, w, h = self.CHAT
        rect = (x + 46, y + 3, w - 50, h - 6)
        self.chat_frame(frame, rect, rows, keys, t, font_size=18, pad=6, max_lines=2, gap=5)

    def render_caption(self, cap, speaking, rect):
        img = self.crop(rect).convert("RGBA")
        d = ImageDraw.Draw(img)
        w, h = rect[2], rect[3]
        reply = cap.get("reply_to")
        head_y = 22
        f_lab = F("Figtree-Black", 13)
        tracked(d, (16, head_y), "HUBIE SAYS" if not reply else "HUBIE  →", f_lab, (20, 110, 118), 2.0, "lm")
        if reply:
            lx = 16 + tracked_len(d, "HUBIE  →", f_lab, 2.0) + 8
            d.text((lx, head_y), "@" + ellipsize(d, reply, F("Figtree-ExtraBold", 16), w - lx - 70),
                   font=F("Figtree-ExtraBold", 16), fill=mix(name_color(reply), (10, 30, 40), 0.45), anchor="lm")
        text = cap.get("text") or ""
        if not text:
            d.text((16, h // 2 + 10), "…", font=F("Figtree-Bold", 30), fill=(90, 120, 130), anchor="lm")
            return np.array(img.convert("RGB"))
        f, size, lines = self.caption_lines(d, text, w - 32, ((30, 3), (27, 4), (23, 5), (20, 5)))
        lines = self.reveal_lines(lines, cap.get("reveal"))
        lh = int(size * 1.2)
        top = 38
        y0 = top + (h - top - lh * len(lines)) // 2 + lh // 2
        col = (8, 24, 32) if speaking else (40, 62, 72)
        for i, ln in enumerate(lines):
            d.text((16, y0 + i * lh), ln, font=f, fill=col, anchor="lm")
        return np.array(img.convert("RGB"))

    def render_info(self, ctx, t, rect):
        img = self.crop(rect).convert("RGBA")
        d = ImageDraw.Draw(img)
        w, h = rect[2], rect[3]
        stats = ctx.get("stats") or {}
        # row 1: brand + live + uptime
        f1 = F("RedHatDisplay-Black", 22)
        d.text((14, 22), "HUBIE", font=f1, fill=WHITE, anchor="lm")
        x1 = 14 + d.textlength("HUBIE", font=f1) + 6
        shape_fill(img, (x1, 10, x1 + 44, 34), 6, CYAN)
        d.text((x1 + 22, 22), "24/7", font=F("Oxanium-ExtraBold", 15), fill=BG0, anchor="mm")
        xx = self.live_pill(img, d, int(x1 + 58), 22, t, h=24)
        up = self.hms(ctx.get("uptime", 0))
        d.text((xx + 10, 23), up, font=F("Oxanium-Bold", 19), fill=WHITE, anchor="lm")
        ux = xx + 10 + d.textlength(up, font=F("Oxanium-Bold", 19)) + 12
        # now playing at right (shrinks rather than touching the clock)
        label = ctx.get("game_label") or ""
        mode = ctx.get("game_mode")
        name = self.game_name(label, mode)
        for size in (19, 17, 15):
            nf = F("Figtree-ExtraBold", size)
            if d.textlength(name, font=nf) + 44 <= w - 12 - ux:
                break
        name = ellipsize(d, name, nf, w - 12 - ux - 44)
        nw = d.textlength(name, font=nf)
        px1 = w - 12
        px0 = px1 - nw - 44
        shape_fill(img, (px0, 8, px1, 36), 14, (*PANEL_HI2, 255))
        shape_ring(img, (px0, 8, px1, 36), 14, (*(CYAN_MID if mode == "live" else GOLD_DEEP), 255), 1.2)
        img.alpha_composite(game_icon(label, 20, CYAN if mode == "live" else GOLD), (int(px0 + 8), 12))
        d.text((px0 + 34, 23), name, font=nf, fill=WHITE, anchor="lm")
        d.line((14, 46, w - 14, 46), fill=(26, 58, 70), width=1)
        # row 2: stats
        cols = [("ROUNDS", f"{stats.get('rounds', 0):,}", WHITE), ("WINS", f"{stats.get('wins', 0):,}", WIN),
                ("BEST", fmt_mult(stats.get("best")) if stats.get("best") else "—", GOLD)]
        cx = 14
        for lab, val, c in cols:
            tracked(d, (cx, 62), lab, F("Figtree-Black", 11), DIM, 1.6, "lm")
            d.text((cx, 86), val, font=F("Oxanium-Bold", 24), fill=c, anchor="lm")
            cx += 98
        tracked(d, (cx + 4, 62), "LAST ROUNDS", F("Figtree-Black", 11), DIM, 1.6, "lm")
        last = stats.get("last", [])[:7]
        if last:
            self.result_chips(d, img, cx + 4, 74, last, maxw=w - cx - 18, size=14, h=23, gap=4)
        else:
            d.text((cx + 4, 86), "first round coming up", font=F("Figtree-SemiBold", 15), fill=DIM, anchor="lm")
        tracked(d, (14, h - 12), "DEMO MODE  •  NOTHING AT STAKE", F("Figtree-Black", 11), (60, 150, 156), 1.6, "lm")
        return np.array(img.convert("RGB"))


class SessionStats:
    """Round stats for the info strip, fed from game_events.jsonl (real Housebets rounds only)."""

    def __init__(self):
        self.rounds = self.wins = 0
        self.best = None
        self.last = []          # [(chip text, win)], newest first

    def feed(self, e):
        if e.get("type") != "round" or not str(e.get("adapter", "")).startswith("housebets"):
            return
        self.rounds += 1
        win = bool(e.get("win"))
        m = e.get("multiplier")
        if win:
            self.wins += 1
            if m and (self.best is None or m > self.best):
                self.best = m
        if e.get("game") == "dice" and e.get("roll") is not None:
            txt = f"{e['roll']:.2f}"
        else:
            txt = fmt_mult(m)
        self.last = ([(txt, win)] + self.last)[:8]

    def snapshot(self):
        return {"rounds": self.rounds, "wins": self.wins, "best": self.best, "last": list(self.last)}


LAYOUTS = {"A": LayoutA, "B": LayoutB}


def make(name):
    return LAYOUTS.get((name or "A").strip().upper(), LayoutA)()
