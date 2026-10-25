#!/usr/bin/env bash
# Prepare the GPU box: venv, lab requirements, and vLLM itself.
# Run on: GPU box, first thing after ssh. Installs vllm>=0.28, then verifies
# torch and vLLM import and can see the device. No Kubernetes here.
set -euo pipefail

# This script is at infra/cluster/setup/, so the repo root is three levels up,
# not two. It was two — PROJECT_ROOT landed on infra/, pip looked for
# infra/requirements.txt, and the venv would have been built there as well,
# where workers.sh does not look for it. Found on the first real box.
PROJECT_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$PROJECT_ROOT"

# Quiet by default, with the full pip output kept in a file. vLLM pulls in
# torch and CUDA wheels — several thousand lines that bury the four `==>` lines
# around them and make a working install look like something went wrong.
# `make fleet-lambda` never shows this at all, because its pods bring vLLM in the
# image; this path installs it, and should not be noisier for that.
LOG="${PACKAGES_LOG:-$PROJECT_ROOT/infra/metrics/local/box_packages.log}"
mkdir -p "$(dirname "$LOG")"

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

step() {
  local what="$1"; shift
  printf '    %-34s' "$what"
  if "$@" >>"$LOG" 2>&1; then
    printf 'ok\n'
  else
    printf 'FAILED\n'
    echo
    echo "    last 20 lines of $LOG:"
    tail -20 "$LOG" | sed 's/^/      /'
    exit 1
  fi
}

say "packages  —  venv, requirements, vLLM"
note "full log: $LOG"

step "venv"              python3 -m venv .venv
source .venv/bin/activate
step "pip, wheel"        pip install -qU pip wheel
step "requirements.txt"  pip install -q -r requirements.txt
step "requirements-load" pip install -q -r requirements-load.txt
# The long one: torch + CUDA wheels, several GB. Worth its own line so a slow
# network looks like progress rather than a hang.
step "vllm (~10 GB)"     pip install -q "vllm>=0.28"

# Prometheus and Grafana, as binaries. The Kubernetes path gets them from Helm,
# which needs a cluster — so without this the process path had no observability
# at all, and KV pressure is something you watch rather than read about.
bash "$PROJECT_ROOT/infra/local/observability.sh" || true

say "verify"
python -c "
import torch, vllm
print('    torch', torch.__version__)
print('    cuda ', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')
print('    vllm ', vllm.__version__)
" || { echo "    import failed — see $LOG"; exit 1; }
