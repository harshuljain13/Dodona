#!/usr/bin/env bash
# Serve one model with vLLM, sized to whichever GPU is present.
# Run on: GPU box. Picks gpu-memory-utilization 0.85 on cards of 50 GiB or less,
# 0.34 on larger ones. Prefix caching and priority scheduling are on.
set -euo pipefail

MODEL="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct}"
PORT="${PORT:-8000}"

if [[ -z "${UTIL:-}" ]]; then
  MEM_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ' || echo 81920)"
  if [[ "${MEM_MIB:-0}" -le 50000 ]]; then
    UTIL=0.85
    echo "GPU ${MEM_MIB} MiB → UTIL=0.85 so ${MODEL} fits. D5 0.34 is the 80GiB paper slice."
  else
    UTIL=0.34
    echo "GPU ${MEM_MIB} MiB → UTIL=0.34 (D5 slice, not 0.95)."
  fi
fi

exec vllm serve "$MODEL" \
  --host 0.0.0.0 \
  --port "$PORT" \
  --enable-prefix-caching \
  --scheduling-policy priority \
  --gpu-memory-utilization "$UTIL" \
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE:-1}" \
  --pipeline-parallel-size "${PIPELINE_PARALLEL_SIZE:-1}" \
  --data-parallel-size "${DATA_PARALLEL_SIZE:-1}" \
  ${SPECULATIVE_CONFIG:+--speculative-config "$SPECULATIVE_CONFIG"}
