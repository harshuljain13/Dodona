#!/usr/bin/env bash
# One command. Run on: your Mac.
#
#   ./up.sh              sync -> setup -> serve -> gate checks -> Open WebUI -> tunnel
#   ./up.sh --stage 0    stop after the engine is serving and the gate has run
#   ./up.sh --no-tunnel  do everything, but do not hold the terminal open
#   ./up.sh --down       stop the engine and Open WebUI on the box
#   ./up.sh --logs       tail the engine log
#
# Everything it does remotely is one of the scripts in cluster/setup/, run over
# ssh. Nothing here is a second implementation of those — if a step is wrong,
# fix the script, not this file.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

STAGE=1 TUNNEL=1 ACTION=up
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage)     STAGE="$2"; shift 2 ;;
    --no-tunnel) TUNNEL=0; shift ;;
    --down)      ACTION=down; shift ;;
    --logs)      ACTION=logs; shift ;;
    -h|--help)   sed -n '2,12p' "$0"; exit 0 ;;
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
    "${SSH[@]}" "$LAMBDA" 'pkill -f "vllm serve" || true; docker rm -f open-webui 2>/dev/null || true; echo stopped'
    echo "The GPU box is still running and still billing. Stop the instance in Lambda's console."
    exit 0 ;;
  logs)
    exec "${SSH[@]}" "$LAMBDA" 'tail -f ~/dodona/engine.log' ;;
esac

# ---- 1. ship the code ---------------------------------------------------
step "sync  —  Mac → $LAMBDA:~/dodona"
bash cluster/setup/sync_to_lambda.sh

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
"${SSH[@]}" "$LAMBDA" "cd ~/dodona && pkill -f 'vllm serve' || true; \
  source .venv/bin/activate && \
  LOCAL_MODEL='$MODEL' nohup bash cluster/setup/lambda_vllm.sh > engine.log 2>&1 & echo started"

printf '    waiting for the engine'
for i in $(seq 1 180); do
  if "${SSH[@]}" "$LAMBDA" 'curl -sf localhost:8000/v1/models >/dev/null' 2>/dev/null; then
    printf ' ready (%ss)\n' "$((i*5))"; break
  fi
  [[ $i -eq 180 ]] && { printf '\n'; echo "Engine did not answer in 15 min. ./up.sh --logs" >&2; exit 1; }
  printf '.'; sleep 5
done

# ---- 5. the gate --------------------------------------------------------
step "gate  —  the four checks from COMMANDS.md step 5"
"${SSH[@]}" "$LAMBDA" "cd ~/dodona && python3 - <<'PY'
import json, glob, os, urllib.request

def ok(c, label, detail=''):
    print(f\"    [{'PASS' if c else 'FAIL'}] {label}\" + (f'  {detail}' if detail else ''))
    return c

try:
    urllib.request.urlopen('http://localhost:8000/v1/models', timeout=10).read()
    ok(True, '5a  serves')
except Exception as e:
    ok(False, '5a  serves', str(e))

m = urllib.request.urlopen('http://localhost:8000/metrics', timeout=10).read().decode()
ok('gpu_cache_usage_perc' in m, '5b  gpu_cache_usage_perc present')

g = glob.glob(os.path.expanduser('~/.cache/huggingface/hub/models--*AWQ*/snapshots/*/config.json')) \\
  + glob.glob(os.path.expanduser('~/.cache/huggingface/hub/models--*/snapshots/*/config.json'))
if g:
    c = json.load(open(g[0]))
    L, KV = c['num_hidden_layers'], c['num_key_value_heads']
    HD = c.get('head_dim') or c['hidden_size'] // c['num_attention_heads']
    kib = 2 * L * KV * HD * 2 / 1024
    ok(abs(kib - 56) < 2.8, '5c  kv_bytes_per_token (P6)',
       f'layers={L} kv_heads={KV} head_dim={HD} -> {kib:.0f} KiB/token (predicted 56)')
else:
    ok(False, '5c  kv_bytes_per_token (P6)', 'config.json not found in the HF cache')
PY"
echo "    5d  weights / KV split:"
"${SSH[@]}" "$LAMBDA" "grep -iE 'KV cache|model weights|GPU blocks|Marlin|awq' ~/dodona/engine.log | tail -6" | sed 's/^/        /' || true

[[ "$STAGE" == "0" ]] && { step "stage 0 done"; echo "    ./up.sh          continue to Open WebUI"; echo "    ./up.sh --logs   engine log"; exit 0; }

# ---- 6. Open WebUI ------------------------------------------------------
step "Open WebUI  —  :3000"
"${SSH[@]}" "$LAMBDA" "docker rm -f open-webui >/dev/null 2>&1 || true; \
  docker run -d --name open-webui -p 3000:8080 \
    -e OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1 \
    -e OPENAI_API_KEY=dummy -e WEBUI_AUTH=False \
    --add-host=host.docker.internal:host-gateway \
    -v open-webui:/app/backend/data \
    ghcr.io/open-webui/open-webui:main >/dev/null && echo '    started'"

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
  echo "    bash cluster/setup/ssh.sh   to open the tunnel"
fi
