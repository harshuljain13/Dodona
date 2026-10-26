#!/usr/bin/env bash
# Drive the GPU box from your Mac. Normally called by the Makefile.
#
#   up.sh fleet-runpod            one vLLM per card, gateway in front, then the tunnel
#   up.sh fleet-runpod-down       stop it
#   up.sh fleet-runpod-logs       follow the detached fleet's log
#   up.sh fleet-runpod-status     what the fleet is actually doing
#   up.sh fleet-runpod-ssh        tunnel for the fleet's ports
#   up.sh fleet-runpod-smoke-test prove it works, end to end
#   up.sh dcgm                    install real DCGM, report profiling support
#   up.sh engine                  ONE worker on the whole card, for comparison only
#   up.sh down                    stop that engine
#   up.sh load [args]             run a load test ON THE BOX, pull the bundle back
#   up.sh clean                   wipe the box back to bare metal
#
# Every remote step is one of the scripts next to this one, run over ssh. This
# file only sequences them — if a step is wrong, fix that script, not this one.

# Bash, not sh: arrays and [[ ]]. Under dash load_env is a syntax error.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "up.sh needs bash:  bash infra/setup/up.sh fleet-runpod   (or: make fleet-runpod)" >&2
  exit 1
fi
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # repo root, above infra/
cd "$PROJECT_ROOT"

REMOTE_DIR="dodona"        # where sync.sh puts the project on the box
ENGINE_LOG="engine.log"    # inside REMOTE_DIR
ENGINE_WAIT_TRIES=180      # x5s = 15 minutes, for weights plus CUDA graphs

announce() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail()     { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# ── the box ────────────────────────────────────────────────────────────────
# LAMBDA / LAMBDA_SSH_KEY are the older names and still work.
load_env() {
  [[ -f .env ]] || fail "No .env. Run:  cp .env.example .env   then set BOX and SSH_KEY"
  set -a; source .env; set +a

  BOX="${BOX:-${LAMBDA:-}}"
  SSH_KEY="${SSH_KEY:-${LAMBDA_SSH_KEY:-}}"
  # RunPod and most container hosts put sshd on a high port, not 22. Exported so
  # sync.sh and tunnel.sh, which are separate processes, see the same value.
  export SSH_PORT="${SSH_PORT:-22}"
  MODEL="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"

  [[ -n "$BOX"     ]] || fail "Set BOX in .env, e.g. BOX=ubuntu@203.0.113.4"
  [[ -n "$SSH_KEY" ]] || fail "Set SSH_KEY in .env to your private key path"
  [[ -f "$SSH_KEY" ]] || fail "No such key: $SSH_KEY"

  # Same reason: the two scripts below are spawned, not sourced.
  export BOX SSH_KEY

  SSH=(ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
}

# Run a command on the box, in the project directory.
on_box() { "${SSH[@]}" "$BOX" "cd ~/$REMOTE_DIR && $1"; }

# With a tty, or helm and kubectl output arrives in one lump at the end.
on_box_tty() { "${SSH[@]}" -t "$BOX" "cd ~/$REMOTE_DIR && $1"; }

# Our own processes only: the pods' run as root. [v] stops the pattern matching
# pkill's own command line, which would kill the remote shell and return 255.
stop_engine() {
  "${SSH[@]}" "$BOX" '{ pkill -u "$(id -u)" -f "[v]llm serve" || true; }; sleep 2'
}

# ── steps ──────────────────────────────────────────────────────────────────

sync_project() {
  announce "sync  —  Mac → $BOX:~/$REMOTE_DIR"
  bash infra/setup/sync.sh
}


show_card() {
  announce "card"
  "${SSH[@]}" "$BOX" \
    'nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader' | sed 's/^/    /'
  local compute_cap
  compute_cap=$("${SSH[@]}" "$BOX" \
    'nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d " "')
  if [[ "$compute_cap" != "8.0" ]]; then
    echo "    NOTE: compute capability $compute_cap is not sm80, so fp8 works on this card"
    echo "          and int4 is no longer forced. docs/project/01-capacity.md needs redoing."
  fi
}

# WITH_OBSERVABILITY=1 adds prometheus and grafana. Off by default so the
# engine path does not pay for binaries it never starts.
install_packages() {
  # No announce here: box_packages.sh prints its own heading and a per-step
  # status, and two "==> packages" lines in a row read as a repeated step.
  on_box_tty "WITH_OBSERVABILITY=${WITH_OBSERVABILITY:-0} bash infra/setup/box_packages.sh"
}

# Holds the whole card, to measure the model's real footprint before slicing.
start_engine() {
  announce "engine  —  $MODEL on :8000, whole card"
  stop_engine
  on_box "source .venv/bin/activate && \
    LOCAL_MODEL='$MODEL' setsid nohup bash infra/setup/engine.sh \
    </dev/null >$ENGINE_LOG 2>&1 & \
    sleep 1; echo '    started, logging to ~/$REMOTE_DIR/$ENGINE_LOG'"
}

wait_for_engine() {
  printf '    waiting for the engine'
  local try
  for try in $(seq 1 "$ENGINE_WAIT_TRIES"); do
    if "${SSH[@]}" "$BOX" 'curl -sf localhost:8000/v1/models >/dev/null' 2>/dev/null; then
      printf ' ready (%ss)\n' "$((try * 5))"
      return 0
    fi
    printf '.'; sleep 5
  done
  printf '\n'
  fail "Engine did not answer in 15 min.  bash infra/setup/up.sh logs"
}


# One worker on the whole card, for the comparison measurement only. Needs the
# box venv, which is why it is not part of `make fleet-lambda`.
cmd_engine() {
  sync_project
  show_card
  install_packages
  start_engine
  wait_for_engine
  check_capacity
  announce "engine up"
  echo "    This holds the whole card. Run 'make fleet-lambda' to replace it with the fleet."
}

# The fleet without an orchestrator, for boxes that are containers — which is
# most rented GPUs. Being a container
# is the reason this command exists.
#
# It installs vLLM, which `make fleet-lambda` does not need: there the pods bring
# their own in the image. Here the workers are processes on the box.
cmd_fleet_runpod() {
  local base="${PORT_BASE:-8100}"
  local model="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"

  # Say what is about to happen and where. Seven components land on a rented
  # box and none of them on this Mac, which is not obvious from a command run
  # on this Mac — and knowing which host owns what is the difference between
  # reading a log and guessing at one.
  announce "fleet-runpod  —  the plan"
  printf '    %-12s %s\n' "this Mac"  "ssh only. The tunnel at the end."
  printf '    %-12s %s\n' "the box"   "$BOX  —  everything below, under ~/$REMOTE_DIR"
  echo
  printf '    %-10s %-26s %s\n' "install" "python venv + vLLM"      "~/$REMOTE_DIR/.venv"
  printf '    %-10s %-26s %s\n' ""        "prometheus, grafana"     "/usr/local/bin"
  echo
  printf '    %-10s %-26s %s\n' "start"   "ledger     :50051"       "records KV routing decisions"
  # What actually starts depends on LAB_TOPOLOGY and on how many cards the box
  # has, and the card count is not known until we are on it — so describe the
  # rule rather than invent three workers. Claiming "1 prefill + 2 decode" to a
  # two-card aggregated fleet is a plan that disagrees with its own run.
  if [[ "${LAB_TOPOLOGY:-aggregated}" == "aggregated" ]]; then
    printf '    %-10s %-26s %s\n' ""      "engine-N   :$base +N"     "one per card, both phases"
  else
    printf '    %-10s %-26s %s\n' ""      "prefill    :$base  GPU 0" "builds the prompt KV"
    printf '    %-10s %-26s %s\n' ""      "decode-*   :$((base+1))+" "generate; decode re-prefills"
  fi
  printf '    %-10s %-26s %s\n' ""        "gateway    :8080"        "guard · admit · place · queue"
  printf '    %-10s %-26s %s\n' ""        "prometheus :9090"        "scrapes all of the above"
  printf '    %-10s %-26s %s\n' ""        "grafana    :31495"       "the dashboards"
  echo
  printf '    %-10s %s\n' "model" "$model"
  printf '    %-10s %s\n' "logs"  "~/$REMOTE_DIR/infra/metrics/local/  —  make fleet-runpod-logs"
  echo

  sync_project
  show_card
  WITH_OBSERVABILITY=1 install_packages
  announce "fleet  —  1 vLLM per GPU, gateway, mooncake"

  # Replace a fleet that is already running, rather than aborting into it.
  #
  # Re-running this over a live fleet did nothing and reported success. workers.sh
  # calls check_ports, finds :8080 and the worker ports held by the OLD fleet, and
  # exits before starting anything — while the readiness poll below asks those
  # same ports and gets answers, from the very fleet we were trying to replace.
  # The whole run then looks identical to a good one. That is how three workers
  # stayed up for 45 minutes without the --enable-auto-tool-choice they had been
  # relaunched to acquire, and how the voice agent kept failing on a fleet whose
  # bring-up said "up".
  #
  # A fleet started by this script is ours to stop, so stop it. The alternative —
  # refusing with "run fleet-runpod-down first" — makes the common case
  # (change something, re-run) a two-command ritual nobody remembers.
  #
  # workers[.]sh, not workers.sh: `pgrep -f` would otherwise match the remote
  # shell running this very check, which contains the pattern in its own
  # command line. That mistake reports a fleet on an empty box.
  if on_box "pgrep -f 'runpod-cluster/setup/workers[.]sh' >/dev/null 2>&1"; then
    announce "a fleet is already running  —  stopping it before starting the new one"
    on_box "bash infra/runpod-cluster/setup/down.sh" >/dev/null 2>&1 || true
    # down.sh kills supervisors first, but a 32B's CUDA context takes a moment to
    # release. Wait for the gateway port to actually free, or check_ports fails
    # again and we are back where we started.
    local freed=0
    for _ in $(seq 1 30); do
      if ! on_box "curl -sf -m 2 localhost:8080/v1/models >/dev/null 2>&1"; then
        freed=1
        break
      fi
      sleep 2
    done
    if (( freed )); then
      printf '    stopped. ports released.\n'
    else
      printf '    WARNING: :8080 still answering — the new fleet may refuse to start.\n'
      printf '             check with: make fleet-runpod-status\n'
    fi
  fi

  # Detached, not attached. Held in the foreground this blocks the terminal, so
  # the tunnel needs a second one — and a workshop instruction that begins
  # "open another terminal" is the step everyone misses. `make fleet-runpod-down`
  # stops it, which is why detaching is safe now and was not before.
  on_box "mkdir -p infra/metrics/local && \
    setsid nohup $(workers_env) bash infra/runpod-cluster/setup/workers.sh \
      > infra/metrics/local/fleet.log 2>&1 < /dev/null & echo started"

  # Weights download on the first run and three workers load in sequence, so
  # this is minutes. Show what it is waiting for rather than a silent hang.
  local port="${PORT_BASE:-8100}"
  echo "    loading — the first run downloads the weights, so this takes a while"
  echo

  # Ask the components, do not read a log.
  #
  # This tailed fleet.log twice and printed blank lines both times — once
  # because log rotation renamed the file out from under the writer, once
  # because the fleet had not written to it yet. A progress display whose
  # failure mode is "looks identical to a hang" is worse than none.
  #
  # Each poll asks which ports answer and prints a line per change, so a
  # component appearing is visible the moment it happens.
  local probe=(ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new
               -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2
               -o BatchMode=yes)

  # Ask the BOX how many workers there will be, rather than assuming three.
  #
  # The poll used to wait on $base, +1 and +2 by name. An aggregated fleet on
  # two cards starts two engines, so :8102 never answered and this loop sat
  # there until its 40-minute timeout on a fleet that was already serving.
  # layout.sh is the one place that knows the shape, so read it from there.
  local worker_names worker_count
  worker_names="$(on_box 'source infra/runpod-cluster/setup/layout.sh 2>/dev/null \
    && printf "%s " "${NAMES[@]}"' 2>/dev/null || true)"
  # shellcheck disable=SC2206
  local names_arr=(${worker_names:-prefill decode-0 decode-1})
  worker_count=${#names_arr[@]}

  local worker_ports=""
  for (( wi = 0; wi < worker_count; wi++ )); do
    worker_ports+=" $(( base + wi ))"
  done

  local waited=0 last="" state=""
  while (( waited < 2400 )); do
    state="$("${probe[@]}" "$BOX" "
      for p in 50051 9400$worker_ports 8080 9090 31495; do
        curl -sf -m 2 \"localhost:\$p/metrics\" >/dev/null 2>&1 ||
        curl -sf -m 2 \"localhost:\$p/v1/models\" >/dev/null 2>&1 ||
        curl -sf -m 2 \"localhost:\$p/api/health\" >/dev/null 2>&1 && printf 'y' || printf 'n'
      done" 2>/dev/null || true)"

    # One character per component, in start order.
    local names=(mooncake gpu "${names_arr[@]}" gateway prometheus grafana)
    local shown="" i=0
    for n in "${names[@]}"; do
      case "${state:$i:1}" in
        y) shown+="$n ✓  " ;;
        *) shown+="$n ·  " ;;
      esac
      i=$(( i + 1 ))
    done

    if [[ "$shown" != "$last" ]]; then
      printf '    [%2dm%02ds]  %s\n' $(( waited / 60 )) $(( waited % 60 )) "$shown"
      last="$shown"
    elif (( waited % 60 == 0 && waited > 0 )); then
      # Even with nothing new, say something each minute. A 32B takes minutes
      # per worker, and silence is indistinguishable from death.
      printf '    [%2dm%02ds]  %s\n' $(( waited / 60 )) $(( waited % 60 )) "$shown"
    fi

    # EVERY worker and the gateway. Breaking on the gateway alone is how
    # `make fleet-runpod` reported success with every worker dead: the gateway
    # serves /v1/models from its own allowlist, so it answers with or without a
    # pool behind it. Workers occupy positions 2..2+count-1, the gateway next.
    local all_up=1 gi
    for (( gi = 2; gi < 2 + worker_count + 1; gi++ )); do
      [[ "${state:$gi:1}" == "y" ]] || { all_up=0; break; }
    done
    if (( all_up )); then
      echo; announce "fleet up"; break
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done

  # Same test as the loop, so a timeout and a success cannot disagree about
  # what "up" means.
  if [[ "${state:2:1}" != "y" || "${state:3:1}" != "y" \
     || "${state:4:1}" != "y" || "${state:5:1}" != "y" ]]; then
    fail "  the fleet did not come up — the last line above shows which part.
  Logs:  make fleet-runpod-logs
  Stop:  make fleet-runpod-down"
  fi

  echo "    gateway   127.0.0.1:8080/v1"
  echo "    workers   $port  $((port+1))  $((port+2))"
  echo "    logs      make fleet-runpod-logs          stop   make fleet-runpod-down"
  echo

  # And the tunnel, in the foreground, which is the thing to leave open.
  cmd_fleet_runpod_ssh
}

# Proves the fleet works rather than that it started. Syncs first, like every
# other remote command.
cmd_fleet_smoke() {
  sync_project >/dev/null 2>&1 || true
  on_box "$(workers_env) GRAFANA_PASSWORD=$(printf %q "${GRAFANA_PASSWORD:-admin}") bash infra/runpod-cluster/setup/smoke.sh"
}

# The process fleet's own tunnel. `up.sh tunnel` forwards the Kubernetes
# NodePorts — 30030 Open WebUI, 30800/30801 the fleet Services — none of which
# exist here, so it advertised addresses that could never answer. This forwards
# exactly what fleet-runpod starts, and says what each one is.
cmd_fleet_runpod_ssh() {
  local base="${PORT_BASE:-8100}"
  announce "tunnel  —  the process fleet. Leave this open."
  echo "    gateway      http://127.0.0.1:8080/v1"
  echo "    grafana      http://127.0.0.1:31495      admin / ${GRAFANA_PASSWORD:-admin}"
  echo "    prometheus   http://127.0.0.1:9090"
  echo "    workers      $base (prefill)  $((base+1)) $((base+2)) (decode)"
  echo "    mooncake     http://127.0.0.1:50051/metrics"
  echo
  exec ssh -i "$SSH_KEY" -p "$SSH_PORT" \
    -o StrictHostKeyChecking=accept-new \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
    -L 8080:127.0.0.1:8080 \
    -L "$base:127.0.0.1:$base" \
    -L "$((base+1)):127.0.0.1:$((base+1))" \
    -L "$((base+2)):127.0.0.1:$((base+2))" \
    -L 9090:127.0.0.1:9090 \
    -L 31495:127.0.0.1:31495 \
    -L 50051:127.0.0.1:50051 \
    -t "$BOX" "mkdir -p ~/$REMOTE_DIR; cd ~/$REMOTE_DIR; exec bash -l"
}

# Asks the fleet what it is doing rather than reading ports. Syncs first: the
# box only has what the last sync left, and a status script that is not there
# reports a broken repo instead of a fleet.
cmd_fleet_status() {
  sync_project >/dev/null 2>&1 || true
  on_box "$(workers_env) GRAFANA_PASSWORD=$(printf %q "${GRAFANA_PASSWORD:-admin}") bash infra/runpod-cluster/setup/status.sh"
}

# The fleet runs detached, so its output is a file rather than your terminal.
cmd_logs_fleet() {
  load_env >/dev/null 2>&1 || true
  on_box "tail -n 80 -f infra/metrics/local/fleet.log"
}

#: What the fleet needs from .env, carried to the box explicitly.
#:
#: sync.sh excludes .env on purpose — "the box has the code but not your
#: secrets" — so nothing in it reaches a worker. The first real run downloaded a
#: 7B because LOCAL_MODEL was unset and engine.sh fell back to its default,
#: while .env on the Mac said 32B. Every other choice was invisible the same
#: way, including SLICE_MIB, which decides whether a run is INERT.
#:
#: Named individually rather than shipping the file: the box is rented, and it
#: has no business holding a Deepgram key.
WORKERS_ENV=(
  LOCAL_MODEL TEXT_MODEL VISION_MODEL      # which model, and the guard allowlist
  SLICE_MIB WHOLE_CARD_UTIL                # the memory budget per worker
  PORT_BASE                                # a rented box is not empty
  MAX_MODEL_LEN MAX_NUM_SEQS SCHEDULING_POLICY
  # Without this the engine 400s every request carrying `tools`, which is every
  # substantive turn the voice agent takes. Default is set in engine.sh.
  TOOL_CALL_PARSER
  KV_BACKEND LAB_TOPOLOGY LAB_SPLIT
  HF_TOKEN                                 # gated weights

  # The admission demo. Without ADMISSION_POLICY the gate is off and
  # /metrics reports policy="none" — the dashboards show a fleet with no
  # admission control, which is the one thing the fleet exists to show.
  ADMISSION_POLICY ADMISSION_THRESHOLD ADMISSION_MAX_WAITING
  ADMISSION_ENFORCE ADMISSION_LOG ADMISSION_EXPERIMENT ADMISSION_RUN

  # The gate runs AFTER the tenant rate limit, so this number decides whether
  # the gate ever sees pressure. At the 10k/min default a load test of long
  # prompts is refused as 429 (client quota) long before KV fills, and the KV
  # threshold never fires: the run looks like admission control working when
  # it is actually a quota.
  TENANT_TOKENS_PER_MIN
)

# Build `env K=V ...` for the values that are actually set, quoted for the
# remote shell. Unset stays unset, so each script's own default still applies.
workers_env() {
  local out="env" name value
  for name in "${WORKERS_ENV[@]}"; do
    value="${!name:-}"
    [[ -n "$value" ]] && out+=" $name=$(printf %q "$value")"
  done
  printf '%s' "$out"
}

# No equivalent on the Kubernetes path: there `make clean` deletes the
# workloads and k3s goes with them. Raw processes have no such owner.
cmd_fleet_down() {
  # PORT_BASE, so the port report at the end names the right ones.
  # Sync first. down.sh is a file on the box, and the box only has whatever the
  # last sync left there — so a newly added script is "No such file or
  # directory", which reads as a broken repo rather than a stale one. Every
  # other command that runs a remote script syncs before it; this one did not.
  sync_project
  announce "fleet-down  —  stopping the process fleet"
  on_box "$(workers_env) bash infra/runpod-cluster/setup/down.sh"
}

# Only the whole-card engine from `up.sh engine`. The fleet has its own stop,
# because it has supervisors that would restart a plain pkill.
cmd_down() {
  announce "stopping the whole-card engine"
  "${SSH[@]}" "$BOX" '{ pkill -u "$(id -u)" -f "[v]llm serve" || true; }; echo stopped'
  echo "    The fleet is separate:  make fleet-runpod-down"
  echo "    The box is still billing. Stop the pod in your provider's console."
}


# Load runs ON THE BOX, against 127.0.0.1:8080. Over the ssh tunnel your WAN
# round-trip lands inside every TTFT and concurrency serialises through one TCP
# stream, so a bundle taken from the Mac measures your internet, not the fleet.
cmd_load() {
  sync_project
  announce "load  —  on the box, against the gateway on 127.0.0.1:8080"
  on_box_tty "python3 -m benchmarking.load --url http://127.0.0.1:8080 $*"
  announce "pulling the bundle back"
  rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    "$BOX:~/$REMOTE_DIR/infra/metrics/" infra/metrics/
  echo "    bundles under infra/metrics/"
}

# Needs the current script on the box, like every other remote step.
cmd_dcgm() {
  sync_project
  announce "dcgm  —  install, then ask the driver what it will report"
  on_box_tty "bash infra/runpod-cluster/setup/dcgm.sh"
}

# Syncs first: this deletes things, so it must be the current script doing it.
cmd_clean() {
  sync_project
  announce "clean  —  stopping the fleet and removing what we installed"
  on_box_tty "bash infra/runpod-cluster/setup/clean.sh $*"
}



main() {
  local command="${1:-}"
  case "$command" in
    dcgm|fleet-runpod|fleet-runpod-down|fleet-runpod-logs|fleet-runpod-status|fleet-runpod-ssh|fleet-runpod-smoke-test|engine|down|clean|load) ;;
    # Print the header block by shape, not by line number: `sed -n '2,14p'`
    # dropped `clean` off the end the moment two commands were added, and the
    # test that catches it is the only reason anyone noticed.
    -h|--help|"") sed -n '2,/^$/p' "$0" | sed '$d'; exit 0 ;;
    *) fail "unknown command: $command   (try: up.sh --help)" ;;
  esac
  load_env
  case "$command" in
    dcgm)          cmd_dcgm ;;
    fleet-runpod)  cmd_fleet_runpod ;;
    fleet-runpod-down)    cmd_fleet_down ;;
    fleet-runpod-logs)    cmd_logs_fleet ;;
    fleet-runpod-status)  cmd_fleet_status ;;
    fleet-runpod-ssh)     cmd_fleet_runpod_ssh ;;
    fleet-runpod-smoke-test) cmd_fleet_smoke ;;
    engine)        cmd_engine ;;
    down)          cmd_down ;;
    clean)         shift; cmd_clean "$@" ;;
    load)          shift; cmd_load "$@" ;;
  esac
}

main "$@"
