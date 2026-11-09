"""Game process: plays a game and feeds the stage.

HUBIE_GAME=housebets (default): the REAL Housebets site, zero-amount bets, in the owner's logged-in remote
Chrome over CDP, rotating Dice <-> Plinko; see hubie247/housebets.py. Falls back to a drawn placeholder dice
while the site is unavailable (Cloudflare check, logged out, page down) and flags RUN/needs_owner.txt.

HUBIE_GAME=local: the old stand-in below, one headless Chromium (Playwright):

  - screenshots the page to game.jpg ~HUBIE_GAME_FPS times a second (atomic replace)
  - plays a round every HUBIE_ROUND_SECS and appends the result to game_events.jsonl
  - heartbeat in game.json

  local     games/local_dice.html, a clearly labelled stand-in (works offline)
Chromium is recycled every HUBIE_GAME_RECYCLE_MIN minutes to keep memory flat on a 24/7 run.
"""
import asyncio
import os
import time

from playwright.async_api import async_playwright

from . import bus
from .config import GAME_EVENTS, GAME_FRAME, GAME_STATUS, ROOT, env, env_float


class LocalDice:
    name = "local"
    label = "Dice (placeholder)"

    async def open(self, page):
        await page.goto((ROOT / "games" / "local_dice.html").as_uri())
        return True

    async def play_round(self, page):
        return await page.evaluate("window.roll()")


async def run():
    fps = env_float("HUBIE_GAME_FPS", 4)
    round_secs = env_float("HUBIE_ROUND_SECS", 9)
    recycle = env_float("HUBIE_GAME_RECYCLE_MIN", 120) * 60
    async with async_playwright() as p:
        while True:
            browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--mute-audio"])
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            adapter, note = LocalDice(), ""
            await adapter.open(page)
            bus.append_jsonl(GAME_EVENTS, {"type": "game_state", "state": "ready", "adapter": adapter.name,
                                           "label": adapter.label})
            started, last_round, rounds, pending = time.time(), time.time(), 0, None
            tmp = f"{GAME_FRAME}.tmp.jpg"
            try:
                while time.time() - started < recycle:
                    t = time.time()
                    await page.screenshot(path=tmp, type="jpeg", quality=78)
                    os.replace(tmp, GAME_FRAME)
                    # rounds run alongside the screenshot loop so the roll animation is captured
                    if pending is None and t - last_round >= round_secs:
                        last_round = t
                        pending = asyncio.create_task(adapter.play_round(page))
                    if pending is not None and pending.done():
                        res, pending = pending.result(), None
                        if res:
                            rounds += 1
                            bus.append_jsonl(GAME_EVENTS, {"type": "round", "adapter": adapter.name, **res})
                    bus.write_json(GAME_STATUS, {"ts": time.time(), "pid": os.getpid(), "adapter": adapter.name,
                                                 "label": adapter.label, "rounds": rounds, "note": note})
                    await asyncio.sleep(max(0.0, 1.0 / fps - (time.time() - t)))
            finally:
                await browser.close()


def main():
    if env("HUBIE_GAME", "housebets").startswith("housebets"):
        from .housebets import main as hb_main
        return hb_main()
    asyncio.run(run())


if __name__ == "__main__":
    main()
