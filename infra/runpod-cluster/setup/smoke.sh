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

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
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

# The same path again, but carrying `tools` — because the plain completion
# above passed 11/11 on a fleet where the voice agent could not finish a single
# substantive turn.
#
# app/dodona.yaml gives the agent web_search and scrape_url, so every real
# question sends tool schemas. vLLM refuses those outright unless it was
# started with --enable-auto-tool-choice and --tool-call-parser, and the
# gateway reports that refusal as 200 with no `choices` at all. Nothing logs a
# cause at any layer: the only visible symptom is the agent apologising aloud.
tools_body='{"model":"'"$MODEL"'","max_tokens":40,"messages":[{"role":"user","content":"What is 17 times 3?"}],"tools":[{"type":"function","function":{"name":"web_search","description":"search the web","parameters":{"type":"object","properties":{"q":{"type":"string"}}}}}]}'
tool_reply=$(curl -sf -m 180 localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' -d "$tools_body" 2>/dev/null \
  | python3 -c 'import sys, json
body = json.load(sys.stdin)
choices = body.get("choices")
if not choices:
    # The signature of an engine that rejected the tool schemas.
    print("NO_CHOICES")
else:
    message = choices[0].get("message") or {}
    print("ok" if (message.get("content") or message.get("tool_calls")) else "EMPTY")' 2>/dev/null)
case "${tool_reply:-}" in
  ok)
    pass "a request carrying tools is served — the agent can call web_search"
    ;;
  NO_CHOICES|EMPTY|"")
    fail "a request with tools came back with no choices.
      The engine refused the tool schemas. vLLM needs BOTH of:
        --enable-auto-tool-choice   --tool-call-parser hermes
      infra/setup/engine.sh passes them from TOOL_CALL_PARSER (default hermes).
      If it does, the workers predate the change: make fleet-runpod to relaunch."
    ;;
esac

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
  # Exactly four, and exactly these four: the request path, in order. An extra
  # one means a stale dashboard survived in Grafana's database from a previous
  # layout, which is how four blank Kubernetes screens stayed up for a week.
  expected="dodona-gateway dodona-engine dodona-kv dodona-hardware"
  uids=$(curl -sf -m 10 -u "admin:$GRAFANA_PASSWORD" 'localhost:31495/api/search?type=dash-db' 2>/dev/null \
    | python3 -c 'import sys,json; print(" ".join(sorted(d["uid"] for d in json.load(sys.stdin))))' 2>/dev/null)
  n=$(printf '%s' "$uids" | wc -w | tr -d ' ')
  missing=""
  for u in $expected; do
    [[ " $uids " == *" $u "* ]] || missing="${missing:+$missing }$u"
  done
  if [[ "${n:-0}" -eq 4 && -z "$missing" ]]; then
    pass "4 dashboards: gateway -> engines -> kv -> hardware"
  elif [[ -n "$missing" ]]; then
    fail "missing dashboard(s): $missing (got: ${uids:-none})"
  else
    fail "$n dashboards, expected 4 — a stale one survived: ${uids:-none}"
  fi
  ds=$(curl -sf -m 10 -u "admin:$GRAFANA_PASSWORD" localhost:31495/api/datasources 2>/dev/null \
    | python3 -c 'import sys,json; print(",".join(d["uid"] for d in json.load(sys.stdin)))' 2>/dev/null)
  if [[ "$ds" == *prometheus* ]]; then
    pass "datasource uid 'prometheus' present — the uid every provisioned dashboard queries"
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
