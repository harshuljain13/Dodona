#!/usr/bin/env bash
# Prove the fleet works, rather than that it started.
#
#   make fleet-runpod-smoke-test     (this script, run on the GPU box)
#
# Every check here corresponds to something that has been broken today while
# looking fine from outside: a worker answering with the wrong model, a health
# probe satisfied by nginx, Prometheus scraping ports nothing listens on,
# Grafana running with no dashboards provisioned. "It started" is not the
# question. "Does a request come back" is.
#
# Exit code is the number of failures, so it is usable as a gate.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
[[ -f .env ]] && set -a && source .env && set +a

PORT_BASE="${PORT_BASE:-8100}"
MODEL="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"
GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-admin}"

fails=0
say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
pass() { printf '    \033[0;32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '    \033[1;31mFAIL\033[0m  %s\n' "$*"; fails=$(( fails + 1 )); }
skip() { printf '    ....  %s\n' "$*"; }

say "workers"
i=0
for role in prefill decode-0 decode-1; do
  port=$(( PORT_BASE + i )); i=$(( i + 1 ))
  got=$(curl -sf -m 10 "localhost:$port/v1/models" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)
  if [[ "$got" == "$MODEL" ]]; then
    pass "$role :$port serves $MODEL"
  elif [[ -n "$got" ]]; then
    # The failure that matters: it answered, with something else. A fleet
    # serving the wrong model produces numbers that look perfectly valid.
    fail "$role :$port serves '$got', expected '$MODEL'"
  else
    fail "$role :$port not answering"
  fi
done

say "the serve path"
# The whole point: guard -> admit -> place -> queue -> a worker -> back.
reply=$(curl -sf -m 180 localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: ready\"}],\"max_tokens\":10}" \
  2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip())' 2>/dev/null)
if [[ -n "$reply" ]]; then
  pass "completion through the gateway: \"$reply\""
else
  fail "no completion from the gateway — guard/admit/place/queue or the pools"
fi

# Placement is only a decision if more than one decode worker can take work.
# One answering decode is routing, not choosing.
alive=0
for p in $(( PORT_BASE + 1 )) $(( PORT_BASE + 2 )); do
  curl -sf -m 5 "localhost:$p/v1/models" >/dev/null 2>&1 && alive=$(( alive + 1 ))
done
if (( alive == 2 )); then
  pass "two decode workers answering — place() is a real choice"
else
  fail "$alive decode worker(s) — place() degenerates to routing"
fi

say "observability"
if curl -sf -m 5 localhost:9090/-/healthy >/dev/null 2>&1; then
  pass "prometheus healthy"
  # Scraping the right ports is separate from running: a config left pointing
  # at 8000-8002 while the fleet moved to 8100-8102 leaves every panel empty.
  down=$(curl -sf -m 10 'localhost:9090/api/v1/targets?state=active' 2>/dev/null \
    | python3 -c '
import sys,json
d=json.load(sys.stdin)["data"]["activeTargets"]
bad=[t for t in d if t["health"]!="up" and t["labels"].get("job")!="dcgm"]
print(" ".join(t["scrapeUrl"] for t in bad))' 2>/dev/null)
  if [[ -z "$down" ]]; then
    pass "every scrape target up (dcgm excepted — it is optional)"
  else
    fail "targets down: $down"
  fi
else
  fail "prometheus not healthy"
fi

if curl -sf -m 5 localhost:31495/api/health >/dev/null 2>&1; then
  pass "grafana healthy"
  # Running with nothing provisioned is the quiet failure: Grafana starts, the
  # datasource is absent, every panel is blank and nothing logs an error.
  n=$(curl -sf -m 10 -u "admin:$GRAFANA_PASSWORD" 'localhost:31495/api/search?type=dash-db' 2>/dev/null \
    | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null)
  if [[ "${n:-0}" -ge 1 ]]; then
    pass "$n dashboard(s) provisioned"
  else
    fail "grafana has no dashboards — check the provisioning tree"
  fi
  ds=$(curl -sf -m 10 -u "admin:$GRAFANA_PASSWORD" localhost:31495/api/datasources 2>/dev/null \
    | python3 -c 'import sys,json; print(",".join(d["uid"] for d in json.load(sys.stdin)))' 2>/dev/null)
  if [[ "$ds" == *prometheus* ]]; then
    pass "datasource uid 'prometheus' present — the uid all 10 dashboards query"
  else
    fail "no datasource with uid 'prometheus' (got: ${ds:-none})"
  fi
else
  fail "grafana not healthy"
fi

say "the hop"
if curl -sf -m 5 localhost:50051/metrics >/dev/null 2>&1; then
  pass "mooncake answering"
else
  fail "mooncake not answering — kvbus will time out on every hop"
fi

echo
if (( fails == 0 )); then
  say "smoke test passed"
else
  say "$fails check(s) failed"
fi
exit "$fails"
