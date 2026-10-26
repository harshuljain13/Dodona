#!/usr/bin/env bash
# Install and start the real DCGM exporter. Run on: the GPU box.
#
#   bash infra/runpod-cluster/setup/dcgm.sh          install, start, report
#   bash infra/runpod-cluster/setup/dcgm.sh --probe  report only, change nothing
#
# WHY, given we already have an exporter:
#
# `observability/gpu_exporter.py` reads `nvidia-smi`, which can report
# utilisation, memory, clocks, power and temperature — and nothing else.
# The numbers the roofline and bandwidth-wall arguments rest on are DCGM
# *profiling* fields, which come from a different interface entirely:
#
#   DCGM_FI_PROF_PIPE_TENSOR_ACTIVE   share of cycles the tensor cores ran
#   DCGM_FI_PROF_DRAM_ACTIVE          share of cycles HBM was moving bytes
#   DCGM_FI_PROF_NVLINK_TX_BYTES      what a tensor-parallel split would cost
#   DCGM_FI_PROF_PCIE_TX_BYTES        host transfer, when NVLink is absent
#
# Those four are the difference between saying "decode is memory-bound" and
# showing it: compute low while DRAM_ACTIVE is high IS the bandwidth wall, on
# your own card, in a panel.
#
# THE CATCH, AND WHY THIS SCRIPT REPORTS RATHER THAN ASSUMES:
#
# Profiling is gated by a driver parameter set on the HOST —
# `NVreg_RestrictProfilingToAdminUsers`. On a rented container you do not own
# the host, so it may simply be unavailable however root you are inside. There
# is no way to know but to ask the driver, so this asks, prints the answer, and
# leaves the nvidia-smi exporter in place when the answer is no.
#
# A silent fallback would be worse than no attempt: four empty panels that look
# like a broken scrape, on the one screen whose argument needs them.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

PROBE_ONLY=0
[[ "${1:-}" == "--probe" ]] && PROBE_ONLY=1

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
bad()  { printf '    \033[1;31m%s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32m%s\033[0m\n' "$*"; }

command -v nvidia-smi >/dev/null 2>&1 || { bad "no nvidia-smi — this runs on the GPU box"; exit 1; }

# ── is DCGM present at all? ────────────────────────────────────────────────
say "DCGM"
if command -v dcgmi >/dev/null 2>&1; then
  note "dcgmi already installed: $(dcgmi --version 2>/dev/null | head -1)"
elif (( PROBE_ONLY )); then
  note "dcgmi not installed (probe only, not installing)"
  exit 2
else
  note "installing DCGM (apt, a few minutes)"

  DISTRO="$(. /etc/os-release && echo "${ID}${VERSION_ID//./}")"
  ARCH="$(uname -m)"; [[ "$ARCH" == "aarch64" ]] && ARCH="sbsa"
  note "distro ${DISTRO}  arch ${ARCH}"

  # Failures here are PRINTED. The first version sent apt to /dev/null and
  # reported "could not install" with no reason, which is unactionable — and
  # is the exact habit that has cost this project hours today.
  if ! dpkg -l cuda-keyring 2>/dev/null | grep -q '^ii'; then
    KEYRING_URL="https://developer.download.nvidia.com/compute/cuda/repos/${DISTRO}/${ARCH}/cuda-keyring_1.1-1_all.deb"
    note "fetching $KEYRING_URL"
    if curl -fsSL -o /tmp/cuda-keyring.deb "$KEYRING_URL"; then
      dpkg -i /tmp/cuda-keyring.deb 2>&1 | tail -2 | sed 's/^/      /'
    else
      bad "no CUDA repo published for ${DISTRO}/${ARCH}"
      note "this image is not one NVIDIA ships DCGM for — see"
      note "https://developer.download.nvidia.com/compute/cuda/repos/"
    fi
  fi

  note "apt-get update"
  apt-get update 2>&1 | tail -3 | sed 's/^/      /'

  # NVIDIA renamed this across major versions and which name exists depends on
  # the repo generation the image is pinned to. Try each, and say which took.
  INSTALLED=""
  for pkg in datacenter-gpu-manager-4-cuda12 datacenter-gpu-manager-4-core datacenter-gpu-manager; do
    note "trying $pkg"
    apt-get install -y "$pkg" 2>&1 | tail -4 | sed 's/^/      /'
    if command -v dcgmi >/dev/null 2>&1 || command -v nv-hostengine >/dev/null 2>&1; then
      INSTALLED="$pkg"; break
    fi
  done

  if [[ -z "$INSTALLED" ]]; then
    bad "no DCGM package installed — the apt output above says why"
    note "Most likely, in order: no CUDA apt repo on this image; apt could not"
    note "reach it from this pod; or the package is named differently here."
    note ""
    note "Not fatal. The fleet serves identically on the nvidia-smi exporter —"
    note "you lose the two roofline panels, and they are NOT generated rather"
    note "than left blank."
    exit 1
  fi
  ok "installed $INSTALLED"
fi

# ── the host engine, which owns the GPU handles ────────────────────────────
if ! pgrep -x nv-hostengine >/dev/null 2>&1; then
  if (( PROBE_ONLY )); then
    note "nv-hostengine not running (probe only)"
  else
    note "starting nv-hostengine"
    nv-hostengine >/dev/null 2>&1 || true
    sleep 2
  fi
fi

# ── the question that decides everything ───────────────────────────────────
# `dcgmi dmon` on a profiling field either returns numbers or refuses. Asking
# is the only way to find out: the driver parameter lives on the host.
say "can this box read profiling fields?"
PROF_OK=0
if command -v dcgmi >/dev/null 2>&1; then
  # 1001 = PIPE_TENSOR_ACTIVE, 1005 = DRAM_ACTIVE
  out="$(timeout 20 dcgmi dmon -e 1001,1005 -c 2 2>&1 || true)"
  if printf '%s' "$out" | grep -qE '^\s*GPU [0-9]'; then
    PROF_OK=1
    ok "yes — tensor-core and DRAM activity are readable"
    printf '%s\n' "$out" | tail -3 | sed 's/^/      /'
  else
    bad "no — the driver refused the profiling fields"
    printf '%s' "$out" | grep -iE "permission|not supported|profiling|error" | head -2 | sed 's/^/      /'
    note "NVreg_RestrictProfilingToAdminUsers is set on the HOST, which a"
    note "rented container cannot change. This is a property of the box, not"
    note "a mistake here."
  fi
fi

# ── what the fleet should use ──────────────────────────────────────────────
say "verdict"
if (( PROF_OK )); then
  ok "use dcgm-exporter: the roofline and bandwidth-wall panels will have data"
  note "workers.sh picks this up on the next \`make fleet-runpod\`"
else
  note "keep observability/gpu_exporter.py (nvidia-smi)"
  note "utilisation, memory, clocks, power and temperature all still work."
  note "The four profiling panels are NOT generated, so you get no blank"
  note "graphs — the hardware screen shows what this box can actually report."
fi
exit $(( PROF_OK ? 0 : 3 ))
