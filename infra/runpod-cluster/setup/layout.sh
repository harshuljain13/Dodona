#!/usr/bin/env bash
# The shape of the fleet, in one place.
#
# Sourced — not executed — by workers.sh, status.sh and smoke.sh. It exists
# because those three disagreed: workers.sh learned to start a worker per card,
# and the other two went on asking for `prefill decode-0 decode-1` on fixed
# ports. A status command that invents workers, and a smoke test that passes
# because it checked the wrong three ports, are both worse than nothing.
#
# Sets, from the box and the environment:
#
#   GPUS           cards nvidia-smi reports
#   GPU_MIB[]      total memory per card
#   LAB_TOPOLOGY   aggregated (default) | disaggregated
#   WORKER_COUNT   how many engines this fleet runs
#   NAMES[]        what each is called — matches pools.py's worker ids
#   DEVICES[]      which card each is pinned to
#   PORTS[]        which port each listens on
#
# Callers source this AFTER .env, so anything set there wins.

# ── the cards ──────────────────────────────────────────────────────────────
if command -v nvidia-smi >/dev/null 2>&1; then
  mapfile -t GPU_MIB < <(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | tr -d ' ')
else
  GPU_MIB=()
fi
GPUS=${#GPU_MIB[@]}

# ── which topology, and therefore which workers ────────────────────────────
#
# `aggregated` is the default, and the reason is that the hop the split would
# orchestrate does not exist on this path. A real prefill/decode split needs
# three things — placement, orchestration, transfer — and transfer lives inside
# vLLM behind --kv-transfer-config, which names an RDMA device and a cluster DNS
# name, so it cannot be passed on a rented box. Without it decode re-prefills
# the whole prompt: the prefill worker burns a forward pass whose result is
# discarded, and one answer costs two prefills. See docs/project/topology.md.
#
# Aggregated instead gives every card a full engine and lets the router choose
# between them, which makes placement a real decision, prefix-sticky routing a
# real saving, and admission control the thing the fleet demonstrates.
LAB_TOPOLOGY="${LAB_TOPOLOGY:-aggregated}"

NAMES=(); DEVICES=()
if [[ "$LAB_TOPOLOGY" == "aggregated" ]]; then
  # One engine per card. Two is the floor for `place()` to be a choice rather
  # than a formality; one card still works and is honest about being one.
  WORKER_COUNT=$(( GPUS > 0 ? GPUS : 1 ))
  for _li in $(seq 0 $(( WORKER_COUNT - 1 ))); do
    NAMES+=("engine-$_li")
    DEVICES+=("$_li")
  done
else
  # Three slots regardless of cards: fewer cards means sharing, which is what
  # HAMi used to arbitrate — here it is just two processes on one device.
  WORKER_COUNT=3
  NAMES=(prefill decode-0 decode-1)
  case "$GPUS" in
    0|1) DEVICES=(0 0 0) ;;
    2)   DEVICES=(0 1 1) ;;
    *)   DEVICES=(0 1 2) ;;
  esac
fi

# ── ports ──────────────────────────────────────────────────────────────────
# 8100 rather than 8000: RunPod's Jupyter template runs nginx on :8001, which
# would be the second worker's port if the block started at 8000. Every
# attendee on that template would hit it, and vLLM's failure mode there is
# "Address already in use" on a loop while the health check reports ready,
# because nginx answers. PORT_BASE moves the whole block.
PORT_BASE="${PORT_BASE:-8100}"
PORTS=()
for _li in $(seq 0 $(( WORKER_COUNT - 1 ))); do
  PORTS+=($(( PORT_BASE + _li )))
done

# `phase` for a worker, as the prometheus labels and dashboards use it. `both`
# is the honest answer for an aggregated engine: labelling a full engine
# "decode" to keep a dashboard panel company is how a graph starts lying.
phase_of() {
  case "$1" in
    prefill*) printf 'prefill' ;;
    decode*)  printf 'decode' ;;
    *)        printf 'both' ;;
  esac
}
