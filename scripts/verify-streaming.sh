#!/usr/bin/env bash
# Verify the proxy's streaming contract on a side port BEFORE switching the public entry point.
# Everything here is read-only: it never restarts the model or the tunnel.
set -u
BASE=${1:-http://127.0.0.1:8090}
KEY=${2:-dummy}
MODEL=${3:-deepseek-v4.1-flash-1017-heretic}

fail=0
tmp=$(mktemp)

echo "[1/4] /v1/models"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -H "Authorization: Bearer $KEY" "$BASE/v1/models")
echo "  http $code"; [ "$code" = "200" ] || fail=1

echo "[2/4] streaming chat (expect SSE + [DONE])"
curl -s -N -m 120 "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"stream\":true,\"max_tokens\":24,\"messages\":[{\"role\":\"user\",\"content\":\"数到三\"}]}" > "$tmp"
if grep -q '^data: ' "$tmp" && grep -q 'data: \[DONE\]' "$tmp"; then
  echo "  chunks: $(grep -c '^data: ' "$tmp"), DONE ok"
else
  echo "  missing [DONE] or no SSE frames (first line: $(head -c 80 "$tmp"))"; fail=1
fi

echo "[3/4] auth passthrough (no key -> upstream status)"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -X POST "$BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}")
echo "  http $code (401/403 expected when the upstream enforces a key)"

echo "[4/4] image intent goes through the studio gateway"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 300 -X POST "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":200,\"messages\":[{\"role\":\"user\",\"content\":\"帮我画一张: 海边灯塔\"}]}")
echo "  http $code"; [ "$code" = "200" ] || fail=1

rm -f "$tmp"
[ "$fail" = "0" ] && echo "OK" || { echo "FAILED"; exit 1; }
