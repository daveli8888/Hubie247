"""Tiny file bus between processes: atomic JSON snapshots + append-only JSONL event files.
Everything is in /dev/shm, so it's RAM-speed and inspectable with cat."""
import json
import os
import time


def write_json(path, obj):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def append_jsonl(path, obj):
    obj.setdefault("ts", time.time())
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


class Tail:
    """Follow a JSONL file from its current end (or start). Survives truncation/rotation."""

    def __init__(self, path, from_start=False):
        self.path = str(path)
        self.pos = 0
        if not from_start:
            try:
                self.pos = os.path.getsize(self.path)
            except OSError:
                self.pos = 0

    def read(self):
        out = []
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return out
        if size < self.pos:
            self.pos = 0
        if size == self.pos:
            return out
        with open(self.path) as f:
            f.seek(self.pos)
            data = f.read()
        # keep a partial last line for next time
        cut = data.rfind("\n")
        if cut < 0:
            return out
        self.pos += len(data[: cut + 1].encode())
        for line in data[: cut + 1].splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out


def age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return 1e9
