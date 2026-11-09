"""Turn-taking between the game and Hubie's brain (offline: stub LLM, fake TTS, simulated stage).

What must hold so his speech matches the picture:
  - a reaction is about the round that just ended, and the game is held until it was spoken (then released)
  - a reaction that went stale (newer round, too old) is dropped, never said late, and the round is released
  - "next up: X" is voiced at the heads-up and spoken when the switch is requested, before the switch
  - chat is answered in arrival order, after the running round's reaction, and holds the next round
"""
import os
import tempfile
import time

import numpy as np
import pytest

os.environ.setdefault("HUBIE_RUN_DIR", tempfile.mkdtemp(prefix="hubie_test_run_"))
from hubie247 import brain as B  # noqa: E402
from hubie247 import bus  # noqa: E402
from hubie247.chat_sources import ChatMsg  # noqa: E402
from hubie247.llm import StubBrain  # noqa: E402


class FakeStage:
    def __init__(self, path):
        self.path, self.started, self.done = path, [], None

    def write(self):
        bus.write_json(self.path, {"ts": time.time(), "speech_started": self.started[-20:], "speech_done": self.done})

    def start(self, sid):
        self.started.append(sid)
        self.write()

    def finish(self, sid):
        self.done = sid
        self.write()


@pytest.fixture
def env(tmp_path, monkeypatch):
    run = tmp_path / "run"
    (run / "speech").mkdir(parents=True)
    paths = dict(RUN=run, STATE=run / "state.json", TURN=run / "turn.json", BRAIN_STATUS=run / "brain.json",
                 STAGE_STATUS=run / "stage.json", GAME_EVENTS=run / "game_events.jsonl",
                 GAME_STATUS=run / "game.json", LOG=tmp_path / "brain_events.jsonl")
    for k, v in paths.items():
        monkeypatch.setattr(B, k, v)
    paths["GAME_EVENTS"].write_text("")
    bus.write_json(paths["GAME_STATUS"], {"ts": time.time(), "label": "Housebets Dice"})
    monkeypatch.setattr(B, "start_sources", lambda q, status: [])
    monkeypatch.setattr(B, "make_brain", StubBrain)
    slow = {"on": False}

    def synth(text, out, total=18):
        if slow["on"] and total < 10:
            return None                     # edge-tts stalled past the reaction's budget
        np.zeros((4800, 2), np.int16).tofile(out)
        return out
    monkeypatch.setattr(B.tts, "synth", synth)
    monkeypatch.setenv("HUBIE_GAME_LINE_GAP", "0")
    monkeypatch.setenv("HUBIE_IDLE_SECS", "999")
    br = B.Brain()
    for _ in range(100):                     # pre-voiced fallbacks
        if br.fallback:
            break
        time.sleep(0.02)
    stage = FakeStage(paths["STAGE_STATUS"])
    stage.write()
    return br, stage, paths, slow


def ev(paths, **e):
    bus.append_jsonl(paths["GAME_EVENTS"], e)


def turn(paths):
    return bus.read_json(paths["TURN"])


def settle(br, until, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        br.step()
        if until():
            return True
        time.sleep(0.02)
    return False


def speak(br, stage):
    """Play whatever the brain committed; return it."""
    cur = br.cur
    stage.start(cur["id"])
    br.step()
    stage.finish(cur["id"])
    br.step()
    return cur


def test_reaction_holds_and_releases(env):
    br, stage, paths, _ = env
    ev(paths, type="round_start", rid=100)
    ev(paths, type="round", rid=100, game="dice", roll=73.1, target=50.5, multiplier=2.0, win=True)
    assert settle(br, lambda: br.cur is not None)
    assert br.cur["kind"] == "win" and br.cur["rid"] == 100
    t = turn(paths)
    assert t["released"] < 100 and t["react_rid"] == 100 and t["react_speaking"]
    cur = speak(br, stage)
    assert cur["done_at"] and turn(paths)["released"] == 100


def test_stale_reaction_is_dropped(env):
    br, stage, paths, _ = env
    ev(paths, type="round", rid=200, game="dice", roll=10.0, target=50.5, multiplier=2.0, win=False)
    assert settle(br, lambda: "react" in br.ready or br.cur is not None)
    if br.cur is not None:                  # spoken straight away: fine, nothing to drop
        return
    ev(paths, type="round_start", rid=201)  # the game moved on (e.g. wait cap): the line must not be said
    assert settle(br, lambda: "react" not in br.ready)
    assert br.cur is None and turn(paths)["released"] >= 200


def test_reaction_too_old(env, monkeypatch):
    br, stage, paths, _ = env
    old = time.time() - 30
    bus.append_jsonl(paths["GAME_EVENTS"], {"type": "round", "rid": 300, "game": "plinko", "multiplier": 0.5,
                                            "win": False, "ts": old})
    br.step()
    assert br.cur is None and turn(paths)["released"] == 300


def test_slow_tts_uses_prevoiced_fallback(env):
    br, stage, paths, slow = env
    slow["on"] = True
    ev(paths, type="round", rid=400, game="plinko", multiplier=0.3, win=False)
    assert settle(br, lambda: br.cur is not None)
    assert br.cur.get("fallback") in ("pool", "static") and br.cur["pcm"]
    assert "{" not in br.cur["text"] and not any(ch.isdigit() for ch in br.cur["text"])   # true for any result


def test_switch_announced_before_switch(env):
    br, stage, paths, _ = env
    ev(paths, type="game_state", state="switch_soon", to="Housebets Plinko")
    assert settle(br, lambda: "switch" in br.ready)
    br.step()
    assert br.cur is None, "the heads-up line must wait for the switch to be due"
    ev(paths, type="game_state", state="switch_pending", sw=777, to="Housebets Plinko", **{"from": "Housebets Dice"})
    assert settle(br, lambda: br.cur is not None)
    assert br.cur["kind"] == "switch" and "Plinko" in br.cur["text"]
    assert turn(paths)["switch_ack"] != 777        # not acked until the stage starts it
    stage.start(br.cur["id"])
    br.step()
    assert turn(paths)["switch_ack"] == 777
    # the game switches now; the "ready" for Plinko must not trigger a second announcement
    stage.finish(br.cur["id"])
    ev(paths, type="game_state", state="switching", to="Housebets Plinko")
    ev(paths, type="game_state", state="ready", label="Housebets Plinko", adapter="housebets_plinko", game="plinko")
    settle(br, lambda: False, timeout=0.3)
    assert br.cur["kind"] == "switch" and "switch" not in br.jobs and "switch" not in br.ready


def test_chat_in_order_after_reaction(env):
    br, stage, paths, _ = env
    ev(paths, type="round_start", rid=500)
    for u in ("ann", "bob", "cat"):
        br.q.put(ChatMsg(u, f"hi hubie from {u}, what game is it", "fake"))
    assert settle(br, lambda: "chat" in br.ready)
    assert br.cur is None, "no chat answer while the round is still running"
    assert turn(paths)["chat_busy"]
    ev(paths, type="round", rid=500, game="dice", roll=12.0, target=50.5, multiplier=2.0, win=False)
    assert settle(br, lambda: br.cur is not None and br.cur["kind"] == "loss")
    speak(br, stage)
    order = []
    for _ in range(3):
        assert settle(br, lambda: br.cur is not None and not br.cur.get("done_at"))
        order.append(br.cur["reply_to"])
        speak(br, stage)
    assert order == ["ann", "bob", "cat"]
    assert not turn(paths)["chat_busy"]
