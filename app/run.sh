#!/usr/bin/env bash
# The voice app: the AgentShip service, and the page that talks to it.
#
#   make app
#
# Two processes, both on this laptop. The microphone, the recogniser, the voice
# and the agent all run here; the only thing that leaves the machine is the
# completion, and it goes to the gateway on 127.0.0.1:8080 — through guard,
# admit, place and queue — because a completion that skips the serve path tells
# us nothing about the infrastructure this project exists to measure.
#
# There is no LiveKit and no container. WebRTC earns its keep across a network,
# and this audio never leaves the host; what it cost was an ICE/media path to
# get wrong and a spec that only half applied.
#
# Job control is on so each background job gets its own process group: `next
# dev` spawns children, and killing the subshell alone leaves :3000 bound.
set -euo pipefail
set -m

cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

: "${DEEPGRAM_API_KEY:?set DEEPGRAM_API_KEY in .env — see app/README.md}"

# Not a credential, and not something to ask anyone for. The agent reaches our
# own gateway through LiteLLM's `openai` provider because the gateway speaks the
# OpenAI wire format, and LiteLLM refuses to call any openai/* model without a
# key even when the endpoint is ours and ignores it. Unset, the agent's first
# turn dies with "No API credentials for model 'openai/Qwen...'", which reads as
# a missing cloud account. The gateway checks nothing, so any string does.
export OPENAI_API_KEY="${OPENAI_API_KEY:-not-used-the-gateway-is-ours}"

AGENTSHIP_PORT="${AGENTSHIP_PORT:-8090}"   # 8000 is the whole-card engine, 8080 the gateway
say() { printf '\033[1;36m==> %s\033[0m\n' "$1"; }

# A previous run that was killed rather than stopped leaves these bound, and
# both servers then fail at once with EADDRINUSE — two stack traces for one
# cause, neither naming the other process. Reclaim our own leftovers; refuse to
# touch anything else, because :3000 is a popular port and guessing whose it is
# would kill someone's unrelated dev server.
reclaim() {
  local port="$1" pid
  pid="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1)" || true
  [[ -z $pid ]] && return 0
  local cmd here
  cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  # The working directory, not the name. Next renames itself to
  # "next-server (v15.5.28)" the moment it finishes booting, so matching on
  # "next dev" finds it for about one second and never again — and matching on
  # "next-server" alone would kill an unrelated project's dev server on :3000.
  # Ours is the one running inside this repo.
  here="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
  if [[ $here == "$PWD"* ]] &&
     [[ $cmd == *"agentship serve"* || $cmd == *"next-server"* || $cmd == *"next dev"* || $cmd == *"app/run.sh"* ]]; then
    say "port $port  —  held by an earlier 'make app' (pid $pid), stopping it"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 20); do lsof -nP -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || return 0; sleep 0.25; done
    kill -9 "$pid" 2>/dev/null || true
    return 0
  fi
  echo "    Port $port is in use by something that is not Dodona:"
  echo "      pid $pid  ${cmd:-unknown}"
  echo "    Stop it, or set a different port (AGENTSHIP_PORT=... for the service)."
  exit 1
}
reclaim 3000
reclaim "$AGENTSHIP_PORT"

if ! curl -sf -m 2 http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
  echo "    WARNING: the gateway is not answering on :8080."
  echo "    Open the tunnel first, or the agent will have nothing to think with:"
  echo "      make fleet-runpod-ssh   (rented box)   |   make ssh   (kubernetes)"
fi

# Fresh clone: the app's deps are in requirements-app.txt, not requirements.txt,
# so `make fleet-lambda` has not installed them. Same for the UI's node_modules.
# Install rather than complain — `make fleet-lambda` builds its venv the same way.
[[ -d .venv ]] || python3 -m venv .venv
if ! .venv/bin/python -c 'import agentship_voice, pipecat' 2>/dev/null; then
  say "python deps  —  requirements-app.txt, first run only"
  .venv/bin/pip install -qU pip && .venv/bin/pip install -qr requirements-app.txt
fi
if [[ ! -d app/web/node_modules ]]; then
  say "node deps  —  first run only"
  (cd app/web && npm install --no-fund --no-audit)
fi

# A dev key for a service bound to loopback. Minted per run rather than stored:
# it authenticates the browser on this laptop to the agent on this laptop, and
# a value that never outlives the process cannot be leaked by committing it.
# The scope is the narrowest that works — invoke, this agent, nothing else.
export DODONA_DEV_KEY="${DODONA_DEV_KEY:-$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))')}"
export AGENTSHIP_API_KEYS="[{\"key\":\"$DODONA_DEV_KEY\",\"user\":\"listener\",\"scopes\":[\"agent:dodona:invoke\"]}]"
export AGENTSHIP_PORT
export NEXT_PUBLIC_DODONA_PORT="$AGENTSHIP_PORT"

cleanup() {
  say "stopping"
  # Negative PID: the whole process group. `next dev` runs its server as a
  # child, and killing only the shell it was started from leaves :3000 bound,
  # so the next `make app` fails on a port that nothing visible is using.
  for pid in "${SERVICE_PID:-}" "${WEB_PID:-}"; do
    [[ -n $pid ]] && kill -TERM "-$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

# --allow-in-memory-durability because `durability: checkpoint` with no session
# store would otherwise refuse to start. Correct for a demo, wrong for a run you
# intend to cite: set AGENT_SESSION_STORE_URI and drop the flag.
say "AgentShip  —  127.0.0.1:$AGENTSHIP_PORT  (agent + ears + mouth)"
.venv/bin/agentship serve \
  --agents-dir app \
  --port "$AGENTSHIP_PORT" \
  --allow-in-memory-durability &
SERVICE_PID=$!

# Doctor-gated: an invalid spec or a missing provider key fails before the
# socket binds, so if it is going to die it dies now rather than on first click.
for _ in $(seq 40); do
  curl -sf -m 1 "http://127.0.0.1:$AGENTSHIP_PORT/healthz" >/dev/null 2>&1 && break
  kill -0 "$SERVICE_PID" 2>/dev/null || { echo "    AgentShip exited during startup — see above."; exit 1; }
  sleep 0.5
done

say "UI  —  http://localhost:3000"
(cd app/web && npm run dev) &
WEB_PID=$!

wait
