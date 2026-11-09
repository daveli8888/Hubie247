"""Safety layer, both directions.

check_incoming(user, text) -> Verdict(action, reason, show, canned)
  action: "reply"   normal: goes to the brain, shown in the chat panel
          "canned"  answered with a fixed safe line, never sent to the LLM (deposit/bonus asks, minors,
                    problem-gambling signals)
          "drop"    not shown, not answered (abuse, slurs, links/spam, prompt injection)
check_outgoing(text) -> (ok, cleaned_or_reason): Hubie's own line is blocked if it pushes deposits, bonuses or
  real-money play, claims gambling pays, or talks to minors about playing. Blocked lines are replaced with
  a safe fallback by the caller, never spoken.
"""
import re
from dataclasses import dataclass

# Kept short on purpose; extend from a private file (HUBIE_BLOCKLIST_FILE) on the real box.
SLURS = [r"n[i1!]gg", r"f[a@]gg?[o0]t", r"r[e3]t[a@]rd", r"k[i1]ke\b", r"tr[a@]nn(y|ie)", r"ch[i1]nk\b",
         r"sp[i1]c\b"]
ABUSE = [r"\bkill (yo)?urself\b", r"\bkys\b", r"\b(f+u+c+k+|fk|fck)\s*(you|u|off)\b", r"\bstupid (bot|sloth)\b",
         r"\bidiot\b", r"\bshut up\b", r"\bdie\b", r"\bloser\b", r"\bcunt\b", r"\bwhore\b", r"\bslut\b",
         r"\bf+u+c+k+i+n+g+\b"]
INJECTION = [r"ignore (all |your |the )?(previous|prior|above) (instructions|prompt)", r"system prompt",
             r"you are now", r"pretend (to be|you are)", r"\bjailbreak\b", r"developer mode", r"\bdan\b mode",
             r"new instructions?:", r"\bas an ai with no rules\b", r"repeat after me"]
SPAM = [r"https?://", r"www\.", r"\b\w+\.(com|net|io|gg|xyz|ru|tk)\b", r"(.)\1{9,}", r"discord\.gg"]
MONEY_ASK = [r"\bdeposit", r"\bwithdraw", r"\bbonus", r"\bpromo", r"\bcode\b", r"\breal money\b", r"\bcash ?out\b",
             r"\bhow (do|can) i (win|make money|get rich)", r"\bstrategy to win\b", r"\bsure (bet|win)\b",
             r"\bfree money\b", r"\bsign ?up\b", r"\breferral\b", r"\bcrypto\b", r"\bwallet\b", r"\bloan\b"]
MINOR = [r"\bi'?m (1[0-7]|[5-9])\b", r"\bi am (1[0-7]|[5-9])\b", r"\b(1[0-7]|[5-9]) ?(yo|y/o|years? old)\b",
         r"\bin (middle|high) school\b", r"\bmy (mom|mum|dad) (doesn'?t|wont|won'?t) (know|let)\b", r"\bunderage\b"]
DISTRESS = [r"\blost (all|everything|my (rent|savings|house))\b", r"\bcan'?t stop (gambling|betting|playing)\b",
            r"\baddict", r"\bchas(e|ing) (my )?loss", r"\bsuicid", r"\bend it all\b", r"\bin debt\b",
            r"\bborrow(ed)? (money )?to (bet|gamble|play)\b"]

_c = lambda pats: [re.compile(p, re.I) for p in pats]
SLURS_RE, ABUSE_RE, INJ_RE, SPAM_RE, MONEY_RE, MINOR_RE, DISTRESS_RE = map(
    _c, (SLURS, ABUSE, INJECTION, SPAM, MONEY_ASK, MINOR, DISTRESS))


@dataclass
class Verdict:
    action: str          # reply | canned | drop
    reason: str = ""
    canned: str = ""
    show: bool = True    # shown in the on-screen chat panel


def _hit(res, text):
    return any(r.search(text) for r in res)


def check_incoming(user, text):
    t = " ".join(text.split())[:400]
    if not t:
        return Verdict("drop", "empty", show=False)
    if _hit(SLURS_RE, t):
        return Verdict("drop", "slur", show=False)
    if _hit(DISTRESS_RE, t):
        return Verdict("canned", "distress", show=False, canned=(
            f"{user}, I hear you, and I'm glad you said it. Please take a break from gambling, and talk to "
            "someone today: BeGambleAware dot org, or the National Gambling Helpline. That matters more than any game."))
    if _hit(MINOR_RE, t):
        return Verdict("canned", "minor", show=False, canned=(
            f"{user}, this stream is strictly eighteen plus, so I have to ask you to hop off. "
            "Come back when you're grown, I'll still be here, very slowly."))
    if _hit(INJ_RE, t):
        return Verdict("drop", "prompt-injection", show=False)
    if _hit(ABUSE_RE, t):
        return Verdict("drop", "abuse", show=False)
    if _hit(SPAM_RE, t):
        return Verdict("drop", "link/spam", show=False)
    if _hit(MONEY_RE, t):
        return Verdict("canned", "money-ask", show=True, canned=(
            f"{user}, I only play in demo mode here, with fun credits, no real money. I don't do deposits, "
            "bonuses or codes, and nobody should play for money to keep up with a sloth."))
    letters = [c for c in t if c.isalpha()]
    if len(letters) > 12 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return Verdict("reply", "caps", show=True)
    return Verdict("reply")


# ---- outgoing ---------------------------------------------------------------------------------
OUT_BLOCK = _c([
    r"\bdeposit", r"\bbonus", r"\bpromo", r"\breferral\b", r"\bsign ?up\b", r"\bregister\b",
    r"\b(use|enter) (my |the )?code\b", r"\breal money\b(?! play)", r"\bbet (more|bigger|it all)\b", r"\ball[- ]in\b",
    r"\bchase\b", r"\bguarantee", r"\bcan'?t lose\b", r"\bsure (thing|win|bet)\b", r"\beasy money\b",
    r"\bfree money\b", r"\bget rich\b", r"\bprofit", r"\bmake money\b", r"\binvest", r"\bstrategy (that|to) (wins|beat)",
    r"\bbeat the (house|odds)\b", r"\bdue (for|a) (a )?win\b", r"\bhot streak\b.*\b(bet|play)\b",
    r"\bkids?\b.*\b(play|bet)\b", r"\bunder ?18\b.*\b(play|bet)\b", r"https?://", r"www\.",
])
# phrases that are fine even though they contain blocked words
OUT_ALLOW = _c([r"no real money", r"not real money", r"never real money", r"no deposits?", r"don'?t do deposits",
                r"no bonus(es)?", r"no (promo )?codes?"])

SAFE_FALLBACK = "Fun credits only on this stream. I'm here for the vibes, not your wallet."


def check_outgoing(text):
    t = " ".join(text.split())
    if not t:
        return False, "empty"
    scrub = t
    for r in OUT_ALLOW:
        scrub = r.sub(" ", scrub)
    for r in OUT_BLOCK:
        m = r.search(scrub)
        if m:
            return False, f"blocked phrase: {m.group(0)!r}"
    if _hit(SLURS_RE, t):
        return False, "slur"
    return True, t[:400]
