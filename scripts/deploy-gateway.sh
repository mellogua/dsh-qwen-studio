#!/usr/bin/env bash
set -u
export LC_ALL=C
H="$HOME/studio"
IMG=$(echo 11111111 | sudo -S docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null | grep 'vllm/vllm-openai' | awk '{print $2}' | head -1)
mkdir -p "$H/gateway/out"

echo 11111111 | sudo -S docker rm -f studio-gateway 2>/dev/null
echo "[$(date +%H:%M:%S)] start gateway"
echo 11111111 | sudo -S docker run -d --name studio-gateway --network host \
  --oom-score-adj 500 --restart no \
  -v "$H:$H" -w "$H" --entrypoint python3 "$IMG" \
  "$H/gateway.py" >/dev/null

for i in $(seq 1 40); do
  if curl -s -m 2 -o /dev/null http://127.0.0.1:8100/healthz; then
    echo "[$(date +%H:%M:%S)] gateway up after ${i}s"
    curl -s http://127.0.0.1:8100/healthz; echo
    exit 0
  fi
  sleep 1
done
echo "=== NOT UP"
echo 11111111 | sudo -S docker logs --tail 25 studio-gateway 2>&1
exit 1
