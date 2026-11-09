"""All settings come from the environment (optionally a .env file in the repo root)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0]          # allow trailing comments like .env.example has
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(name, default=None):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def env_int(name, default):
    return int(env(name, default))


def env_float(name, default):
    return float(env(name, default))


# Runtime state lives in RAM (tmpfs); nothing here needs to survive a reboot.
RUN = Path(env("HUBIE_RUN_DIR", "/dev/shm/hubie247"))
RUN.mkdir(parents=True, exist_ok=True)
(RUN / "speech").mkdir(exist_ok=True)

STATE = RUN / "state.json"            # brain -> stage: caption, chat rows, speech, clip events
STAGE_STATUS = RUN / "stage.json"     # stage -> brain/watchdog: heartbeat, frames, speech done
GAME_FRAME = RUN / "game.jpg"         # game -> stage: latest game screenshot
GAME_STATUS = RUN / "game.json"       # game -> watchdog/brain: heartbeat, adapter, page state
GAME_EVENTS = RUN / "game_events.jsonl"  # game -> brain: round results
BRAIN_STATUS = RUN / "brain.json"     # brain -> watchdog: heartbeat
FAKE_CHAT = RUN / "fake_chat.jsonl"   # tools/fake_chat.py -> brain
TURN = RUN / "turn.json"              # brain -> game: turn-taking (reaction done, talking, switch announced)
LOG_DIR = Path(env("HUBIE_LOG_DIR", str(ROOT / "logs")))
LOG_DIR.mkdir(exist_ok=True)

W, H, FPS = 1280, 720, env_int("HUBIE_FPS", 30)
AUDIO_RATE = 48000
