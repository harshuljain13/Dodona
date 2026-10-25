#!/usr/bin/env bash
# Copy the whole project from your Mac to ~/dodona on the GPU box, over rsync/SSH.
# Run on: Mac. Reads BOX, SSH_KEY and SSH_PORT from .env and exits 1 without them.
# Excludes .venv, __pycache__, .pytest_cache — source only, no environments.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"   # the whole project, not just infra/

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

# LAMBDA / LAMBDA_SSH_KEY are the older names and still work — the project ran
# on Lambda first, and nothing about it was Lambda-specific except these.
BOX="${BOX:-${LAMBDA:-}}"
SSH_KEY="${SSH_KEY:-${LAMBDA_SSH_KEY:-}}"
# RunPod and most container hosts put sshd on a high port, not 22.
SSH_PORT="${SSH_PORT:-22}"

if [[ -z "$BOX" || -z "$SSH_KEY" || ! -f "$SSH_KEY" ]]; then
  echo "Set BOX and SSH_KEY in .env" >&2
  exit 1
fi

echo "Sync Mac → $BOX:~/dodona/  (port $SSH_PORT)"
ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new "$BOX" "mkdir -p ~/dodona"
# --delete, so the box mirrors the repo rather than accumulating it.
#
# Without it a rename leaves both trees behind: infra/cluster survived
# alongside infra/lambda-cluster, and the running Grafana kept provisioning
# from the old path — serving eleven dashboards out of a directory the repo no
# longer has. A stale file that still works is worse than a missing one.
#
# The --exclude list is protected: rsync will not delete an excluded path, so
# .venv, .env and metrics/ on the box survive.
rsync -avz --delete -e "ssh -i $SSH_KEY -p $SSH_PORT -o StrictHostKeyChecking=accept-new" \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.env' \
  --exclude '.git' \
  --exclude 'metrics/*' \
  ./ \
  "$BOX:~/dodona/"
echo "Synced."
