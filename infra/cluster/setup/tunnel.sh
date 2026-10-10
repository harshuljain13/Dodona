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
# Close any tunnel we already hold to this box. Two tunnels cannot bind the
# same local ports, and the second one fails with nine "Address already in
# use" lines and no forwards — which looks like a broken tunnel when it is
# really a duplicate. One command, always works, is worth more than the
# second shell.
STALE="$(pgrep -f "ssh .*-L 8000:127.0.0.1:8000 .*${LAMBDA##*@}" 2>/dev/null | grep -v "^$$\$" || true)"
if [[ -n "$STALE" ]]; then
  echo "  closing $(echo "$STALE" | wc -l | tr -d ' ') existing tunnel(s) to ${LAMBDA##*@}"
  # shellcheck disable=SC2086
  kill $STALE 2>/dev/null || true
  sleep 1
fi

exec ssh -i "$LAMBDA_SSH_KEY" \
  -o StrictHostKeyChecking=accept-new \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=6 \
  -L 8000:127.0.0.1:8000 \
  -L 8080:127.0.0.1:8080 \
  -L 30030:127.0.0.1:30030 \
  -L 30080:127.0.0.1:30080 \
  -L 30800:127.0.0.1:30800 \
  -L 30801:127.0.0.1:30801 \
  -L 31495:127.0.0.1:31495 \
  -t "$LAMBDA" \
  'mkdir -p ~/dodona; cd ~/dodona; exec bash -l'
