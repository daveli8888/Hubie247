# Setup: from a fresh Ubuntu 24.04 VPS to a 24/7 Kick stream

Tested target: **4 vCPU / 8 GB RAM, Ubuntu 24.04, no GPU.** Rough budget while live: the stage + ffmpeg use
about 1.5 to 2 cores at 720p30 `veryfast`, Chrome about one core, the rest is small.

You will need:
- a Kick account for the stream, plus a Kick developer app (for chat)
- a domain name pointing at the VPS (Kick only delivers chat webhooks to HTTPS)
- a Housebets account the operator logs into himself (zero-amount bets only; no balance is needed or used)
- optional: free Groq and/or Gemini API keys, or an Anthropic key (without any key Hubie uses written lines)

All commands below run as a normal sudo user from the repo folder unless noted.

## 1. System packages

```bash
sudo apt update
sudo apt install -y git ffmpeg xvfb python3-venv python3-pip fonts-dejavu-core nginx certbot python3-certbot-nginx

# Google Chrome (the stream's game browser; plain Chrome, see section 6)
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb

# Node.js + pm2 (process supervisor)
sudo apt install -y nodejs npm
sudo npm install -g pm2
```

Check: `ffmpeg -encoders | grep libx264`, `google-chrome --version`, `pm2 --version`.

## 2. Code and Python environment

```bash
git clone https://github.com/daveli8888/Hubie247 hubie247 && cd hubie247
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium   # only used by the offline HUBIE_GAME=local stand-in
cp .env.example .env && chmod 600 .env
python3 -m pytest                        # offline tests; browser-only tests skip without a Chrome
```

`.env` is git-ignored. Never commit it.

## 3. `.env` fields

Every field is optional; with an empty `.env` you get the offline demo (stub lines, fake chat, recording to
`recordings/`). The important groups:

| Field(s) | What it is |
|---|---|
| `HUBIE_RTMP_URL` | Kick ingest URL + stream key, `rtmps://<ingest>/<key>`. Empty = record to files instead |
| `HUBIE_RECORD_LIVE` | `1` = while live, also write hourly local copies of the same encode (your unedited VOD) |
| `HUBIE_CHAT_SOURCES` | `kickhook` for Kick chat via the official API (add `fake` to also accept local test chat) |
| `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET` | your Kick developer app's credentials (used only to subscribe the webhook) |
| `KICK_CHANNEL` | your channel slug |
| `KICK_WEBHOOK_PORT` | local port of the webhook receiver (default 8108) |
| `ANTHROPIC_API_KEY`, `HUBIE_MODEL` | optional Claude backend (if set, Claude is used alone) |
| `GROQ_API_KEY`, `HUBIE_GROQ_MODEL` | free Groq key, first in the free chain |
| `GEMINI_API_KEY`, `HUBIE_GEMINI_MODEL` | free Gemini key, second in the chain |
| `HUBIE_LLM_EVENT_GAP`, `HUBIE_LLM_COOLDOWN` | free-quota budget for non-chat lines; rest period for a failing backend |
| `HUBIE_VOICE`, `HUBIE_VOICE_RATE`, `HUBIE_VOICE_PITCH` | edge-tts voice (default: Andrew, -12%, -6 Hz) |
| `HUBIE_GAME` | `housebets` (real site, zero bets) or `local` (offline stand-in) |
| `HUBIE_HB_CDP` | DevTools address of the logged-in Chrome, default `http://127.0.0.1:9241` |
| `HUBIE_HB_ROTATION`, `HUBIE_HB_ROTATE_MIN` | games to rotate through (`dice,plinko,mines`) and minutes per game |
| `HUBIE_VIEWER_URL` | the HTTPS address of the login viewer, quoted in `RUN/needs_owner.txt` |
| `HUBIE_LAYOUT` | `A` Broadcast or `B` Lounge |

The full list with defaults and comments is in `.env.example`; tuning knobs are in section 9.

## 4. Kick: stream key

1. Kick dashboard -> Settings -> **Stream URL & Key**.
2. Put both together in `.env`: `HUBIE_RTMP_URL=rtmps://<stream url>/<stream key>` (if the URL already ends in
   `/app/`, append the key after it).
3. Set the stream title/category on Kick yourself; the code only sends the picture.

The stage hides the RTMP URL in its log output, so the key never lands in `pm2 logs`.

## 5. Kick: chat through the official API (webhook)

Kick pushes each chat message as a signed HTTPS POST (`chat.message.sent`). `hubie247/kick_webhook.py` listens on
`127.0.0.1:KICK_WEBHOOK_PORT`, verifies Kick's RSA signature (public key fetched from Kick, never hardcoded),
drops duplicates and appends the message to the chat file the brain reads.

1. Create an app at **kick.com/settings/developer**. Enable webhooks, set the webhook URL to
   `https://<your-domain>/kick/webhook`, and allow it to subscribe to events. Put the client id/secret and your
   channel slug in `.env`.
2. HTTPS reverse proxy (nginx example). Get a certificate with `sudo certbot --nginx -d <your-domain>`, then in
   that server block:

   ```nginx
   location /kick/ {
       proxy_pass http://127.0.0.1:8108;
       proxy_set_header Host $host;
   }
   ```
3. Start the receiver and subscribe once:

   ```bash
   pm2 start ecosystem.config.js --only hubie247-kickhook
   curl https://<your-domain>/kick/health          # -> ok
   .venv/bin/python -m hubie247.kick_webhook subscribe
   ```
4. `HUBIE_CHAT_SOURCES=kickhook` in `.env`. Type in your Kick chat and watch
   `tail -f /dev/shm/hubie247/kick_chat.jsonl`.

Hubie answers by voice on stream; nothing is posted back into Kick chat.

## 6. The browser: a logged-in Chrome the stream attaches to

House originals have no fun mode, so Hubie plays with **zero-amount bets from a logged-in account**. The stream
does not log in by itself and never touches a Cloudflare check: a person (the operator) logs in once, and the
session is kept in the Chrome profile.

What the game process needs is simply **a plain Google Chrome with its DevTools port on `127.0.0.1:9241`**,
logged in to housebets.com. The browser launcher used on our server is not part of this repo (it belongs to
another project), so start one yourself, for example on a virtual display you can reach over VNC:

```bash
pm2 start "xvfb-run -a -s '-screen 0 1280x900x24' google-chrome --remote-debugging-port=9241 \
    --remote-debugging-address=127.0.0.1 --user-data-dir=$PWD/browser_profile https://housebets.com" \
    --name hubie247-browser
```

Log in to Housebets yourself in that window (VNC, or any remote viewer you trust). The stream never types
credentials and never touches a Cloudflare check; if one appears it only waits and writes `RUN/needs_owner.txt`.
Set `HUBIE_VIEWER_URL` to wherever you reach the browser so that note points to it. Keep port 9241 closed to
the internet (bind it to 127.0.0.1; don't change that). If Housebets is not available in your server's region,
point Chrome at a proxy in a region where it is (`--proxy-server=...`).

On first start the game process navigates the tab to the first game in `HUBIE_HB_ROTATION`. Before the first
round, check in the browser that the Bet Amount field shows 0; the adapters refuse to click otherwise.

## 7. Start everything

```bash
export HUBIE_PYTHON=$PWD/.venv/bin/python        # ecosystem.config.js runs the modules with this interpreter
pm2 start ecosystem.config.js                     # stage, game, brain, watchdog, kickhook
pm2 status
pm2 logs hubie247-brain                           # what Hubie heard, said and blocked
pm2 logs hubie247-game                            # browser state, rounds, safety refusals
```

Make it survive reboots:

```bash
pm2 save
pm2 startup        # run the command it prints (installs a systemd unit for pm2)
```

`HUBIE_STAGE_NICE` (default -10) and `HUBIE_PYTHON` are read from the shell when `pm2 start` runs. A negative
nice value only takes effect when pm2 runs as root; as a normal user `nice` prints a warning and the stage
runs at normal priority (set `HUBIE_STAGE_NICE=0` to silence it).

## 8. What runs unattended

- **pm2** restarts any process that exits (exponential back-off, up to 1000 restarts).
- **The watchdog** (`hubie247-watchdog`) restarts parts that hang: stage if its heartbeat or ffmpeg's frame
  counter stops for `HUBIE_WD_STALL` s; game or brain after 30 s without a heartbeat. Log:
  `logs/watchdog.jsonl`.
- **The game runner** reloads broken pages, accepts cookie banners, closes popups, and shows the stand-in
  table while the site is unavailable. Only a Cloudflare challenge or a logout needs a person: look for
  `RUN/needs_owner.txt` (e.g. check it from cron and send yourself a message).
- **LLM and TTS failures** fall back automatically (next backend, written lines, pre-voiced lines).

## 9. Tuning knobs

| Variable | Default | Effect |
|---|---|---|
| `HUBIE_HB_FPS` | 10 | game picture frames per second into the stage; lower it to save CPU |
| `HUBIE_X264_PRESET` | veryfast | x264 speed/quality; `superfast`/`ultrafast` if the encoder can't keep up |
| `HUBIE_X264_THREADS` | 2 | encoder threads |
| `HUBIE_VIDEO_BITRATE` | 2500k | stream bitrate |
| `HUBIE_WD_STALL` | 12 | seconds of frozen encoder before the watchdog restarts the stage |
| `HUBIE_HB_ROTATION` | dice,plinko | games in rotation (`dice`, `plinko`, `mines`; `slot` needs WebGL) |
| `HUBIE_HB_ROTATE_MIN` | 20 | minutes per game |
| `HUBIE_LAYOUT` | A | `A` Broadcast, `B` Lounge |
| `HUBIE_LLM_EVENT_GAP` | 120 | at most one LLM call per N s for game/idle lines (chat always uses the LLM) |
| `HUBIE_IDLE_SECS` | 25 | idle commentary after this much silence |
| `HUBIE_TURN_*`, `HUBIE_SWITCH_*`, `HUBIE_REACT_*` | see `.env.example` | turn-taking caps between game and speech |

## 10. LLM keys and quotas

- **No key**: Hubie speaks from large written line pools (per game, win/loss/idle, chat topics). Fine for testing.
- **Groq** (console.groq.com) and **Gemini** (aistudio.google.com) both have free tiers. With both keys set the
  chain is Groq first, Gemini second, written lines last. Free tiers are rate-limited per day and per minute
  (check the providers' current limits); `HUBIE_LLM_EVENT_GAP` keeps non-chat lines to about 30 an hour so the
  quota goes to chat replies. A backend that errors (quota, timeout) rests `HUBIE_LLM_COOLDOWN` s and the next
  one answers.
- **Anthropic** (optional, paid): `ANTHROPIC_API_KEY` switches to Claude (`HUBIE_MODEL`, default
  `claude-haiku-5-5`) and skips the chain.

Every model line still goes through the output filter before it is voiced.

## 11. Before going live: dry runs

```bash
tools/proof_run.sh 180 100                 # full stack, scripted test chat, encoder killed at 100 s -> recovers
MODE=hang tools/proof_run.sh 100 30        # encoder frozen instead -> watchdog recovers
tools/housebets_demo.sh 300 2              # real games (needs the logged-in Chrome), file output only
python3 tools/sync_check.py recordings/housebets_demo_<time>.mp4   # timing table: speech vs picture
```

Both record to `recordings/` and never stream (`housebets_demo.sh` forces `HUBIE_RTMP_URL` empty).

For the contest VOD: `HUBIE_RECORD_LIVE=1` keeps hourly `.ts` copies of exactly what went to Kick; join them with
`tools/make_demo.sh out.mp4 recordings/live_*.ts` or `ffmpeg -f concat -safe 0 -i list.txt -c copy vod.mp4`.

## 12. Troubleshooting

| Symptom | Look at | Usual fix |
|---|---|---|
| Stream shows the stand-in table | `cat RUN/needs_owner.txt`, `pm2 logs hubie247-game` | Cloudflare box or logged out: fix it in the viewer; it resumes by itself |
| "be right back" card | `pm2 logs hubie247-game` | game process restarting or Chrome not reachable on 9241: `curl 127.0.0.1:9241/json/version` |
| Game logs `Unsafe: bet amount is not zero` | the viewer | set the Bet Amount back to 0 by hand; the runner refuses to bet until then |
| Hubie only uses written lines | `pm2 logs hubie247-brain` (`LLM failed`) | key missing or quota used up; add the second free key |
| Captions but no voice | brain log `tts failed` | edge-tts unreachable from the VPS; it retries, pre-voiced lines cover reactions |
| No chat | `curl https://<domain>/kick/health`, `pm2 logs hubie247-kickhook` | webhook URL in the Kick app, HTTPS cert, rerun `kick_webhook subscribe`, `HUBIE_CHAT_SOURCES=kickhook` |
| Kick shows offline / stutters | `logs/stage_ffmpeg.log`, `logs/watchdog.jsonl` | CPU: lower `HUBIE_HB_FPS`, use a faster `HUBIE_X264_PRESET`; check the stream key |
| Watchdog restarts the stage in a loop | `logs/watchdog.jsonl` | encoder too slow for the box, or RTMP refused (wrong key) |
