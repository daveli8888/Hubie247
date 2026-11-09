"""Third-party slot on Housebets in its own FUN mode ("Try fun mode"), never real mode.

Status (2026-11-09): fun mode itself works through the proxy (Hacksaw Le Bandit loads with mode=demo /
token=demo), but the remote Chrome runs without WebGL (--disable-gpu, no SwiftShader), so Hacksaw shows
"device not compatible". Pragmatic titles are region-locked from the Canada proxy. This adapter is wired in but
NOT verified end to end; it needs the remote browser restarted with software WebGL first. Until then the
runner finds it not ready and skips it in the rotation.

Safety before EVERY spin (one page script, then the key press):
  - the page's Real/Fun switch has "fun" checked and "real" unchecked
  - the game iframe's URL carries the provider's demo marker (Hacksaw: mode=demo)
  - no "Real Play" launch screen is up
Any failure raises Unsafe. The only page buttons this adapter ever presses are "Try fun mode" and the "Fun"
side of the Real/Fun switch. The spin itself is the Space key sent to the game frame.
"""
import asyncio
import json
import time

from .base import HBGame, Unsafe

FUN_CHECK = r"""
(markers) => {
  const radios = [...document.querySelectorAll('input[type=radio]')].filter(i => ['real', 'fun'].includes(i.value));
  const fun = radios.find(i => i.value === 'fun'), real = radios.find(i => i.value === 'real');
  const realBtn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Real Play' && b.offsetParent);
  const tryFun = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Try fun mode' && b.offsetParent);
  const frame = [...document.querySelectorAll('iframe')].find(f => (f.title || '').startsWith('game-'));
  const src = frame ? frame.src : '';
  return {fun: !!(fun && fun.checked), real: !!(real && real.checked), launcher: !!(realBtn || tryFun),
          tryFun: !!tryFun, src, demoUrl: markers.some(m => src.includes(m)),
          rect: frame ? (() => { const b = frame.getBoundingClientRect(); return {x:b.left, y:b.top, width:b.width, height:b.height}; })() : null};
}"""


class HousebetsSlot(HBGame):
    key = "slot"
    kind = "slot"
    round_secs = 12.0

    def __init__(self, path="/games/hacksaw-le-bandit", label="Le Bandit (fun mode)",
                 demo_markers=("mode=demo", "token=demo")):
        self.path, self.label, self.demo_markers = path, label, list(demo_markers)
        self.rounds = 0
        self.last_bet = None
        self._hooked = None

    async def _state(self, page):
        return await page.evaluate(FUN_CHECK, self.demo_markers)

    def _hook(self, page):
        """Read spin results from the provider's demo API responses (best effort, provider specific)."""
        if self._hooked is page:
            return
        self._hooked = page

        async def on_resp(resp):
            u = resp.url
            if "rgs-demo" not in u or "/bet" not in u:
                return
            try:
                data = json.loads(await resp.text())
            except Exception:
                return
            self.last_bet = {"ts": time.time(), "data": data}
        page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))

    async def ready(self, page):
        st = await self._state(page)
        if st["tryFun"]:
            await page.get_by_role("button", name="Try fun mode", exact=True).click(timeout=5000)
            return False, "starting fun mode"
        if st["real"] and not st["fun"]:
            # switch the page's own Real/Fun toggle to Fun (never the other way)
            await page.locator("label:has(input[value=fun])").click(timeout=5000)
            return False, "switching to fun mode"
        if not (st["fun"] and st["demoUrl"]):
            return False, "fun mode not confirmed yet"
        try:
            frame = next(f for f in page.frames if any(m in f.url for m in self.demo_markers))
            body = (await frame.evaluate("document.body ? document.body.innerText : ''"))[:400].lower()
            if "not compatible" in body:
                return False, "slot needs WebGL (browser shows 'not compatible')"
        except StopIteration:
            return False, "slot frame still loading"
        except Exception:
            pass
        self._hook(page)
        return True, ""

    async def frame_rect(self, page):
        st = await self._state(page)
        r = st.get("rect")
        if not r:
            return None
        return {"x": r["x"], "y": r["y"], "width": r["width"], "height": r["height"]}

    async def play_round(self, page):
        st = await self._state(page)
        if not st["fun"] or st["real"] or not st["demoUrl"] or st["launcher"]:
            raise Unsafe(f"slot not in fun mode (fun={st['fun']} real={st['real']} demo_url={st['demoUrl']})")
        frame = next((f for f in page.frames if any(m in f.url for m in self.demo_markers)), None)
        if frame is None:
            return {"skipped": "game frame not found"}
        before = self.last_bet
        await frame.locator("body").press("Space", timeout=5000)
        t0 = time.time()
        while time.time() - t0 < 15 and self.last_bet is before:
            await asyncio.sleep(0.3)
        await asyncio.sleep(3)   # let the reels land on screen before Hubie reacts
        self.rounds += 1
        res = {"game": "slot", "round": self.rounds, "fun_mode": True, "win": False, "multiplier": None}
        if self.last_bet is not before:
            d = self.last_bet["data"]
            win = _find_num(d, ("totalWin", "win", "winAmount", "accumulatedRoundWin"))
            bet = _find_num(d, ("betAmount", "bet", "totalBet", "stake"))
            if win is not None and bet:
                res["multiplier"] = round(win / bet, 2)
                res["win"] = win > 0
        return res


def _find_num(obj, keys, depth=0):
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return float(v)
                except ValueError:
                    pass
        for v in obj.values():
            r = _find_num(v, keys, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj[:20]:
            r = _find_num(v, keys, depth + 1)
            if r is not None:
                return r
    return None
