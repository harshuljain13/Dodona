#!/usr/bin/env bash
# Open an interactive shell on the GPU box, already cd'ed into ~/dodona, with
# every port this fleet uses forwarded to the Mac. Leave this session open.
# Run on: Mac. Reads BOX, SSH_KEY and SSH_PORT from .env, so that file must
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
# LAMBDA / LAMBDA_SSH_KEY are the older names and still work.
BOX="${BOX:-${LAMBDA:-}}"
SSH_KEY="${SSH_KEY:-${LAMBDA_SSH_KEY:-}}"
# RunPod and most container hosts put sshd on a high port, not 22.
SSH_PORT="${SSH_PORT:-22}"

[[ -n "$BOX"     ]] || { echo "Set BOX in .env" >&2; exit 1; }
[[ -f "$SSH_KEY" ]] || { echo "Set SSH_KEY in .env to your private key path" >&2; exit 1; }

STALE="$(pgrep -f "ssh .*-L 8000:127.0.0.1:8000 .*${BOX##*@}" 2>/dev/null | grep -v "^$$\$" || true)"
if [[ -n "$STALE" ]]; then
  echo "  closing $(echo "$STALE" | wc -l | tr -d ' ') existing tunnel(s) to ${BOX##*@}"
  # shellcheck disable=SC2086
  kill $STALE 2>/dev/null || true
  sleep 1
fi

exec ssh -i "$SSH_KEY" \
  -p "$SSH_PORT" \
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
  -L 31490:127.0.0.1:31490 \
  -L 31495:127.0.0.1:31495 \
  -t "$BOX" \
  'mkdir -p ~/dodona; cd ~/dodona; exec bash -l'
