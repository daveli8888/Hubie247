"""edge-tts voice (free Microsoft neural voices). Text -> raw s16le 48 kHz stereo PCM file for the stage.
Retries (the service sometimes returns no audio); returns None on failure so the line is still shown
as a caption instead of stalling the show."""
import asyncio
import subprocess

import edge_tts

from .config import AUDIO_RATE, env

VOICE = env("HUBIE_VOICE", "en-US-AndrewNeural")
RATE = env("HUBIE_VOICE_RATE", "-12%")
PITCH = env("HUBIE_VOICE_PITCH", "-6Hz")


async def _mp3(text, path):
    await edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH).save(path)


async def _hedged(text, base, hedge_after=2.5, max_tries=4, total=18):
    """edge-tts from a VPS often stalls or returns nothing (about half the first tries in testing).
    Instead of waiting out a timeout, start another request every `hedge_after` s and keep the first
    one that finishes with audio."""
    tasks, errors = [], []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total
    try:
        while loop.time() < deadline:
            if len(tasks) < max_tries and (not tasks or all(not t.done() or t.exception() for t in tasks)):
                path = f"{base}.{len(tasks)}.mp3"
                tasks.append(asyncio.create_task(_mp3(text, path)))
                tasks[-1].path = path
            done, _ = await asyncio.wait(tasks, timeout=max(0.05, min(hedge_after, deadline - loop.time())),
                                         return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                if t.done() and not t.cancelled():
                    if t.exception() is None:
                        return t.path
                    if t.exception() not in errors:
                        errors.append(t.exception())
            if len(tasks) >= max_tries and all(t.done() for t in tasks):
                break
        raise RuntimeError(f"tts failed: {errors!r}"[:300])
    finally:
        for t in tasks:
            t.cancel()


def synth(text, out_pcm, total=18):
    """`total` caps the whole edge-tts attempt (hedged retries included); time-critical lines (reactions to a
    round) pass a short budget so a stall skips the line instead of holding the show."""
    try:
        short = total < 10       # most good answers come back in ~1 s; a stalled one rarely recovers
        mp3 = asyncio.run(_hedged(text, out_pcm, hedge_after=1.3 if short else 2.5, total=total,
                                  max_tries=4 if short else 7))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp3, "-af", "apad=pad_dur=0.25",
                        "-f", "s16le", "-ac", "2", "-ar", str(AUDIO_RATE), out_pcm], check=True, timeout=20)
        return out_pcm
    except Exception as e:
        print(f"tts failed, caption only: {e!r}", flush=True)
        return None
