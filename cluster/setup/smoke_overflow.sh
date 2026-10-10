#!/usr/bin/env bash
# Smoke-test the burst path against the real overflow provider.
# Exits 0 immediately when OVERFLOW_BASE_URL is unset, so it is safe to run blind.
# Warning: when that is set this spends money — it calls the external endpoint.
set -euo pipefail

if [[ -z "${OVERFLOW_BASE_URL:-}" ]]; then
  echo "OVERFLOW_BASE_URL unset — skip"
  exit 0
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export TRACE_PATH="${TRACE_PATH:-$ROOT/traces/requests.jsonl}"
exec python "$ROOT/setup/overflow_smoke.py"
