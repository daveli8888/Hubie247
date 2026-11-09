#!/bin/bash
# Local proof run: whole stack under pm2, fake chat script (incl. safety tests), one forced encoder
# crash at ~100 s to show auto-recovery, then stop. Records to recordings/ (no external streaming).
set -u
cd "$(dirname "$0")/.."
DUR=${1:-180}
CRASH_AT=${2:-100}
# RAM rule on the shared dev box: only one Playwright Chromium at a time -> wait for a free slot
for i in $(seq 1 900); do
  pgrep -f "^[^ ]*ms-playwright/chromium[^ ]*/chrome " >/dev/null || { sleep 2; pgrep -f "^[^ ]*ms-playwright/chromium[^ ]*/chrome " >/dev/null || break; }
  sleep 1
done
echo "[$(date +%T)] chromium slot free after ${i}s"
rm -f /dev/shm/hubie247/show_start   # new show: "on air" clock starts at 0
pm2 start ecosystem.config.js >/dev/null && echo "[$(date +%T)] pm2 started"
sleep 6
python3 tools/fake_chat.py --script > logs/fake_chat.log 2>&1 &
FC=$!
sleep $CRASH_AT
P=$(pgrep -f "^ffmpeg .*-i /dev/shm/hubie247/audio.fifo")
if [ "${MODE:-crash}" = hang ]; then
  echo "[$(date +%T)] hang test: freezing encoder ffmpeg pid $P (SIGSTOP), only the watchdog can catch this"
  [ -n "$P" ] && kill -STOP $P
else
  echo "[$(date +%T)] crash test: killing encoder ffmpeg pid $P"
  [ -n "$P" ] && kill -9 $P
fi
sleep $((DUR - CRASH_AT))
kill $FC 2>/dev/null
pm2 stop ecosystem.config.js >/dev/null; pm2 delete ecosystem.config.js >/dev/null
echo "[$(date +%T)] stopped"
