#!/usr/bin/env bash
set -u
export LC_ALL=C
H="$HOME/studio"
IMG=$(echo 11111111 | sudo -S docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null | grep 'vllm/vllm-openai' | awk '{print $2}' | head -1)

echo 11111111 | sudo -S docker rm -f studio-auto 2>/dev/null
echo "[$(date +%H:%M:%S)] start auto-proxy"
echo 11111111 | sudo -S docker run -d --name studio-auto --network host \
  --oom-score-adj 400 --restart no \
  -v "$H:$H" -w "$H" --entrypoint python3 "$IMG" \
  "$H/auto-proxy.py" >/dev/null

for i in $(seq 1 40); do
  if curl -s -m 2 -o /dev/null http://127.0.0.1:8090/healthz; then
    echo "[$(date +%H:%M:%S)] auto-proxy up after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "=== NOT UP"
echo 11111111 | sudo -S docker logs --tail 30 studio-auto 2>&1
exit 1
