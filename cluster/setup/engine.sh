#!/usr/bin/env bash
# Serve one model with vLLM: a single worker on the whole card, no slices.
# Run on: GPU box, inside the venv from box_packages.sh.
#
# Every default here appears in the capacity arithmetic — see
# docs/project/01-capacity.md. Override any of them from the environment:
#
#   LOCAL_MODEL     the model. int4 is forced on sm80 (no fp8 tensor cores)
#   MAX_MODEL_LEN   16384. At 8192 the decode pool is 22, not 11
#   MAX_NUM_SEQS    64, deliberately ABOVE the KV limit of 5.5, so the cache
#                   runs out before the scheduler does. Lower it and a
#                   KV-pressure policy can never fire
#   UTIL            0.90. At the whole card this is 0.90 of 40 GiB; on a slice it
#                   is 0.90 of the HAMi slice, which is a 3x difference
#   SCHEDULING_POLICY  fcfs. The lab used `priority`, which is worth having once
#                   the gateway sets a priority per request. At
#                   0 nothing sets one, so fcfs is the honest default
set -euo pipefail

MODEL="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"
PORT="${PORT:-8000}"

UTIL="${UTIL:-0.90}"
CAP="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"
MEM_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
echo "GPU ${MEM_MIB} MiB, compute capability ${CAP}, util ${UTIL}"
# sm80 has no fp8 tensor cores, which is why the model is int4 and not fp8.
# A card reporting 8.9 or 9.0 changes that, and the capacity chain should be redone.
case "$CAP" in
  8.0) : ;;
  "")  echo "WARN: could not read compute capability — is nvidia-smi present?" >&2 ;;
  *)   echo "NOTE: compute capability ${CAP} is not sm80. fp8 is available here, so int4" >&2
       echo "      is no longer forced — see docs/project/01-capacity.md step 4." >&2 ;;
esac

exec vllm serve "$MODEL" \
  --host 0.0.0.0 \
  --port "$PORT" \
  --max-model-len "${MAX_MODEL_LEN:-16384}" \
  --max-num-seqs "${MAX_NUM_SEQS:-64}" \
  --enable-prefix-caching \
  --scheduling-policy "${SCHEDULING_POLICY:-fcfs}" \
  --gpu-memory-utilization "$UTIL" \
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE:-1}" \
  --pipeline-parallel-size "${PIPELINE_PARALLEL_SIZE:-1}" \
  --data-parallel-size "${DATA_PARALLEL_SIZE:-1}" \
  ${SPECULATIVE_CONFIG:+--speculative-config "$SPECULATIVE_CONFIG"}
