"""Housebets Dice (House original) with BET AMOUNT EXACTLY 0.

House originals have no separate fun mode; a logged-in account can place zero-amount bets, which play the real
game (real provably-fair roll, real animation) with nothing at stake. Every click is preceded, in the same
page script, by a check that every Bet Amount field reads zero, Profit on Win reads zero, the Manual tab is on
and the button is the plain "Bet" button. Any failure raises Unsafe and no click happens.
"""
import asyncio
import itertools
import time

from .base import HBGame, Unsafe

HOST = "[data-testid=housegames-direct-dice-element]"

CHECK_AND_BET = r"""
(g) => {
  const r = g.shadowRoot; if (!r) return {ok:false, unsafe:false, why:'game not loaded'};
  const zero = v => !/[1-9]/.test(String(v ?? ''));
  const vis = e => !!e && e.offsetParent !== null;
  const tab = r.querySelector('.hd-p-tab--on');
  if (!tab || tab.textContent.trim() !== 'Manual')
    return {ok:false, unsafe:true, why:'not in Manual mode (' + (tab ? tab.textContent.trim() : 'none') + ')'};
  const amts = [...r.querySelectorAll('.hd-p-amount input')];
  if (!amts.length) return {ok:false, unsafe:true, why:'bet amount field not found'};
  const shown = amts.filter(vis);
  if (shown.length !== 1) return {ok:false, unsafe:true, why:'expected one visible bet field, found ' + shown.length};
  const bad = amts.find(i => !zero(i.value));
  if (bad) return {ok:false, unsafe:true, why:'bet amount is not zero: ' + bad.value};
  const profit = [...r.querySelectorAll('.hd-p-profit input')].filter(vis)[0];
  if (profit && !zero(profit.value)) return {ok:false, unsafe:true, why:'profit on win is not zero: ' + profit.value};
  const btn = [...r.querySelectorAll('button.hd-p-betbtn')].filter(vis)[0];
  if (!btn) return {ok:false, unsafe:false, why:'bet button not found'};
  if (btn.disabled || btn.getAttribute('aria-busy') === 'true') return {ok:false, unsafe:false, why:'bet button busy'};
  const label = (btn.textContent || '').trim();
  if (label !== 'Bet') return {ok:false, unsafe:false, why:'bet button says "' + label + '"'};
  const q = s => r.querySelector(s);
  const info = {amount: shown[0].value || '0', condition: (q('#hd-g-lbl-target')?.textContent || '').trim(),
                target: q('#hd-g-in-target')?.value, mult: q('#hd-g-in-mult')?.value,
                chance: q('#hd-g-in-chance')?.value};
  for (const c of r.querySelectorAll('.hd-g-chip')) c.__hubieSeen = 1;
  btn.click();
  return {ok:true, info};
}"""

POLL = r"""
(g) => {
  const r = g.shadowRoot; if (!r) return null;
  const fresh = [...r.querySelectorAll('.hd-g-chip')].filter(c => !c.__hubieSeen);
  return fresh.map(c => ({aria: c.getAttribute('aria-label') || '', cls: c.className, text: c.textContent.trim()}));
}"""

READY = r"""
(g) => {
  const r = g.shadowRoot; if (!r) return 'no game root';
  if (!r.querySelector('button.hd-p-betbtn')) return 'bet button missing';
  if (!r.querySelector('#hd-g-in-target')) return 'target field missing';
  return '';
}"""

RANGE = r"""
(g) => { const r = g.shadowRoot; const on = r.querySelector('button.hd-on[aria-label]');
         return on ? on.getAttribute('aria-label') : ''; }"""

RECT = r"""
() => {
  const s = document.querySelector('[data-testid=housegames-direct-dice-surface]')
         || document.querySelector('[data-testid=housegames-direct-dice]');
  if (!s) return null;
  const b = s.getBoundingClientRect();
  let bottom = b.bottom;
  const fair = document.querySelector('button[aria-label="Provably fair"]');   // footer bar with the logo
  if (fair) { const f = fair.getBoundingClientRect(); if (f.bottom > bottom && f.bottom - bottom < 90) bottom = f.bottom + 10; }
  bottom = Math.min(bottom, innerHeight);
  return {x: Math.max(0, b.left - 10), y: Math.max(0, b.top - 10), width: Math.min(innerWidth, b.right + 10) - Math.max(0, b.left - 10),
          height: bottom - Math.max(0, b.top - 10)};
}"""


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


class HousebetsDice(HBGame):
    key = "dice"
    label = "Housebets Dice"
    path = "/games/housegames-dice"
    kind = "dice"

    def __init__(self, targets=None, change_every=5):
        # (condition, target) pairs; changed every few rounds so the commentary has something to chew on
        self.targets = itertools.cycle(targets or [("over", 50.5), ("over", 66), ("under", 49.5), ("over", 75),
                                                   ("under", 30), ("over", 50.5), ("over", 90), ("under", 66)])
        self.change_every = change_every
        self.rounds = 0

    def host(self, page):
        return page.locator(HOST)

    async def ready(self, page):
        g = self.host(page)
        if not await g.count():
            return False, "dice game element not on the page"
        why = await g.evaluate(READY)
        return (not why), why

    async def frame_rect(self, page):
        return await page.evaluate(RECT)

    async def prepare(self, page):
        g = self.host(page)
        rng = await g.evaluate(RANGE)
        if rng not in ("Over", "Under"):
            await g.locator("button[aria-label='Over']").click(timeout=5000)
        await self.set_target(page, *next(self.targets))

    async def set_target(self, page, cond, value):
        """Range type Over/Under + target number. Game controls only; the bet field is never touched."""
        g = self.host(page)
        lbl = (await g.locator("#hd-g-lbl-target").text_content(timeout=5000) or "").strip().lower()
        if not lbl.endswith(cond):
            await g.locator("button.hd-g-switch").click(timeout=5000)
            await asyncio.sleep(0.3)
        t = g.locator("#hd-g-in-target")
        await t.fill(f"{value:.2f}", timeout=5000)
        await t.evaluate("e => e.blur()")
        await asyncio.sleep(0.4)

    async def play_round(self, page):
        g = self.host(page)
        if self.rounds and self.rounds % self.change_every == 0:
            await self.set_target(page, *next(self.targets))
        res = await g.evaluate(CHECK_AND_BET)
        if not res.get("ok"):
            if res.get("unsafe"):
                raise Unsafe(res.get("why"))
            return {"skipped": res.get("why")}
        info = res["info"]
        t0 = time.time()
        chip = None
        while time.time() - t0 < 12:
            await asyncio.sleep(0.25)
            fresh = await g.evaluate(POLL)
            if fresh:
                chip = fresh[0]
                break
        if not chip:
            return {"skipped": "no result within 12 s", "bet_amount": info.get("amount")}
        self.rounds += 1
        aria = chip["aria"]
        win = aria.lower().startswith("win") or "--win" in chip["cls"]
        roll = _num(chip["text"])
        cond = "under" if "under" in (info.get("condition") or "").lower() else "over"
        return {"game": "dice", "roll": roll, "target": _num(info.get("target")), "condition": cond,
                "multiplier": _num(info.get("mult")), "chance": _num(info.get("chance")), "win": bool(win),
                "bet_amount": info.get("amount"), "result_text": aria, "round": self.rounds}
