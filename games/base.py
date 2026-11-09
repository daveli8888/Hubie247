"""Common interface for Housebets game adapters.

An adapter knows ONE game page: where it lives, how to tell the game is loaded, which part of the page to
show on stream, how to verify a safe (zero-amount / fun-mode) bet and how to read a round's result.
Everything else (browser connection, Cloudflare/login checks, cookie banner, popups, picture feed, rotation,
logging, needs_owner flag) is done once, in hubie247/housebets.py.

HARD RULES every adapter follows:
  - never place a bet unless the amount field reads exactly zero (or, for a third-party slot, the game shows
    its fun/demo mode); the check and the click happen in ONE page script, so nothing can change in between
  - if a check fails: raise Unsafe (the runner stops betting, shows the fallback scene, flags the owner)
  - only touch the game's own controls: never Wallet, deposit, withdraw, tip, rewards or settings
"""
import re

ZERO_JS = r"(v) => !/[1-9]/.test(String(v ?? ''))"   # '', '0', '0.00000000' are zero; anything with 1-9 is not


class Unsafe(Exception):
    """A safety check failed: do not bet."""


def is_zero(text):
    """Python twin of the page-side zero check (used for logs/tests)."""
    return not re.search(r"[1-9]", str(text or ""))


class HBGame:
    key = "base"            # used in HUBIE_HB_ROTATION
    label = "Housebets game"
    path = "/"              # relative to https://www.housebets.com
    kind = "dice"           # passed to the brain for win/loss lines
    round_secs = 9.0        # calm pace between rounds

    def url(self, base):
        return base.rstrip("/") + self.path

    def on_page(self, url):
        return self.path.split("?")[0] in (url or "")

    async def ready(self, page):
        """(True, '') when the game is loaded and playable, else (False, reason)."""
        raise NotImplementedError

    async def frame_rect(self, page):
        """Viewport rect {x,y,width,height} (CSS px) of what to show on stream, or None."""
        raise NotImplementedError

    async def prepare(self, page):
        """Called once after the game is ready (pick fun mode, set a starting target, ...)."""

    async def play_round(self, page):
        """Check safety, place ONE bet/spin, wait for and return the result dict, or None if nothing happened.
        Raise Unsafe if a safety check fails."""
        raise NotImplementedError
