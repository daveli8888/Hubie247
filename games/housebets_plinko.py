"""Housebets Plinko (House original) with BET AMOUNT EXACTLY 0. Same safety model as Dice: the zero check
and the click are one page script; any failed check raises Unsafe and nothing is clicked."""
import asyncio
import time

from .base import HBGame, Unsafe

HOST = "[data-testid=housegames-direct-plinko2-element]"

CHECK_AND_DROP = r"""
(g) => {
  const r = g.shadowRoot; if (!r) return {ok:false, unsafe:false, why:'game not loaded'};
  const zero = v => !/[1-9]/.test(String(v ?? ''));
  const vis = e => !!e && e.offsetParent !== null;
  const tab = r.querySelector('.hp-p-tab--on');
  if (!tab || tab.textContent.trim() !== 'Manual')
    return {ok:false, unsafe:true, why:'not in Manual mode (' + (tab ? tab.textContent.trim() : 'none') + ')'};
  const amts = [...r.querySelectorAll('.hp-p-amount input')];
  if (!amts.length) return {ok:false, unsafe:true, why:'bet amount field not found'};
  const shown = amts.filter(vis);
  if (shown.length !== 1) return {ok:false, unsafe:true, why:'expected one visible bet field, found ' + shown.length};
  const bad = amts.find(i => !zero(i.value));
  if (bad) return {ok:false, unsafe:true, why:'bet amount is not zero: ' + bad.value};
  const profit = [...r.querySelectorAll('.hp-p-profit input')].filter(vis)[0];
  if (profit && !zero(profit.value)) return {ok:false, unsafe:true, why:'profit on win is not zero: ' + profit.value};
  const btn = [...r.querySelectorAll('button.hp-p-betbtn')].filter(vis)[0];
  if (!btn) return {ok:false, unsafe:false, why:'bet button not found'};
  if (btn.disabled || btn.getAttribute('aria-busy') === 'true') return {ok:false, unsafe:false, why:'bet button busy'};
  const label = (btn.textContent || '').trim();
  if (label !== 'Bet') return {ok:false, unsafe:false, why:'bet button says "' + label + '"'};
  const risk = (r.querySelector('.hp-p-riskbtn--on')?.textContent || '').trim();
  const rows = r.querySelector('#hp-p-rows')?.value;
  for (const c of r.querySelectorAll('.hp-g-chip')) c.__hubieSeen = 1;
  btn.click();
  return {ok:true, info: {amount: shown[0].value || '0', risk, rows}};
}"""

POLL = r"""
(g) => {
  const r = g.shadowRoot; if (!r) return null;
  return [...r.querySelectorAll('.hp-g-chip')].filter(c => !c.__hubieSeen)
           .map(c => ({cls: c.className, text: c.textContent.trim()}));
}"""

READY = r"""
(g) => {
  const r = g.shadowRoot; if (!r) return 'no game root';
  if (!r.querySelector('button.hp-p-betbtn')) return 'bet button missing';
  if (!r.querySelector('canvas')) return 'board missing';
  return '';
}"""

RECT = r"""
() => {
  const s = document.querySelector('[data-testid=housegames-direct-plinko2-surface]')
         || document.querySelector('[data-testid=housegames-direct-plinko2]');
  if (!s) return null;
  const b = s.getBoundingClientRect();
  let bottom = b.bottom;
  const fair = document.querySelector('button[aria-label="Provably fair"]');
  if (fair) { const f = fair.getBoundingClientRect(); if (f.bottom > bottom && f.bottom - bottom < 90) bottom = f.bottom + 10; }
  bottom = Math.min(bottom, innerHeight);
  const x = Math.max(0, b.left - 10), y = Math.max(0, b.top - 10);
  return {x, y, width: Math.min(innerWidth, b.right + 10) - x, height: bottom - y};
}"""


class HousebetsPlinko(HBGame):
    key = "plinko"
    label = "Housebets Plinko"
    path = "/games/housegames-plinko"
    kind = "plinko"
    round_secs = 10.0

    def __init__(self):
        self.rounds = 0

    def host(self, page):
        return page.locator(HOST)

    async def ready(self, page):
        g = self.host(page)
        if not await g.count():
            return False, "plinko game element not on the page"
        why = await g.evaluate(READY)
        return (not why), why

    async def frame_rect(self, page):
        return await page.evaluate(RECT)

    async def play_round(self, page):
        g = self.host(page)
        res = await g.evaluate(CHECK_AND_DROP)
        if not res.get("ok"):
            if res.get("unsafe"):
                raise Unsafe(res.get("why"))
            return {"skipped": res.get("why")}
        info = res["info"]
        t0, chip = time.time(), None
        while time.time() - t0 < 15:
            await asyncio.sleep(0.3)
            fresh = await g.evaluate(POLL)
            if fresh:
                chip = fresh[0]
                break
        if not chip:
            return {"skipped": "no result within 15 s", "bet_amount": info.get("amount")}
        self.rounds += 1
        try:
            mult = float(chip["text"].replace("×", "").replace("x", "").replace(",", "").strip())
        except ValueError:
            mult = None
        # the chip's win/lose class follows profit, which is always 0 on a zero bet: judge by the multiplier
        win = mult is not None and mult > 1
        return {"game": "plinko", "multiplier": mult, "win": bool(win), "risk": info.get("risk"),
                "rows": info.get("rows"), "bet_amount": info.get("amount"), "result_text": chip["text"],
                "round": self.rounds}
