#!/usr/bin/env bash
# Stop a process fleet started by workers.sh.
#
#   make fleet-runpod-down      (this script, run on the GPU box)
#
# The Kubernetes path never needed this: `make clean` deletes the workloads and
# k3s goes with them. A supervisor of raw processes has no such owner, so
# stopping it was a pkill typed from memory — and the supervise loops restart
# anything killed in the wrong order, which is how a "stopped" fleet comes back.
#
# Order matters. The supervisors die first, or they resurrect every worker in
# the three seconds it takes to kill the next one.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

#: Matched against full command lines, in the order they must die. Anchored on
#: this repo's path where possible, so a vLLM someone else is running on the
#: same box is left alone.
declare -a TARGETS=(
  "infra/local/workers.sh"        # the supervisors — first, always
  "$ROOT/.venv/bin/vllm"          # the three engines
  "vllm serve"                    # ...and any started a different way
  "$ROOT/.venv/bin/python -m gateway.serve"
  "$ROOT/.venv/bin/python -m router.mooncake"
  "prometheus --config.file=$ROOT"
  "grafana"
)

say "stopping the fleet"
stopped=0
for pattern in "${TARGETS[@]}"; do
  # -f matches the whole command line. Our own pid is excluded by pkill.
  if pkill -f "$pattern" 2>/dev/null; then
    note "stopped: $pattern"
    stopped=$(( stopped + 1 ))
    # The supervisors need a moment to notice before the next pattern runs,
    # or their restart races the kill.
    [[ "$pattern" == *workers.sh ]] && sleep 2
  fi
done

sleep 2

# Report what is left rather than claiming success. A worker that ignored TERM
# is more useful named than assumed gone.
left="$(pgrep -fa "vllm serve|gateway.serve|router.mooncake|infra/local/workers.sh" 2>/dev/null || true)"
if [[ -n "$left" ]]; then
  note ""
  note "still running:"
  echo "$left" | sed 's/^/      /'
  note ""
  note "give them a moment, then re-run. Or: pkill -9 -f 'vllm serve'"
  exit 1
fi

if (( stopped == 0 )); then
  note "nothing was running"
else
  note "all stopped"
fi

# The ports the fleet holds, so a restart does not meet its own corpse.
for port in 8000 8001 8002 8100 8101 8102 8080 50051 9090 31495; do
  pid=$(ss -lptn "sport = :$port" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  [[ -n "$pid" ]] && note "note: :$port still held by $(ps -p "$pid" -o comm= 2>/dev/null) (pid $pid)"
done
exit 0
