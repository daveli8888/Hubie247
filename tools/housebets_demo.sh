#!/bin/bash
# Local demo recording with the REAL Housebets game (zero bets) - never streams to Kick.
# Runs game + stage + brain as plain processes in a separate RAM dir (not the pm2 ones), records,
# then joins the segment into recordings/housebets_demo_<time>.mp4.
#   tools/housebets_demo.sh [seconds=95] [rotate_after_min=0.8]
# HUBIE_DEMO_NAME=housebets_sync  -> recordings/housebets_sync_<time>.mp4
# HUBIE_DEMO_CHAT="25|user|text;60|user|text"  test chat, each sent ~1 s into the next round after its time
set -u
cd "$(dirname "$0")/.."
DUR=${1:-95}
ROT=${2:-0.8}
STAMP=$(date +%Y%m%d_%H%M%S)
export HUBIE_RUN_DIR=/dev/shm/hubie247_demo
export HUBIE_RTMP_URL=                  # file output only, whatever .env says
export HUBIE_SEGMENT_SECS=3600
export HUBIE_GAME=housebets HUBIE_HB_ROTATION=dice,plinko HUBIE_HB_ROTATE_MIN=$ROT
export HUBIE_CHAT_SOURCES=fake HUBIE_GAME_LINE_GAP=${HUBIE_GAME_LINE_GAP:-12} HUBIE_WIN_CLIP_GAP=20
rm -rf "$HUBIE_RUN_DIR"; mkdir -p "$HUBIE_RUN_DIR" logs
for f in demo_game.log demo_stage.log demo_brain.log demo_chat.log; do : > logs/$f; done
mv -f logs/stage_timeline.jsonl logs/stage_timeline.prev.jsonl 2>/dev/null
echo "$STAMP" > logs/demo_stamp.txt
before=$(ls recordings/hubie_*.ts 2>/dev/null)
python3 -u -m hubie247.game  > logs/demo_game.log 2>&1 & G=$!
sleep 6                                  # let the game go live before the picture starts
python3 -u -m hubie247.stage > logs/demo_stage.log 2>&1 & S=$!
python3 -u -m hubie247.brain > logs/demo_brain.log 2>&1 & B=$!
IFS=';' read -ra CHAT <<< "${HUBIE_DEMO_CHAT:-20|mila_rose|hubie what game is this?;50|nightowl|love the hat hubie}"
python3 -u tools/demo_chat.py "${CHAT[@]}" > logs/demo_chat.log 2>&1 & C=$!
sleep "$DUR"
kill -TERM $B $G $C 2>/dev/null; kill -TERM $S; wait $S 2>/dev/null; wait $B $G 2>/dev/null
SEG=$(comm -13 <(echo "$before") <(ls recordings/hubie_*.ts) | tail -1)
OUT=recordings/${HUBIE_DEMO_NAME:-housebets_demo}_$STAMP.mp4
ffmpeg -v error -y -i "$SEG" -c:v libx264 -preset veryfast -crf 23 -threads 2 -pix_fmt yuv420p \
  -c:a aac -b:a 128k -movflags +faststart "$OUT" && rm -f "$SEG"
echo "$OUT"
