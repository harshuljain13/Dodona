#!/usr/bin/env bash
# Bring up the voice demo: LiveKit, the agent worker, the UI.
#
# Three processes, one terminal. Killing this script stops all of them — a demo
# that leaves a worker holding a microphone and a docker container listening on
# 7880 is worse than one that did not start.
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

: "${DEEPGRAM_API_KEY:?set DEEPGRAM_API_KEY in .env — see app/README.md}"

# LiveKit --dev uses this well-known pair. Localhost only; not a secret.
export LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:7880}"
export LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-devkey}"
export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-secret}"
export NEXT_PUBLIC_LIVEKIT_URL="$LIVEKIT_URL"

say() { printf '\033[1;36m==> %s\033[0m\n' "$1"; }

if ! curl -sf -m 2 http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
  echo "    WARNING: the gateway is not answering on :8080."
  echo "    Run 'make ssh' first, or the agent will have nothing to think with."
fi

say "LiveKit  —  127.0.0.1:7880"
docker compose -f app/docker-compose.yml up -d
# The worker's first act is to register with LiveKit; starting it against a
# server that is still binding fails in a way that looks like a bad key.
until curl -sf -m 2 http://127.0.0.1:7880 >/dev/null 2>&1; do sleep 0.5; done

cleanup() {
  say "stopping"
  kill "${WORKER_PID:-}" "${WEB_PID:-}" 2>/dev/null || true
  docker compose -f app/docker-compose.yml down >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

say "agent worker  —  Deepgram ears and mouth, GPU brain"
.venv/bin/python app/voice.py dev &
WORKER_PID=$!

say "UI  —  http://localhost:3000"
(cd app/web && npm run dev) &
WEB_PID=$!

wait
