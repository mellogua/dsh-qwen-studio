#!/usr/bin/env bash
# guard.sh [container] [floor_mb] — kill the studio before it starves the DeepSeek rank.
set -u
N=${1:-comfy-studio}
FLOOR_MB=${2:-4096}
LOG=${STUDIO_LOGDIR:-$HOME/studio/logs}
mkdir -p "$LOG"
echo "$(date +%F' '%T) guard start name=$N floor=${FLOOR_MB}MB" >> "$LOG/guard.log"
sleep 3
while [ -n "$(echo 11111111 | sudo -S docker ps -q -f name=^${N}$ 2>/dev/null)" ]; do
  a=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  echo "$(date +%T) avail_mb=$a" >> "$LOG/guard_mem.log"
  if [ "$a" -lt "$FLOOR_MB" ]; then
    echo 11111111 | sudo -S docker kill "$N" >/dev/null 2>&1
    echo "$(date +%F' '%T) KILLED $N avail_mb=$a" >> "$LOG/guard.log"
    break
  fi
  sleep 1
done
echo "$(date +%F' '%T) guard exit" >> "$LOG/guard.log"
