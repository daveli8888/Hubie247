"""Zero-bet safety checks for the Housebets adapters.

test_zero_rule runs anywhere. test_page_checks needs a Chrome on HUBIE_HB_CDP (the remote browser): it opens a
throwaway tab with fake game markup (nothing is sent to housebets.com) and asserts that the adapters' page
scripts refuse to click whenever an amount is not zero, and do click when it is.
"""
import asyncio
import os
import urllib.request

import pytest

from games.base import is_zero
from games.housebets_dice import CHECK_AND_BET
from games.housebets_plinko import CHECK_AND_DROP


def test_zero_rule():
    for v in ("", "0", "0.00000000", "0,00", ".0", None):
        assert is_zero(v)
    for v in ("1", "0.00000001", "1e-8", "0.5", "10", "0.1"):
        assert not is_zero(v)


FIXTURE = """<!doctype html><body><div id=h></div><script>
function make(prefix, tabOn, amounts, profit) {
  const host = document.createElement(prefix === 'hd' ? 'dice-game' : 'plinko2-game');
  document.getElementById('h').replaceChildren(host);
  const r = host.attachShadow({mode: 'open'});
  window.clicks = 0;
  r.innerHTML = `<button class="${prefix}-p-tab ${prefix}-p-tab--on">${tabOn}</button>` +
    amounts.map((v, i) => `<div class="${prefix}-p-amount" style="${i ? 'display:none' : ''}"><input value="${v}"></div>`).join('') +
    `<div class="${prefix}-p-profit"><input value="${profit}"></div>` +
    `<button class="${prefix}-p-betbtn" aria-busy="false">Bet</button>` +
    `<label id="hd-g-lbl-target">Roll Over</label><input id="hd-g-in-target" value="50.50">`;
  r.querySelector('button.' + prefix + '-p-betbtn').addEventListener('click', () => window.clicks++);
  return host;
}
</script></body>"""

CASES = [  # tab, amounts (first visible), profit, expect click, expect unsafe
    ("Manual", ["", "", ""], "0.00000000", True, False),
    ("Manual", ["0.00000000", "0", ""], "0", True, False),
    ("Manual", ["0.00000001", "", ""], "0", False, True),
    ("Manual", ["", "5", ""], "0", False, True),          # hidden Auto field not zero -> refuse too
    ("Manual", ["", "", ""], "0.0002", False, True),
    ("Auto", ["", "", ""], "0", False, True),
]


def _cdp():
    url = os.environ.get("HUBIE_HB_CDP", "http://127.0.0.1:9241")
    try:
        urllib.request.urlopen(url + "/json/version", timeout=2)
        return url
    except Exception:
        return None


@pytest.mark.skipif(_cdp() is None, reason="no Chrome on the CDP port")
def test_page_checks():
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            b = await p.chromium.connect_over_cdp(_cdp())
            ctx = b.contexts[0]
            owner_tab = ctx.pages[0]
            pg = await ctx.new_page()
            try:
                await pg.set_content(FIXTURE)
                for prefix, js in (("hd", CHECK_AND_BET), ("hp", CHECK_AND_DROP)):
                    for tab, amts, profit, click, unsafe in CASES:
                        host = await pg.evaluate_handle("([p,t,a,pr]) => make(p,t,a,pr)", [prefix, tab, amts, profit])
                        res = await host.evaluate(js)
                        clicks = await pg.evaluate("window.clicks")
                        assert bool(res["ok"]) == click and clicks == (1 if click else 0), (prefix, amts, res)
                        assert bool(res.get("unsafe")) == unsafe, (prefix, amts, res)
            finally:
                await pg.close()
                await owner_tab.bring_to_front()
    asyncio.run(run())


# ---- Mines: Bet, every tile reveal and Cash out are each one checked page script ----
from games.housebets_mines import CHECK_AND_BET as MINES_BET, CHECK_AND_CASHOUT as MINES_CASHOUT  # noqa: E402
from games.housebets_mines import CHECK_AND_REVEAL as MINES_REVEAL, current_mult, tile_state  # noqa: E402


def test_mines_parsing():
    assert current_mult("Total profit | 0.00 | 1.29x | Next 1.48x") == 1.29
    assert current_mult("Total profit | 0.00 | 1.29x | Clear board") == 1.29
    assert current_mult("Total profit | 0.00 | Next 1.13x") is None
    assert tile_state("") == "" and tile_state("hl-g-tile--pending") == "pending"
    assert tile_state("hl-g-tile--gem") == "gem" and tile_state("hl-g-tile--gem hl-g-tile--idle") == "gem"
    assert tile_state("hl-g-tile--mine") == "mine"
    assert tile_state("hl-g-tile--dim hl-g-tile--dim-mine hl-g-tile--idle") == ""   # unpicked mine shown after


MINES_FIXTURE = """<!doctype html><body><div id=h></div><script>
function mines(tabOn, amounts, live, label, disabled) {
  const host = document.createElement('mines2-game');
  document.getElementById('h').replaceChildren(host);
  const r = host.attachShadow({mode: 'open'});
  window.clicks = 0; window.tileClicks = 0;
  r.innerHTML = `<button class="hl-p-tab hl-p-tab--on">${tabOn}</button>` +
    amounts.map((v, i) => `<div class="hl-input hl-p-amount" style="${i ? 'display:none' : ''}"><input value="${v}"></div>`).join('') +
    `<select id="hl-p-mines"><option value="3" selected>3</option></select>` +
    `<button class="hl-p-qt hl-p-qt--on" data-grid="25">5x5</button>` +
    `<button class="hl-btn-primary hl-p-betbtn${live ? ' hl-p-betbtn--cashout' : ''}" ${disabled ? 'disabled' : ''}>${label}</button>` +
    `<div class="hl-g-board hl-g-board--${live ? 'live' : 'pick'}"><div class="hl-g-grid">` +
    [...Array(25).keys()].map(i => `<button class="hl-g-tile${live ? '' : ' hl-g-tile--idle'}" data-idx="${i}"></button>`).join('') +
    `</div></div>`;
  r.querySelector('button.hl-p-betbtn').addEventListener('click', () => window.clicks++);
  r.querySelectorAll('.hl-g-tile').forEach(t => t.addEventListener('click', () => window.tileClicks++));
  return host;
}
</script></body>"""

MINES_CASES = [  # script, tab, amounts (first visible), live board, button label, disabled, expect click, unsafe
    ("bet", "Manual", ["", ""], False, "Bet", False, True, False),
    ("bet", "Manual", ["0.00000000", "0"], False, "Bet", False, True, False),
    ("bet", "Manual", ["0.00000001", ""], False, "Bet", False, False, True),
    ("bet", "Manual", ["", "5"], False, "Bet", False, False, True),           # hidden field not zero -> refuse
    ("bet", "Auto", ["", ""], False, "Bet", False, False, True),
    ("bet", "Manual", ["", ""], True, "Cash out 0.00000000", True, False, False),   # round open: no new bet
    ("reveal", "Manual", ["", ""], True, "Cash out 0.00000000", True, True, False),
    ("reveal", "Manual", ["1", ""], True, "Cash out 0.00000000", True, False, True),
    ("reveal", "Auto", ["", ""], True, "Cash out 0.00000000", True, False, True),
    ("reveal", "Manual", ["", ""], False, "Bet", False, False, False),        # no round: tiles only pre-select
    ("cashout", "Manual", ["", ""], True, "Cash out 0.00000000", False, True, False),
    ("cashout", "Manual", ["", "0.5"], True, "Cash out 0.00000000", False, False, True),
    ("cashout", "Manual", ["", ""], True, "Cash out 0.00000000", True, False, False),   # no gem yet
    ("cashout", "Manual", ["", ""], False, "Bet", False, False, False),
]


@pytest.mark.skipif(_cdp() is None, reason="no Chrome on the CDP port")
def test_mines_page_checks():
    from playwright.async_api import async_playwright
    scripts = {"bet": MINES_BET, "reveal": MINES_REVEAL, "cashout": MINES_CASHOUT}

    async def run():
        async with async_playwright() as p:
            b = await p.chromium.connect_over_cdp(_cdp())
            pg = await b.contexts[0].new_page()
            try:
                await pg.set_content(MINES_FIXTURE)
                for name, tab, amts, live, label, dis, click, unsafe in MINES_CASES:
                    host = await pg.evaluate_handle("([t,a,l,lb,d]) => mines(t,a,l,lb,d)", [tab, amts, live, label, dis])
                    res = await (host.evaluate(scripts[name], 7) if name == "reveal" else host.evaluate(scripts[name]))
                    n = await pg.evaluate("window.tileClicks" if name == "reveal" else "window.clicks")
                    other = await pg.evaluate("window.clicks" if name == "reveal" else "window.tileClicks")
                    case = (name, tab, amts, live, label, res)
                    assert bool(res["ok"]) == click and n == (1 if click else 0) and other == 0, case
                    assert bool(res.get("unsafe")) == unsafe, case
            finally:
                await pg.close()
    asyncio.run(run())
