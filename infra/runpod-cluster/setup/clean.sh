#!/usr/bin/env bash
# Wipe the box back to a bare machine. Run on: the GPU box.
#
#   make clean                  stop everything, remove what we installed
#   make clean ARGS=--purge-models   and drop the downloaded weights too
#
# In this order, because each step frees what the next needs:
#   1. the fleet        supervisors first, so nothing restarts under us
#   2. our artifacts    venv, logs, prometheus tsdb, grafana db, run bundles
#   3. the binaries     prometheus and grafana, if box_packages.sh put them on PATH
#
# KEEPS the HuggingFace cache by default. A 32B AWQ is ~19 GB and lives on disk,
# not on the GPU, so re-downloading it buys nothing and costs a long wait at the
# start of the next bring-up. `--purge-models` drops it when you are handing the
# box back.
#
# The Kubernetes version of this file also tore down k3s, helm releases and
# containerd. There is none of that here — this path installs processes, so
# cleaning up means stopping processes and deleting directories.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

PURGE_MODELS=0
for arg in "$@"; do
  [[ "$arg" == "--purge-models" ]] && PURGE_MODELS=1
done

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

# ── 1. the fleet ───────────────────────────────────────────────────────────
# Reuses down.sh rather than repeating its kill list, which drifted out of date
# the one time it was duplicated: down.sh matches by filename, so it can still
# stop processes started under an older directory layout.
say "stopping the fleet"
if [[ -f "$ROOT/infra/runpod-cluster/setup/down.sh" ]]; then
  bash "$ROOT/infra/runpod-cluster/setup/down.sh" || true
else
  note "down.sh is missing — killing by pattern instead"
  pkill -f "[v]llm serve" || true
  pkill -f "[g]ateway.serve" || true
fi

# ── 2. our artifacts ───────────────────────────────────────────────────────
# Named explicitly. A loop over infra/ would also delete the scripts that are
# about to be re-synced, and `rm -rf` on a variable that could be empty is how
# a cleanup script becomes an incident.
say "removing what we wrote"
for target in \
  "$ROOT/.venv" \
  "$ROOT/infra/metrics/local" \
  "$ROOT/infra/metrics/traces" \
; do
  if [[ -e "$target" ]]; then
    note "$(du -sh "$target" 2>/dev/null | cut -f1) $target"
    rm -rf "$target"
  fi
done

# Run bundles are measurements, so they are listed rather than assumed junk —
# `make load` rsyncs them back to the Mac, but only if you ran it.
bundles=$(find "$ROOT/infra/metrics" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
if [[ "${bundles:-0}" -gt 0 ]]; then
  note "$bundles run bundle(s) left under infra/metrics/ — `make load` pulls them back"
fi

# ── 3. the binaries ────────────────────────────────────────────────────────
say "removing prometheus and grafana"
for path in /usr/local/bin/prometheus /usr/local/bin/promtool /usr/local/bin/grafana \
            /usr/local/bin/grafana-server /opt/observability; do
  if [[ -e "$path" ]]; then
    note "$path"
    rm -rf "$path"
  fi
done

# ── the weights ────────────────────────────────────────────────────────────
if (( PURGE_MODELS )); then
  say "removing the HuggingFace cache"
  note "$(du -sh ~/.cache/huggingface 2>/dev/null | cut -f1 || echo '0') freed"
  rm -rf ~/.cache/huggingface
else
  say "keeping the HuggingFace cache"
  note "$(du -sh ~/.cache/huggingface 2>/dev/null | cut -f1 || echo 'empty') — --purge-models to drop it"
fi

echo
say "clean"
note "The box is still billing. Stop the pod in your provider's console."
