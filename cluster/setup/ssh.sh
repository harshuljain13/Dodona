#!/usr/bin/env bash
# Open an interactive shell on the GPU box, already cd'ed into ~/dodona, with
# every port this fleet uses forwarded to the Mac. Leave this session open.
# Run on: Mac. Reads LAMBDA and LAMBDA_SSH_KEY from .env, so that file must
# exist before the first run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_ROOT="$(cd "$ROOT/.." && pwd)"
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi
exec ssh -i "$LAMBDA_SSH_KEY" \
  -o StrictHostKeyChecking=accept-new \
  -o ExitOnForwardFailure=no \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=6 \
  -L 8000:127.0.0.1:8000 \
  -L 8001:127.0.0.1:8001 \
  -L 8002:127.0.0.1:8002 \
  -L 8080:127.0.0.1:8080 \
  -L 50051:127.0.0.1:50051 \
  -L 3000:127.0.0.1:3000 \
  -L 30030:127.0.0.1:30030 \
  -L 30800:127.0.0.1:30800 \
  -L 30801:127.0.0.1:30801 \
  -L 30080:127.0.0.1:30080 \
  -L 31495:127.0.0.1:31495 \
  -t "$LAMBDA" \
  'mkdir -p ~/dodona; cd ~/dodona; exec bash -l'
