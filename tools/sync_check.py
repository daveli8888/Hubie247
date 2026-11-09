#!/usr/bin/env python3
"""Timing check for a demo recording: does what Hubie says line up with the picture?

  tools/sync_check.py recordings/housebets_sync_<time>.mp4 [run_dir=/dev/shm/hubie247_demo] [out=qa/sync_check.txt]

Reads the run's game_events.jsonl (round_start / round / switch events, wall time), logs/brain_events.jsonl
(what was said for which round / chat) and logs/stage_timeline.jsonl (when each line started and ended in
the recording: video time = audio sample time). Writes a table with, per event, the event time and when the
speech started (both as recording time), plus PASS/FAIL per rule:
  reaction  starts after its result and ends before the next round starts (said while the result is shown)
  switch    the "next up" line starts before the switch card / navigation
  chat      replies start in the order the messages arrived; the bubble is shown from speech start
It also measures the real audio onsets in the mp4 (ffmpeg silencedetect) and matches them to the logged
speech starts, and saves frames around each event to qa/sync_frames/.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def jl(path):
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def onsets(mp4):
    """Speech onsets (s) in the recording's audio track."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(mp4), "-af", "silencedetect=noise=-40dB:d=0.35",
                        "-f", "null", "-"], capture_output=True, text=True)
    return [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", r.stderr)]


def frame(mp4, t, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0, t):.3f}", "-i", str(mp4), "-frames:v", "1",
                    "-q:v", "3", str(out)], check=False)


def main():
    mp4 = Path(sys.argv[1])
    run = Path(sys.argv[2] if len(sys.argv) > 2 else "/dev/shm/hubie247_demo")
    out = Path(sys.argv[3] if len(sys.argv) > 3 else ROOT / "qa" / "sync_check.txt")
    tl = jl(ROOT / "logs" / "stage_timeline.jsonl")
    start = [e for e in tl if e["ev"] == "stage_start"][-1]
    t0 = start["t0_wall"]
    tl = [e for e in tl if e.get("ts", 0) >= t0]
    vt = lambda ts: ts - t0                                         # noqa: E731
    starts = {e["id"]: e for e in tl if e["ev"] == "speech_start"}
    ends = {e["id"]: e for e in tl if e["ev"] == "speech_end"}
    cards = [e for e in tl if e["ev"] == "switch_card"]
    gev = [e for e in jl(run / "game_events.jsonl") if e.get("ts", 0) >= t0 - 1]
    brain = [e for e in jl(ROOT / "logs" / "brain_events.jsonl") if e.get("ts", 0) >= t0 - 30]
    says = [e for e in brain if e.get("ev") == "say"]
    rstart = {e["rid"]: e for e in gev if e.get("type") == "round_start"}
    rounds = [e for e in gev if e.get("type") == "round"]
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(mp4)], capture_output=True, text=True).stdout.strip() or 0)
    aud = onsets(mp4)
    fdir = ROOT / "qa" / "sync_frames"
    fdir.mkdir(parents=True, exist_ok=True)
    for f in fdir.glob("*.jpg"):
        f.unlink()

    rows, fails = [], []

    def onset_for(t):
        near = [a for a in aud if abs(a - t) < 1.0]
        return min(near, key=lambda a: abs(a - t)) if near else None

    def add(kind, what, ev_t, sp_t, end_t, verdict, note=""):
        on = onset_for(sp_t) if sp_t is not None else None
        rows.append((kind, what, ev_t, sp_t, end_t, on, verdict, note))
        if verdict == "FAIL":
            fails.append(f"{kind} {what}: {note}")

    # reactions
    for s in says:
        if s.get("kind") not in ("win", "loss") or s["id"] not in starts:
            continue
        r = next((r for r in rounds if r.get("rid") == s.get("rid")), None)
        if not r:
            continue
        res_t, sp, en = vt(r["ts"]), starts[s["id"]]["video_t"], ends.get(s["id"], {}).get("video_t", dur)
        nxt = [vt(e["ts"]) for e in rstart.values() if e["ts"] > r["ts"]]
        nxt_t = min(nxt) if nxt else dur
        ok = res_t <= sp and en <= nxt_t + 0.05
        what = (f"{r['game']} {'roll ' + str(r.get('roll')) if r.get('roll') is not None else str(r.get('multiplier')) + 'x'}"
                f" {'win' if r.get('win') else 'loss'}")
        add("reaction", what, res_t, sp, en, "PASS" if ok else "FAIL",
            f"next round starts {nxt_t:.2f}" + ("" if ok else " (overlaps)"))
        for tag, t in (("a_result", res_t + 0.4), ("b_speech", sp + 0.5), ("c_before_next", min(en, nxt_t) - 0.3)):
            frame(mp4, t, fdir / f"react_{r['rid']}_{tag}.jpg")
    # switches
    for e in [e for e in gev if e.get("state") == "switch_pending"]:
        s = next((s for s in says if s.get("sw") == e.get("sw")), None)
        card = next((c for c in cards if c["ts"] >= e["ts"]), None)
        nav = next((g for g in gev if g.get("state") == "switching" and g["ts"] >= e["ts"]), None)
        ready = next((g for g in gev if g.get("state") == "ready" and g["ts"] >= e["ts"]), None)
        sp = starts[s["id"]]["video_t"] if s and s["id"] in starts else None
        card_t = card["video_t"] if card else None
        ok = sp is not None and card_t is not None and sp <= card_t and (s and e["to"].split()[-1] in s["text"])
        add("switch", f"-> {e['to']}", vt(e["ts"]), sp, ends.get(s["id"], {}).get("video_t") if s else None,
            "PASS" if ok else "FAIL",
            f"card {card_t:.2f}, nav {vt(nav['ts']):.2f}, new game live {vt(ready['ts']) if ready else -1:.2f}; "
            f"line: {s['text'][:60] if s else '-'}" if card else "no switch card")
        if sp is not None:
            frame(mp4, sp + 0.4, fdir / f"switch_{e['sw']}_a_speech.jpg")
        if card_t is not None:
            frame(mp4, card_t + 0.5, fdir / f"switch_{e['sw']}_b_card.jpg")
        if ready:
            frame(mp4, vt(ready["ts"]) + 1.0, fdir / f"switch_{e['sw']}_c_live.jpg")
    # chat
    chat_in = [e for e in brain if e.get("ev") == "chat_in" and e.get("action") != "drop" and e["ts"] >= t0]
    replies = [s for s in says if s.get("reply_to") and s["id"] in starts]
    spoken_order = [s["reply_to"] for s in sorted(replies, key=lambda s: starts[s["id"]]["video_t"])]
    arrived_order = [c["user"] for c in chat_in if c["user"] in spoken_order]
    for c in chat_in:
        s = next((s for s in replies if s["reply_to"] == c["user"]), None)
        sp = starts[s["id"]]["video_t"] if s else None
        inflight = ""
        if sp is not None:
            during = [rid for rid, e in rstart.items() if vt(e["ts"]) <= sp and
                      not any(r.get("rid") == rid and vt(r["ts"]) <= sp for r in rounds)]
            inflight = " (a round was running!)" if during else ""
        ok = sp is not None and not inflight
        add("chat", f"{c['user']}: {c['text'][:34]}", vt(c["ts"]), sp, ends.get(s["id"], {}).get("video_t") if s else None,
            "PASS" if ok else "FAIL", (f"reply {sp - vt(c['ts']):.1f} s after the message" if sp else "no reply") + inflight)
        if sp is not None:
            frame(mp4, sp - 0.4, fdir / f"chat_{c['user']}_a_before.jpg")
            frame(mp4, sp + 0.6, fdir / f"chat_{c['user']}_b_speech.jpg")
    order_ok = spoken_order == arrived_order
    if not order_ok:
        fails.append(f"chat order: arrived {arrived_order}, answered {spoken_order}")

    # audio: every logged speech start should have a real onset next to it
    matched = [abs(onset_for(s["video_t"]) - s["video_t"]) for s in starts.values()
               if s["video_t"] < dur - 0.5 and onset_for(s["video_t"]) is not None]
    stale = [e for e in brain if e.get("ev") == "stale"]
    lines = [f"Sync check  {mp4.name}  ({dur:.1f} s)",
             "Times are seconds into the recording. speech = logged start of Hubie's line; audio = measured onset",
             "in the mp4's sound track (silencedetect). A reaction must start after its result and end before the",
             "next round starts; a switch line must start before the 'Next up' card; chat in arrival order.", "",
             f"{'type':9} {'event':40} {'event t':>8} {'speech':>8} {'ends':>8} {'audio':>8} {'lag':>6}  result  note"]
    for kind, what, ev_t, sp, en, on, verdict, note in rows:
        f = lambda v: f"{v:8.2f}" if v is not None else f"{'-':>8}"   # noqa: E731
        lag = f"{sp - ev_t:6.2f}" if sp is not None else f"{'-':>6}"
        lines.append(f"{kind:9} {what[:40]:40} {f(ev_t)} {f(sp)} {f(en)} {f(on)} {lag}  {verdict:6}  {note}")
    lines += ["", f"chat order: arrived {arrived_order} -> answered {spoken_order}  {'PASS' if order_ok else 'FAIL'}",
              f"audio onsets matched to logged speech starts: {len(matched)}/{len(starts)}, "
              f"max offset {max(matched) if matched else 0:.2f} s",
              f"lines dropped as stale (never said late): {len(stale)}"
              + "".join(f"\n  - {e.get('slot')}: {e.get('why')} {str(e.get('text', ''))[:60]}" for e in stale),
              f"rounds: {len(rounds)}, reactions spoken: {sum(1 for r in rows if r[0] == 'reaction')}",
              "", "RESULT: " + ("ALL PASS" if not fails else "FAILS:\n  " + "\n  ".join(fails)),
              f"frames: {fdir.relative_to(ROOT)}/"]
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
