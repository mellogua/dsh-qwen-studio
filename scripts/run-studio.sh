#!/usr/bin/env bash
# Start the Qwen-Image-2.1 ComfyUI studio on this Spark, next to the DeepSeek rank.
set -eu
export LC_ALL=C
H=${STUDIO_HOME:-$HOME}
PORT=${STUDIO_PORT:-8188}
COMFY_DIR="$H/studio/ComfyUI"
IMG=$(echo 11111111 | sudo -S docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null | grep 'vllm/vllm-openai' | awk '{print $2}' | head -1)
NAME=${STUDIO_NAME:-comfy-studio}
RESERVE=${STUDIO_RESERVE_VRAM:-88}

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "$NAME exists: docker start $NAME (or docker rm -f $NAME)" >&2
  exit 1
fi

echo 11111111 | sudo -S docker run -d --name "$NAME" --network host --gpus all \
  --oom-score-adj 800 --restart no \
  -v "$H:$H" -v /srv/gb10-cluster/models:/srv/gb10-cluster/models:ro \
  -w "$COMFY_DIR" --entrypoint "$H/venv/bin/python" "$IMG" \
  main.py --listen 0.0.0.0 --port "$PORT" \
    --disable-pinned-memory --fp16-intermediates --reserve-vram "$RESERVE" >/dev/null

for i in $(seq 1 90); do
  if curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/system_stats"; then
    echo "comfy-studio up on :$PORT after ${i}s"
    curl -s "http://127.0.0.1:$PORT/system_stats" | head -c 600
    echo
    exit 0
  fi
  sleep 1
done
echo "did not answer, see: docker logs $NAME" >&2
exit 1
