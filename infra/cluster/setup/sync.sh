#!/usr/bin/env bash
# Copy the whole project from your Mac to ~/dodona on the GPU box, over rsync/SSH.
# Run on: Mac. Reads LAMBDA and LAMBDA_SSH_KEY from .env and exits 1 without them.
# Excludes .venv, __pycache__, .pytest_cache — source only, no environments.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_ROOT="$(cd "$ROOT/../.." && pwd)"
cd "$PROJECT_ROOT"   # the whole project, not just infra/

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

if [[ -z "${LAMBDA:-}" || -z "${LAMBDA_SSH_KEY:-}" || ! -f "$LAMBDA_SSH_KEY" ]]; then
  echo "Set LAMBDA and LAMBDA_SSH_KEY in .env" >&2
  exit 1
fi

echo "Sync Mac → $LAMBDA:~/dodona/"
ssh -i "$LAMBDA_SSH_KEY" -o StrictHostKeyChecking=accept-new "$LAMBDA" "mkdir -p ~/dodona"
rsync -avz -e "ssh -i $LAMBDA_SSH_KEY -o StrictHostKeyChecking=accept-new" \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.env' \
  --exclude '.git' \
  --exclude 'metrics/*' \
  ./ \
  "$LAMBDA:~/dodona/"
echo "Synced."
