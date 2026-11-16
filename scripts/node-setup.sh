#!/usr/bin/env bash
# node-setup.sh <sudo_password> — build the Qwen studio on this Spark.
set -u
export LC_ALL=C
PW=${1:?password required}
H="$HOME/studio"
IMG=spark-ds41f:1107
S() { echo "$PW" | sudo -S "$@" 2>/dev/null; }
M="$H/models/Qwen-Image-2.1"
PIPIDX="https://mirrors.aliyun.com/pypi/simple/"

echo "[$(date +%H:%M:%S)] start on $(hostname)"
mkdir -p "$H" "$M/diffusion_models" "$M/text_encoders" "$M/vae"

echo "[$(date +%H:%M:%S)] comfy source"
if [ ! -f "$H/ComfyUI/main.py" ]; then
  curl -sL --max-time 300 -o "$H/comfy.tgz" http://192.168.2.208:9010/comfy-3dd559d.tar.gz
  rm -rf "$H/ComfyUI" "$H/ComfyUI-tmp"
  mkdir -p "$H/ComfyUI-tmp"
  tar -xzf "$H/comfy.tgz" -C "$H/ComfyUI-tmp" --strip-components=1 && mv "$H/ComfyUI-tmp" "$H/ComfyUI"
fi
echo "  comfy $(du -sh "$H/ComfyUI" 2>/dev/null | cut -f1)"

echo "[$(date +%H:%M:%S)] aria2"
if ! command -v aria2c >/dev/null; then
  echo "$PW" | sudo -S apt-get install -y -qq aria2 >/tmp/apt-aria2.log 2>&1 || echo "apt failed"
fi
command -v aria2c >/dev/null && echo "  aria2c ok" || echo "  aria2c missing"

fetch() { # rel_path remote_path
  local out="$M/$1" url="$2"
  [ -s "$out" ] && [ "$(stat -c%s "$out")" -gt 100000000 ] && { echo "  have $1"; return; }
  if command -v aria2c >/dev/null; then
    aria2c -q -x 16 -s 16 -k 1M -c --file-allocation=none -d "$(dirname "$out")" -o "$(basename "$out")" "$url"
  fi
  [ -s "$out" ] || curl -sL --max-time 1800 -o "$out" "http://192.168.2.208:9009/Qwen-Image-2.1/$1"
}

echo "[$(date +%H:%M:%S)] models"
fetch "diffusion_models/qwen_image_2.1_int8_convrot.safetensors" "https://hf-mirror.com/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors"
fetch "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors" "https://hf-mirror.com/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors"
fetch "text_encoders/qwen3vl_8b_int8_convrot.safetensors" "https://hf-mirror.com/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors"
fetch "vae/qwen_image_2.1_vae_bf16.safetensors" "https://hf-mirror.com/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors"
echo "  models $(du -sh "$M" 2>/dev/null | cut -f1)"
find "$M" -name '*.safetensors' -printf '  %10s %p\n'

echo "[$(date +%H:%M:%S)] venv + deps"
if [ ! -x "$H/venv/bin/python" ]; then
  echo "$PW" | sudo -S docker run --rm --gpus all -v "$H:$H" --entrypoint python3 "$IMG" \
    -m venv --system-site-packages "$H/venv" >/tmp/venv.log 2>&1 || { echo "venv failed"; tail -5 /tmp/venv.log; }
fi
cat > "$H/ComfyUI/extra_model_paths.yaml" <<EOF
qwen_studio:
    base_path: $M
    diffusion_models: diffusion_models
    text_encoders: text_encoders
    vae: vae
    loras: loras
EOF
echo "$PW" | sudo -S docker run --rm --gpus all -v "$H:$H" -w "$H/ComfyUI" --entrypoint "$H/venv/bin/pip" "$IMG" \
  install --no-deps --no-cache-dir --index-url "$PIPIDX" --timeout 30 --retries 2 \
  scipy alembic "av>=17.0.0" "simpleeval>=1.0.0" "kornia>=0.7.1" spandrel kornia-rs \
  "comfy-kitchen==0.2.35" "comfy-aimdo==0.5.5" "SQLAlchemy>=2.0.0" "Mako>=1.2" \
  "comfyui-frontend-package==1.53.6" "comfyui-workflow-templates==0.11.66" "comfyui-embedded-docs==0.5.12" \
  torchsde >/tmp/pip-node.log 2>&1
tail -3 /tmp/pip-node.log

echo "[$(date +%H:%M:%S)] start container"
echo "$PW" | sudo -S docker rm -f comfy-studio >/dev/null 2>&1
echo "$PW" | sudo -S docker run -d --name comfy-studio --network host --gpus all \
  --oom-score-adj 800 --restart no \
  -v "$H:$H" -v "$M:$M:ro" \
  -w "$H/ComfyUI" --entrypoint "$H/venv/bin/python" "$IMG" \
  main.py --listen 0.0.0.0 --port 8188 \
    --disable-pinned-memory --fp16-intermediates --reserve-vram 88 >/dev/null

for i in $(seq 1 120); do
  if curl -s -m 3 -o /dev/null "http://127.0.0.1:8188/system_stats"; then
    echo "[$(date +%H:%M:%S)] UP after ${i}s"
    break
  fi
  sleep 1
done
curl -s -m 5 http://127.0.0.1:8188/system_stats | head -c 300; echo

echo "[$(date +%H:%M:%S)] guard"
cp -f "$H/h3_guard.sh" "$H/h3_guard.sh"
pgrep -f 'h3_guard.sh' >/dev/null || setsid nohup bash "$H/h3_guard.sh" comfy-studio 4096 >/dev/null 2>&1 &
echo "[$(date +%H:%M:%S)] DONE $(hostname)"
