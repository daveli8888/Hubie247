"""Watchdog: pm2 restarts a process that crashes; this catches the ones that hang without crashing.

Every 5 s it checks each part's heartbeat file in HUBIE_RUN_DIR:
  stage  stage.json fresh AND ffmpeg's encoded-frame counter still moving (a stalled encoder or
         a dead RTMP connection stops it)                  -> pm2 restart <prefix>stage
  game   game.json fresh (screenshot loop alive)            -> pm2 restart <prefix>game
  brain  brain.json fresh                                   -> pm2 restart <prefix>brain
Every action is logged to logs/watchdog.jsonl with the detected stall time.
"""
import subprocess
import time

from . import bus
from .config import BRAIN_STATUS, GAME_STATUS, LOG_DIR, STAGE_STATUS, env, env_float

PREFIX = env("HUBIE_PM2_PREFIX", "hubie247-")
LOG = LOG_DIR / "watchdog.jsonl"
GRACE = env_float("HUBIE_WD_GRACE", 40)      # after a (re)start, give a part this long to come up
STALL = env_float("HUBIE_WD_STALL", 12)


def restart(part, reason):
    bus.append_jsonl(LOG, {"ev": "restart", "part": part, "reason": reason})
    print(f"restart {part}: {reason}", flush=True)
    subprocess.run(["pm2", "restart", PREFIX + part], capture_output=True, timeout=60)


def main():
    last_restart = {p: time.time() for p in ("stage", "game", "brain")}
    last_frame, frame_moved = None, time.time()
    bus.append_jsonl(LOG, {"ev": "start"})
    while True:
        now = time.time()
        st = bus.read_json(STAGE_STATUS, {}) or {}
        f = st.get("ffmpeg_frame")
        if f != last_frame:
            last_frame, frame_moved = f, now
        checks = {
            "stage": (bus.age(STAGE_STATUS) > STALL and "heartbeat stale")
                     or (now - frame_moved > STALL and f"encoder stalled at frame {f}"),
            "game": bus.age(GAME_STATUS) > 30 and "game heartbeat stale",
            "brain": bus.age(BRAIN_STATUS) > 30 and "brain heartbeat stale",
        }
        for part, problem in checks.items():
            if problem and now - last_restart[part] > GRACE:
                restart(part, problem)
                last_restart[part] = now
                if part == "stage":
                    frame_moved = now
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
