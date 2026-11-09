# Stack

## Languages

| | Used for |
|---|---|
| Python 3.10+ | every process (stage, game, brain, watchdog, Kick webhook) and the tools |
| JavaScript (in-page) | the game adapters' page scripts: zero-amount check + click in one `page.evaluate` |
| Bash | demo / proof-run scripts |
| JavaScript (Node) | `ecosystem.config.js` for pm2 |

## Python libraries

Versions are the ones the project was developed and tested with; `requirements.txt` sets minimums.

| Library | Version tested | Used for |
|---|---|---|
| numpy | 1.24.3 | frame buffers, audio mixing |
| Pillow | 12.3.0 | text and overlay layers |
| opencv-python-headless | 4.11.0 | compositing, scaling, clip blending, game crops |
| playwright | 1.44.0 | attaching to Chrome over CDP, screencast, page scripts |
| edge-tts | 7.3.1 | Hubie's voice (Microsoft neural voices, free) |
| cryptography | 48.0.0 | Kick webhook RSA signature verification |
| websockets | 15.0.1 | legacy Kick public chat socket (optional source) |
| fonttools | 4.65.0 | drops characters the overlay fonts can't draw (optional) |
| pytest | 9.1.1 | tests |

Everything else is the standard library (`http.server` for the webhook, `urllib` for the LLM APIs, so no
vendor SDKs are needed).

## Services

| Service | Used for |
|---|---|
| **Kick** | RTMPS ingest for the stream; official Kick API (OAuth client credentials + `chat.message.sent` webhooks) for chat |
| **Housebets** | the games: House originals Dice, Plinko and Mines, played with zero-amount bets |
| **Groq** | first LLM in the free chain, model `qwen/qwen3.8-27b` (OpenAI-compatible chat API) |
| **Google Gemini API** | second LLM in the chain, model `gemini-3.8-flash` |
| **Anthropic Claude API** (optional) | `claude-haiku-5-5`, used alone when `ANTHROPIC_API_KEY` is set |
| **edge-tts** (Microsoft Edge read-aloud voices) | text to speech, voice `en-US-AndrewNeural` at -12% rate, -6 Hz pitch |

Without any LLM key the brain uses its own written line pools, so the stream never depends on a paid service.

## Infrastructure

| | |
|---|---|
| Ubuntu VPS | 4 vCPU / 8 GB, no GPU (target Ubuntu 24.04; developed on 22.04) |
| pm2 | supervises the five processes, restart on exit, start on boot |
| ffmpeg + libx264 + AAC | one encoder: 1280x720 at 30 fps, `veryfast`, 2500 kbit/s, to Kick (FLV over RTMPS) and/or hourly MPEG-TS files |
| Xvfb | virtual display for the headful Chrome |
| Google Chrome (stable) | the logged-in game browser, reached over the DevTools protocol on 127.0.0.1 |
| nginx + Let's Encrypt | HTTPS in front of the Kick webhook receiver |
| tmpfs (`/dev/shm`) | the file bus between processes |

## Assets

| | |
|---|---|
| Fonts | Figtree, Red Hat Display, Oxanium (the fonts housebets.com uses) and DM Serif Display, all SIL OFL 1.1, from Google Fonts |
| Hubie clips | AI-generated video clips of Hubie (idle, two talk loops, two wins), stabilised to one locked framing with OpenCV (SIFT + RANSAC) by `tools/build_clips.py`; mouth positions per frame in `anim.json` |

## AI tools used to build it

- **Claude Code** (Anthropic) wrote and debugged most of the code, tests and docs together with the project
  owner, ran the proof runs and timing checks, and reviewed the recordings frame by frame.
- **Google Gemini** video generation (by the project owner) for Hubie's animation clips.
