#!/bin/bash
# Join the recorded .ts segments (in time order) into one shareable mp4 under ~50 MB.
# usage: tools/make_demo.sh OUT.mp4 [segments...]   (default: every recordings/*.ts)
set -e
cd "$(dirname "$0")/.."
OUT=${1:-recordings/demo.mp4}; shift || true
SEGS=("$@"); [ ${#SEGS[@]} -eq 0 ] && SEGS=(recordings/hubie_*.ts)
LIST=$(mktemp); for s in "${SEGS[@]}"; do echo "file '$PWD/$s'" >> "$LIST"; done
ffmpeg -v error -y -f concat -safe 0 -i "$LIST" -c:v libx264 -preset veryfast -crf 25 -threads 2 \
  -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart "$OUT"
rm -f "$LIST"; ls -la "$OUT"
