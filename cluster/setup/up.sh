#!/usr/bin/env bash
# One command. Run on: your Mac.
#
# Driven by the Makefile — `make engine`, `make cluster`, `make observability`,
# `make all`. Run it directly only if you want a flag the Makefile does not map.
#
#   --stage 0|1|2|3   how far to go   (1 = engine + WebUI + tunnel)
#   --only            just that stage, skip the ones before it
#   --gate            run the four checks against a box that is already up
#   --no-tunnel       do the work, give the terminal back
#   --logs  --down
#   ./up.sh --no-tunnel  do everything, but do not hold the terminal open
#   ./up.sh --down       stop the engine and Open WebUI on the box
#   ./up.sh --logs       tail the engine log
#
# Everything it does remotely is one of the scripts in cluster/setup/, run over
# ssh. Nothing here is a second implementation of those — if a step is wrong,
# fix the script, not this file.
# Bash, not sh. This uses arrays and [[ ]]; under dash the array assignment
# below is a syntax error at line ~40 and the message points nowhere useful.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "up.sh needs bash:  bash cluster/setup/up.sh   (or just: make engine)" >&2
  exit 1
fi
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # project root
cd "$ROOT"

STAGE=1 TUNNEL=1 ACTION=up ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage)     STAGE="$2"; shift 2 ;;
    --only)      ONLY=1; shift ;;
    --gate)      ACTION=gate; shift ;;
    --no-tunnel) TUNNEL=0; shift ;;
    --down)      ACTION=down; shift ;;
    --logs)      ACTION=logs; shift ;;
    -h|--help)   sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# ---- .env ---------------------------------------------------------------
if [[ ! -f .env ]]; then
  echo "No .env. Copy it and fill in the box:" >&2
  echo "    cp .env.example .env" >&2
  exit 1
fi
set -a; source .env; set +a
: "${LAMBDA:?set LAMBDA in .env, e.g. ubuntu@203.0.113.4}"
: "${LAMBDA_SSH_KEY:?set LAMBDA_SSH_KEY in .env}"
[[ -f "$LAMBDA_SSH_KEY" ]] || { echo "No such key: $LAMBDA_SSH_KEY" >&2; exit 1; }

SSH=(ssh -i "$LAMBDA_SSH_KEY" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
MODEL="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

case "$ACTION" in
  down)
    step "stopping the engine and Open WebUI"
    "${SSH[@]}" "$LAMBDA" '{ pkill -f "[v]llm serve" || true; }; docker rm -f open-webui 2>/dev/null || true; echo stopped'
    echo "The GPU box is still running and still billing. Stop the instance in Lambda's console."
    exit 0 ;;
  logs)
    exec "${SSH[@]}" "$LAMBDA" 'tail -f ~/dodona/engine.log' ;;
  gate)
    # Sync first: --gate runs the copy ON THE BOX, and a stale one silently
    # checks the wrong thing. Cheap, and it is the whole point of the target.
    rsync -az -e "ssh -i $LAMBDA_SSH_KEY -o StrictHostKeyChecking=accept-new" \
      cluster/setup/gate.py "$LAMBDA:~/dodona/cluster/setup/gate.py"
    exec "${SSH[@]}" "$LAMBDA" 'cd ~/dodona && python3 cluster/setup/gate.py --log ~/dodona/engine.log' ;;
esac

# ---- 1. ship the code ---------------------------------------------------
step "sync  —  Mac → $LAMBDA:~/dodona"
bash cluster/setup/sync_to_lambda.sh

if [[ "$ONLY" == "1" ]]; then
  [[ "$STAGE" -ge 2 ]] && { step "cluster  —  k3s + HAMi, one prefill + two decode"
    "${SSH[@]}" "$LAMBDA" '{ pkill -f "[v]llm serve" || true; }; sleep 3'
    "${SSH[@]}" -t "$LAMBDA" 'cd ~/dodona && bash cluster/setup/lambda_cluster.sh'; }
  [[ "$STAGE" -ge 3 ]] && { step "observability  —  Prometheus + Grafana + DCGM"
    "${SSH[@]}" -t "$LAMBDA" 'cd ~/dodona && bash cluster/setup/day2_observability.sh'; }
  step "done"; echo "    make ssh   to open the tunnel"; exit 0
fi

# ---- 2. what card is it? ------------------------------------------------
step "card"
"${SSH[@]}" "$LAMBDA" 'nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader' | sed 's/^/    /'
CAP=$("${SSH[@]}" "$LAMBDA" 'nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d " "')
if [[ "$CAP" != "8.0" ]]; then
  echo "    NOTE: compute capability $CAP is not sm80. fp8 is available on this card, so int4"
  echo "          is no longer forced — docs/project/01-capacity.md step 4 needs redoing."
fi

# ---- 3. venv, requirements, vllm ----------------------------------------
step "setup  —  venv, requirements, vllm, import check"
"${SSH[@]}" -t "$LAMBDA" 'cd ~/dodona && bash cluster/setup/lambda_setup.sh'

# ---- 4. serve, detached so it survives the ssh session ------------------
step "serve  —  $MODEL on :8000"
# pkill -f '[v]llm serve' — the bracket stops the pattern matching the command
# line it is sitting in. Without it pkill kills its own remote shell, the
# connection drops, and ssh returns 255 with nothing useful in the log.
"${SSH[@]}" "$LAMBDA" "cd ~/dodona && { pkill -f '[v]llm serve' || true; }; sleep 2; \
  source .venv/bin/activate && \
  LOCAL_MODEL='$MODEL' setsid nohup bash cluster/setup/lambda_vllm.sh </dev/null >engine.log 2>&1 & \
  sleep 1; echo '    started, logging to ~/dodona/engine.log'"

printf '    waiting for the engine'
for i in $(seq 1 180); do
  if "${SSH[@]}" "$LAMBDA" 'curl -sf localhost:8000/v1/models >/dev/null' 2>/dev/null; then
    printf ' ready (%ss)\n' "$((i*5))"; break
  fi
  [[ $i -eq 180 ]] && { printf '\n'; echo "Engine did not answer in 15 min. make logs" >&2; exit 1; }
  printf '.'; sleep 5
done

# ---- 5. the gate --------------------------------------------------------
step "gate  —  the four stage-0 checks"
"${SSH[@]}" "$LAMBDA" 'cd ~/dodona && python3 cluster/setup/gate.py --log ~/dodona/engine.log' || {
  echo
  echo "    The gate failed. 5c or 5d failing means the capacity arithmetic is wrong," >&2
  echo "    not the cluster — see docs/project/01-capacity.md before going further." >&2
  exit 1
}

[[ "$STAGE" == "0" ]] && { step "stage 0 done"; echo "    make engine   continue to Open WebUI"; echo "    make logs     engine log"; exit 0; }

# ---- 6. Open WebUI ------------------------------------------------------
step "Open WebUI  —  :3000"
"${SSH[@]}" "$LAMBDA" "docker rm -f open-webui >/dev/null 2>&1 || true; \
  docker run -d --name open-webui -p 3000:8080 \
    -e OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1 \
    -e OPENAI_API_KEY=dummy -e WEBUI_AUTH=False \
    --add-host=host.docker.internal:host-gateway \
    -v open-webui:/app/backend/data \
    ghcr.io/open-webui/open-webui:main >/dev/null && echo '    started'"

# ---- 6b. k3s + HAMi + the three slices (stage 2) ------------------------
if [[ "$STAGE" -ge 2 ]]; then
  step "cluster  —  k3s + HAMi, one prefill + two decode, applied one at a time"
  echo "    The single-worker engine from step 4 is stopped first: it holds the"
  echo "    whole card, and the three slices need all 36 GiB of it."
  "${SSH[@]}" "$LAMBDA" '{ pkill -f "[v]llm serve" || true; }; sleep 3'
  "${SSH[@]}" -t "$LAMBDA" 'cd ~/dodona && bash cluster/setup/lambda_cluster.sh'
fi

# ---- 6c. observability (stage 3) ----------------------------------------
if [[ "$STAGE" -ge 3 ]]; then
  step "observability  —  Prometheus + Grafana + DCGM"
  "${SSH[@]}" -t "$LAMBDA" 'cd ~/dodona && bash cluster/setup/day2_observability.sh'
fi

# ---- 7. tunnel ----------------------------------------------------------
if [[ "$TUNNEL" == "1" ]]; then
  step "tunnel  —  ports forwarded to this Mac. Leave this open."
  cat <<'PORTS'
    http://127.0.0.1:3000    Open WebUI   <- talk to it here
    http://127.0.0.1:8000    vLLM
    http://127.0.0.1:8080    gateway      (stage 4)
    http://127.0.0.1:31495   Grafana      (stage 3)
PORTS
  exec bash cluster/setup/ssh.sh
else
  step "up"
  echo "    make ssh   to open the tunnel"
fi
