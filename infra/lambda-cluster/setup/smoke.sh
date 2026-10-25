#!/usr/bin/env bash
# Smoke-test one vLLM endpoint: a single chat completion, then its vllm: metrics.
# Run on: anywhere that can reach LOCAL_BASE_URL (default http://127.0.0.1:8000/v1).
# Prints SMOKE PASS at the end; set -e means any failed curl aborts first.
set -euo pipefail

LOCAL_BASE_URL="${LOCAL_BASE_URL:-http://127.0.0.1:8000/v1}"
LOCAL_BASE_URL="${LOCAL_BASE_URL%/}"
ROOT="${LOCAL_BASE_URL%/v1}"
MODEL="${LOCAL_MODEL:-lab}"

echo "== $LOCAL_BASE_URL/chat/completions =="
curl -sf "$LOCAL_BASE_URL/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hi in one word.\"}],\"max_tokens\":8}"
echo

echo "== $ROOT/metrics (vllm:) =="
curl -sf "$ROOT/metrics" | grep 'vllm:'
echo
echo "SMOKE PASS"
