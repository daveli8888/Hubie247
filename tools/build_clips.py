"""Build Hubie's cam clips from the Gemini sources (offline, run once after a source changes).

    python3 tools/build_clips.py [name ...]   # -> assets/clips/hubie_<name>.mp4 + assets/clips/anim.json
                                              #    (all clips, or only the named ones; anim.json is merged)

1. Locked camera. The idle and win sources have a slow Gemini camera push-in/out (up to ~1.2x) that made
   the cam feel "zoomy" and made every idle<->talk<->win crossfade ghost (two framings blended). Each frame
   is matched to one reference framing (SIFT + RANSAC, scale + shift only, smoothed over time so Hubie's own
   head and body motion stay) and re-cropped from the 1280x720 originals, so all three clips share one fixed
   framing and any frame can blend into any other.
2. anim.json for the stage (hubie247/clips.py):
   - talk: nose position + mouth openness per frame (the mouth is re-chosen from the voice level live)
   (jump points, calm points and transition matches are cheap: the stage finds them at start-up)
"""
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CLIPS = ROOT / "assets" / "clips"
SRC = CLIPS / "src"
FPS = 24
OW, OH = 640, 360          # stored clip size (the stage scales to the cam box)
ZOOM = 1.22                # framing: crop factor vs the talk clip's framing (the most zoomed idle frame is ~1.2x)
DY = -10.0                 # framing: shift up (reference px) so the hat keeps its room

SOURCES = {"idle": SRC / "idle_src.mp4",      # Gemini idle v2 (1280x720)
           "talk": SRC / "talk_src.mp4",      # Gemini talk clip 3, already a seamless 9 s loop (640x360)
           "talk2": SRC / "talk2_src.mp4",    # owner's clip 2026-11-09: talking with paw gestures (640x360)
           "win": SRC / "win_src.mp4",        # Gemini win: coin shower (1280x720)
           "win2": SRC / "win2_src.mp4",      # owner's clip 2026-11-09: paws up, hat adjust, settles (640x360)
           "react": SRC / "react_src.mp4"}    # owner's clip 2026-11-09, 20 s: a win, calm, then a deadpan (cut below)
CUTS = {"win3": ("react", 0, 160),           # 0-6.7 s: paws up, hat adjust, settles
        "loss2": ("react", 288, 432)}        # 12-18 s: flat, unimpressed look, back to a smile (push-in removed)
TALKS = ("talk", "talk2")


def decode(path, w, h, fps=FPS):
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-an", "-vf", f"fps={fps},scale={w}:{h}:flags=bicubic",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    n = len(raw) // (w * h * 3)
    return np.frombuffer(raw[: n * w * h * 3], np.uint8).reshape(n, h, w, 3)


def gauss(x, sigma):
    r = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    return np.convolve(np.pad(x, r, mode="edge"), k, mode="valid")


def medfilt(x, n):
    r = n // 2
    p = np.pad(x, r, mode="edge")
    return np.array([np.median(p[i:i + n]) for i in range(len(x))])


def track(frames, ref):
    """Per frame: scale s and where the reference centre lands (cx, cy), in 640x360 reference px."""
    sift = cv2.SIFT_create(1500)
    rg = cv2.cvtColor(ref, cv2.COLOR_RGB2GRAY)
    mask = np.zeros(rg.shape, np.uint8)        # match on Hubie (head + torso), not the room: the room and
    cv2.ellipse(mask, (OW // 2, 190), (115, 175), 0, 0, 360, 255, -1)   # Hubie sit at a different depth
    kr, dr = sift.detectAndCompute(rg, mask)
    bm = cv2.BFMatcher()
    out = []
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_RGB2GRAY)
        k, d = sift.detectAndCompute(g, None)
        ms = [a for a, b in bm.knnMatch(dr, d, k=2) if a.distance < 0.75 * b.distance]
        src = np.float32([kr[m.queryIdx].pt for m in ms])
        dst = np.float32([k[m.trainIdx].pt for m in ms])
        A, _ = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=2.0)
        s = float(np.hypot(A[0, 0], A[1, 0]))
        c = A @ np.array([OW / 2, OH / 2, 1.0])
        out.append((s, c[0], c[1]))
    r = np.array(out)
    # the camera move is slow: median (drops RANSAC outliers) + gaussian (keeps his own motion in the clip)
    return [gauss(medfilt(r[:, i], 9), 6) for i in range(3)]


def render(src, T, size):
    """src frame, T: 3x3 canvas (reference px) -> source (reference px); canvas rendered at 2x then area-downsized."""
    k = src.shape[1] / OW
    M = (np.diag([k, k, 1.0]) @ T @ np.diag([0.5, 0.5, 1.0]))[:2].astype(np.float32)
    w = cv2.warpAffine(src, M, (size[0] * 2, size[1] * 2), flags=cv2.WARP_INVERSE_MAP | cv2.INTER_CUBIC,
                       borderMode=cv2.BORDER_REPLICATE)
    return cv2.resize(w, size, interpolation=cv2.INTER_AREA)


def refine(frames, template):
    """Second pass on the stabilised frames: ECC on Hubie's head + torso against the template, as scale + shift
    about the torso centre, heavily smoothed (only the slow residual drift is taken out, not his gestures)."""
    C = np.array([OW / 2, 170.0])
    m = np.zeros((OH, OW), np.uint8)
    cv2.ellipse(m, (int(C[0]), int(C[1])), (110, 160), 0, 0, 360, 1, -1)
    g = lambda f: cv2.GaussianBlur(cv2.cvtColor(f, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255, (0, 0), 1.2)
    ref = g(template)
    r = []
    for f in frames:
        w = np.eye(2, 3, dtype=np.float32)
        try:
            _, w = cv2.findTransformECC(ref, g(f), w, cv2.MOTION_AFFINE,
                                        (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6), m, 1)
            s = float(np.sqrt(abs(np.linalg.det(w[:, :2]))))
            c = w @ np.array([C[0], C[1], 1.0])
            ok = abs(s - 1) < 0.08 and abs(c[0] - C[0]) < 15 and abs(c[1] - C[1]) < 15
        except cv2.error:
            ok = False
        r.append((s, c[0], c[1]) if ok else (np.nan, np.nan, np.nan))
    r = np.array(r)
    for j in range(3):                                   # fill ECC failures, then smooth
        bad = np.isnan(r[:, j])
        if bad.all():
            r[:, j] = (1.0, C[0], C[1])[j]
        elif bad.any():
            r[bad, j] = np.interp(np.flatnonzero(bad), np.flatnonzero(~bad), r[~bad, j])
        r[:, j] = gauss(medfilt(r[:, j], 9), 10)
    return [np.array([[s, 0, cx - s * C[0]], [0, s, cy - s * C[1]], [0, 0, 1.0]]) for s, cx, cy in r]


def stabilise(name, ref, template=None):
    """-> (frames at OWxOH, pass-1 frame 0). Pass 1: SIFT camera track. Pass 2: ECC residual vs template."""
    big = decode(SOURCES[name], 1280, 720)
    small = np.stack([cv2.resize(f, (OW, OH), interpolation=cv2.INTER_AREA) for f in big])
    s, cx, cy = track(small, ref)
    T = []
    for i in range(len(big)):
        a = s[i] / ZOOM            # canvas -> source:  x = cx + a*(u - 320),  y = cy + s*DY + a*(v - 180)
        T.append(np.array([[a, 0, cx[i] - a * OW / 2], [0, a, cy[i] + s[i] * DY - a * OH / 2], [0, 0, 1.0]]))
    first = np.stack([render(f, t, (OW, OH)) for f, t in zip(small, T)])
    if template is None:
        template = first[0]
    T = [t @ w for t, w in zip(T, refine(first, template))]
    worst = 0.0
    for t in T:
        corners = t @ np.array([[0, OW, 0, OW], [0, 0, OH, OH], [1, 1, 1, 1.0]])
        worst = min(worst, corners[0].min(), corners[1].min(), OW - corners[0].max(), OH - corners[1].max())
    out = np.stack([render(f, t, (OW, OH)) for f, t in zip(big, T)])
    print(f"{name}: {len(out)} frames, camera scale {s.min():.3f}..{s.max():.3f}, worst edge {worst:.1f} ref px "
          f"(negative = replicated border)", flush=True)
    return out, template


def encode(frames, path):
    h, w = frames.shape[1:3]
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                          "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "14",
                          "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)], stdin=subprocess.PIPE)
    p.stdin.write(frames.tobytes())
    p.stdin.close()
    p.wait()


def nose_template(talk0):
    """Nose patch from talk frame 0 (it had the nose at (318, 138) before the crop); used for every clip."""
    nx0 = OW / 2 + ZOOM * (318 - OW / 2)
    ny0 = OH / 2 + ZOOM * (138 - OH / 2 - DY)
    tw, th = int(20 * ZOOM), int(14 * ZOOM)
    x0, y0 = int(nx0 - tw), int(ny0 - th)
    return cv2.cvtColor(talk0, cv2.COLOR_RGB2GRAY)[y0:y0 + 2 * th, x0:x0 + 2 * tw], x0, y0


def mouth_track(frames, nose):
    """Nose position (template match, sub-pixel) and mouth openness (dark + pink + teeth share of the mouth box)."""
    tpl, x0, y0 = nose
    th, tw = tpl.shape[0] // 2, tpl.shape[1] // 2
    out = []
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_RGB2GRAY)
        rx, ry = x0 - 60, y0 - 50
        r = cv2.matchTemplate(g[ry:ry + 2 * th + 100, rx:rx + 2 * tw + 120], tpl, cv2.TM_CCOEFF_NORMED)
        _, mv, _, (mx, my) = cv2.minMaxLoc(r)
        dx = dy = 0.0                                   # parabola fit for sub-pixel
        if 0 < mx < r.shape[1] - 1:
            a, b, c = r[my, mx - 1], r[my, mx], r[my, mx + 1]
            dx = 0.5 * (a - c) / (a - 2 * b + c + 1e-9)
        if 0 < my < r.shape[0] - 1:
            a, b, c = r[my - 1, mx], r[my, mx], r[my + 1, mx]
            dy = 0.5 * (a - c) / (a - 2 * b + c + 1e-9)
        nx, ny = rx + mx + dx + tw, ry + my + dy + th
        bx, by = int(round(nx)), int(round(ny))
        box = f[by + int(5 * ZOOM):by + int(36 * ZOOM), bx - int(30 * ZOOM):bx + int(30 * ZOOM)]
        hsv = cv2.cvtColor(box, cv2.COLOR_RGB2HSV).astype(int)
        h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        dark = v < 85
        pink = ((h < 6) | (h > 165)) & (s > 70) & (v > 70)
        white = (s < 60) & (v > 170)
        out.append([round(nx / OW, 5), round(ny / OH, 5), float((dark | pink | white).mean()), round(float(mv), 3)])
    o = np.array([m[2] for m in out])
    lo, hi = np.percentile(o, 10), np.percentile(o, 97)
    for m in out:
        m[2] = round(float(np.clip((m[2] - lo) / (hi - lo), 0, 1)), 3)
    return out


def main():
    ref = decode(SOURCES["talk"], OW, OH)[0]
    names = sys.argv[1:] or [n for n in SOURCES if n not in {c[0] for c in CUTS.values()}] + list(CUTS)
    meta_p = CLIPS / "anim.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() and sys.argv[1:] else {"clips": {}}
    meta.update({"fps": FPS, "size": [OW, OH], "zoom": ZOOM, "dy": DY})
    # talk first: its first stabilised frame is everyone's alignment template (and the nose template)
    fr, template = stabilise("talk", ref, None)
    nose = nose_template(fr[0])
    whole = {}
    for name in ["talk"] + [n for n in names if n != "talk"]:
        if name in CUTS:
            src, a, b = CUTS[name]
            if src not in whole:
                whole[src], _ = stabilise(src, ref, template)
            fr = whole[src][a:b]
        elif name != "talk":
            fr, template = stabilise(name, ref, template)
        if name not in names:
            continue
        path = CLIPS / f"hubie_{name}.mp4"
        encode(fr, path)
        fr = decode(path, OW, OH)            # analyse what the stage will actually decode
        m = {"file": f"assets/clips/hubie_{name}.mp4", "frames": len(fr)}
        if name in TALKS:
            m["mouth"] = mouth_track(fr, nose)
            print(name, "mouth: nose match >= %.2f" % min(x[3] for x in m["mouth"]), flush=True)
        meta["clips"][name] = m
        print(name, "->", path, flush=True)
    meta_p.write_text(json.dumps(meta, separators=(",", ":")))
    print("anim.json written")


if __name__ == "__main__":
    sys.exit(main())
