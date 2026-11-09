"""Housebets Mines (House original "mines2") with BET AMOUNT EXACTLY 0.

Same safety model as Dice/Plinko: every click that acts on a round (Bet, each tile reveal, Cash out) is one page
script that first checks the Manual tab is on and every Bet Amount field reads zero, then clicks. Any failed check
raises Unsafe and nothing is clicked. The server confirms the stake ("stake":0) in its round replies.

A round: Bet -> reveal 1-3 random tiles, about a second apart so viewers can follow -> Cash out if every tile was
a gem (win, at the board's multiplier), or the round ends on a mine (loss). A round left open (tab reloaded
mid-round, runner timeout) is finished on the next call, never left hanging.

Page map (inside <mines2-game data-testid=housegames-direct-mines2-element>, open shadow root):
  .hl-p-tab--on           Manual / Auto tab           .hl-p-amount input   Bet Amount (shared by both tabs)
  #hl-p-mines             mines count <select> 1-24   .hl-p-qt[data-grid]  grid size 25/36/49/64
  button.hl-p-betbtn      "Bet"; during a round "Cash out <amount>" (class --cashout, disabled until a gem);
                          class --busy while the bet request is in flight
  .hl-g-board             --pick before/after a round, --live during one
  .hl-g-tile[data-idx]    pick mode: --idle (a click there only pre-selects, --sel); live: plain .hl-g-tile,
                          then --pending, then --gem / a mine; after the round all tiles show, mines as --dim-mine
  .hl-g-foot              "Total profit | 0.00 | 1.29x | Next 1.48x" (current multiplier, then the next one)
"""
import asyncio
import random
import re
import time

from .base import HBGame, Unsafe

HOST = "[data-testid=housegames-direct-mines2-element]"

# shared prelude: root, zero check, Manual tab, the one visible main button. Returns early with {ok:false,...}
_GUARD = r"""
  const r = g.shadowRoot; if (!r) return {ok:false, unsafe:false, why:'game not loaded'};
  const zero = v => !/[1-9]/.test(String(v ?? ''));
  const vis = e => !!e && e.offsetParent !== null;
  const tab = r.querySelector('.hl-p-tab--on');
  if (!tab || tab.textContent.trim() !== 'Manual')
    return {ok:false, unsafe:true, why:'not in Manual mode (' + (tab ? tab.textContent.trim() : 'none') + ')'};
  const amts = [...r.querySelectorAll('.hl-p-amount input')];
  if (!amts.length) return {ok:false, unsafe:true, why:'bet amount field not found'};
  const shown = amts.filter(vis);
  if (shown.length !== 1) return {ok:false, unsafe:true, why:'expected one visible bet field, found ' + shown.length};
  const bad = amts.find(i => !zero(i.value));
  if (bad) return {ok:false, unsafe:true, why:'bet amount is not zero: ' + bad.value};
  const btn = [...r.querySelectorAll('button.hl-p-betbtn')].filter(vis)[0];
  if (!btn) return {ok:false, unsafe:false, why:'bet button not found'};
  const label = (btn.textContent || '').trim().replace(/\s+/g, ' ');
  const board = r.querySelector('.hl-g-board');
  const live = !!board && board.classList.contains('hl-g-board--live');
"""

CHECK_AND_BET = r"""
(g) => {""" + _GUARD + r"""
  if (live) return {ok:false, unsafe:false, why:'a round is already open'};
  if (btn.disabled || btn.getAttribute('aria-busy') === 'true' || btn.classList.contains('hl-p-betbtn--busy'))
    return {ok:false, unsafe:false, why:'bet button busy'};
  if (label !== 'Bet') return {ok:false, unsafe:false, why:'bet button says "' + label + '"'};
  const info = {amount: shown[0].value || '0', mines: r.querySelector('#hl-p-mines')?.value,
                grid: r.querySelector('.hl-p-qt--on')?.dataset.grid};
  btn.click();
  return {ok:true, info};
}"""

CHECK_AND_REVEAL = r"""
(g, idx) => {""" + _GUARD + r"""
  if (!live) return {ok:false, unsafe:false, why:'no open round'};
  if (!/^Cash out/i.test(label)) return {ok:false, unsafe:false, why:'button says "' + label + '"'};
  const t = r.querySelector('.hl-g-tile[data-idx="' + idx + '"]');
  if (!t) return {ok:false, unsafe:false, why:'tile ' + idx + ' not found'};
  if (t.disabled || t.className.trim() !== 'hl-g-tile') return {ok:false, unsafe:false, why:'tile ' + idx + ' not pickable'};
  t.click();
  return {ok:true, amount: shown[0].value || '0'};
}"""

CHECK_AND_CASHOUT = r"""
(g) => {""" + _GUARD + r"""
  if (!live) return {ok:false, unsafe:false, why:'no open round'};
  if (!/^Cash out/i.test(label)) return {ok:false, unsafe:false, why:'button says "' + label + '"'};
  if (btn.disabled) return {ok:false, unsafe:false, why:'cash out not available yet'};
  btn.click();
  return {ok:true, amount: shown[0].value || '0'};
}"""

STATE = r"""
(g) => {
  const r = g.shadowRoot; if (!r) return null;
  const vis = e => !!e && e.offsetParent !== null;
  const btn = [...r.querySelectorAll('button.hl-p-betbtn')].filter(vis)[0];
  const board = r.querySelector('.hl-g-board');
  return {live: !!board && board.classList.contains('hl-g-board--live'),
          label: btn ? (btn.textContent || '').trim().replace(/\s+/g, ' ') : '',
          disabled: !!btn && btn.disabled,
          busy: !!btn && (btn.classList.contains('hl-p-betbtn--busy') || btn.getAttribute('aria-busy') === 'true'),
          tiles: [...r.querySelectorAll(".hl-g-tile")].map(t => [...t.classList].filter(c => c !== "hl-g-tile").join(" ")),
          foot: (r.querySelector('.hl-g-foot')?.innerText || '').replace(/\s+/g, ' ').trim(),
          mines: r.querySelector('#hl-p-mines')?.value, grid: r.querySelector('.hl-p-qt--on')?.dataset.grid};
}"""

READY = r"""
(g) => {
  const r = g.shadowRoot; if (!r) return 'no game root';
  if (!r.querySelector('button.hl-p-betbtn')) return 'bet button missing';
  if (!r.querySelector('#hl-p-mines')) return 'mines selector missing';
  if (r.querySelectorAll('.hl-g-tile').length < 25) return 'tile grid missing';
  return '';
}"""

RECT = r"""
() => {
  const s = document.querySelector('[data-testid=housegames-direct-mines2-surface]')
         || document.querySelector('[data-testid=housegames-direct-mines2]');
  if (!s) return null;
  const b = s.getBoundingClientRect();
  const x = Math.max(0, b.left - 10), y = Math.max(0, b.top - 10);
  const bottom = Math.min(b.bottom + 10, innerHeight);
  return {x, y, width: Math.min(innerWidth, b.right + 10) - x, height: bottom - y};
}"""


def tile_state(cls):
    """'' (hidden), 'pending', 'gem' or 'mine' for a tile's class list (without the base class)."""
    c = cls or ""
    if "--pending" in c:
        return "pending"
    if "--gem" in c:
        return "gem"
    if re.search(r"mine|bomb|boom|--hit|--lose|--loss", c) and "--dim-mine" not in c:
        return "mine"
    return ""


def current_mult(foot):
    """Board multiplier from the footer: 'Total profit | 0.00 | 1.29x | Next 1.48x' -> 1.29 (None before a gem)."""
    head = (foot or "").split("Next")[0]
    m = re.findall(r"(\d+(?:\.\d+)?)\s*[x×]", head)
    return float(m[-1]) if m else None


class HousebetsMines(HBGame):
    key = "mines"
    label = "Housebets Mines"
    path = "/games/housegames-mines"
    kind = "mines"
    round_secs = 10.0
    budget = 20.0           # the runner cancels play_round after 25 s; finish well inside that

    def __init__(self, mines=3, picks=(1, 2, 2, 3, 3), pause=1.0):
        self.mines = int(mines)
        self.picks = picks
        self.pause = pause
        self.rounds = 0

    def host(self, page):
        return page.locator(HOST)

    async def ready(self, page):
        g = self.host(page)
        if not await g.count():
            return False, "mines game element not on the page"
        why = await g.evaluate(READY)
        return (not why), why

    async def frame_rect(self, page):
        return await page.evaluate(RECT)

    async def prepare(self, page):
        """3 mines on the 5x5 grid. Game controls only; the bet field is never touched. Skipped mid-round."""
        g = self.host(page)
        st = await g.evaluate(STATE)
        if not st or st["live"]:
            return
        if st.get("grid") != "25":
            await g.locator(".hl-p-qt[data-grid='25']").click(timeout=5000)
            await asyncio.sleep(0.3)
        if st.get("mines") != str(self.mines):
            await g.locator("#hl-p-mines").select_option(str(self.mines), timeout=5000)
            await asyncio.sleep(0.3)

    # ---- helpers ----
    async def _guarded(self, g, js, *arg):
        res = await (g.evaluate(js, arg[0]) if arg else g.evaluate(js))
        if not res.get("ok") and res.get("unsafe"):
            raise Unsafe(res.get("why"))
        return res

    async def _wait(self, g, cond, secs):
        t0 = time.time()
        st = await g.evaluate(STATE)
        while not cond(st) and time.time() - t0 < secs:
            await asyncio.sleep(0.25)
            st = await g.evaluate(STATE)
        return st, cond(st)

    async def _reveal(self, g, st):
        """Reveal one random hidden tile; returns (outcome 'gem'/'mine'/None, idx, state, why)."""
        hidden = [i for i, c in enumerate(st["tiles"]) if c == ""]
        if not hidden:
            return None, None, st, "no hidden tile left"
        idx = random.choice(hidden)
        res = await self._guarded(g, CHECK_AND_REVEAL, idx)
        if not res.get("ok"):
            return None, idx, st, res.get("why")

        def done(s):
            return s and (not s["live"] or tile_state(s["tiles"][idx]) in ("gem", "mine"))
        st, ok = await self._wait(g, done, 8)
        if not ok:
            return None, idx, st, "tile did not open within 8 s"
        if tile_state(st["tiles"][idx]) == "gem":
            return "gem", idx, st, ""
        return "mine", idx, st, ""          # a mine, or the round ended without a gem on that tile

    async def _finish_leftover(self, g, st):
        """A round is already open (not ours to report): end it with zero risk to anything, report a skip."""
        if not st["disabled"]:
            res = await self._guarded(g, CHECK_AND_CASHOUT)
        else:                                  # no gem yet, cash out not offered: one reveal ends or unlocks it
            out, _, st, why = await self._reveal(g, st)
            res = {"ok": out is not None, "why": why}
            if out == "gem":
                res = await self._guarded(g, CHECK_AND_CASHOUT)
        await self._wait(g, lambda s: s and not s["live"], 6)
        return {"skipped": "finished a round left open (" + ("ok" if res.get("ok") else res.get("why", "?")) + ")"}

    # ---- one round ----
    async def play_round(self, page):
        g = self.host(page)
        t0 = time.time()
        st = await g.evaluate(STATE)
        if not st:
            return {"skipped": "game not loaded"}
        if st["live"]:
            return await self._finish_leftover(g, st)
        if st.get("mines") != str(self.mines) or st.get("grid") != "25":
            await self.prepare(page)
        res = await self._guarded(g, CHECK_AND_BET)
        if not res.get("ok"):
            return {"skipped": res.get("why")}
        info = res["info"]
        st, ok = await self._wait(g, lambda s: s and s["live"] and not s["busy"], 10)
        if not ok:
            return {"skipped": "bet not confirmed within 10 s", "bet_amount": info.get("amount")}
        want = random.choice(self.picks)
        gems, hit, opened, why = 0, False, [], ""
        for n in range(want):
            if n and time.time() - t0 > self.budget - 8:
                break                          # running out of time: bank what we have
            if n:
                await asyncio.sleep(self.pause)
            out, idx, st, why = await self._reveal(g, st)
            if out is None:
                break
            opened.append(idx)
            if out == "mine":
                hit = True
                break
            gems += 1
        mult = None
        if not hit and gems:
            await asyncio.sleep(0.8)
            mult = current_mult(st["foot"])
            res = await self._guarded(g, CHECK_AND_CASHOUT)
            if not res.get("ok"):
                return {"skipped": "cash out failed: " + res.get("why", "?"), "bet_amount": info.get("amount")}
            st, ok = await self._wait(g, lambda s: s and not s["live"], 6)
            mult = current_mult(st["foot"]) or mult
        elif not hit:
            # no tile could be opened at all: leave it for the next call to finish, report nothing now
            return {"skipped": "no tile opened (" + why + ")", "bet_amount": info.get("amount")}
        else:
            await self._wait(g, lambda s: s and not s["live"], 4)
        self.rounds += 1
        win = not hit
        if win:
            text = f"Cashed out at {mult}x after {gems} gem{'s' if gems != 1 else ''}"
        else:
            text = f"Hit a mine on pick {len(opened)}" + (f" after {gems} gem{'s' if gems != 1 else ''}" if gems else "")
        return {"game": "mines", "win": win, "multiplier": mult if win else 0.0, "mines": int(info.get("mines") or self.mines),
                "picks": len(opened), "gems": gems, "tiles": opened, "bet_amount": info.get("amount"),
                "result_text": text, "round": self.rounds}
