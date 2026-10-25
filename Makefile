# Dodona.
#
#   make fleet-lambda   k3s + HAMi + KEDA + the fleet + gateway  (needs a host)
#   make fleet-runpod   the same fleet as processes, no k3s     (any box)
#   make fleet-runpod-smoke-test  prove it works: a completion, scrapes, dashboards
#   make fleet-runpod-ssh   tunnel — gateway, grafana, prometheus, workers
#   make fleet-runpod-status  model, KV pressure, GPU placement, restarts
#   make fleet-runpod-logs  follow the detached fleet's log
#   make fleet-runpod-down  stop it
#   make observability  Prometheus + Grafana + DCGM
#   make ssh            tunnel — Grafana, the app and the fleet in your browser
#   make doctor         is .env complete? run this before the first bring-up
#   make app            the voice app — LiveKit, the agent worker, the UI
#   make test           run the control plane here, no GPU needed
#   make load           run a load test ON THE BOX, pull the bundle back
#   make logs           pod logs, scrape noise stripped
#   make kv             KV connector state and the transfer counters
#   make clean          wipe the box back to bare metal
#
# Anything more specific is a plain python or bash line in docs/COMMANDS.md.
# Make is the workflow, not a menu.
.DEFAULT_GOAL := help
# `app` must be here: a directory of that name exists, so without it make
# reports "`app' is up to date" and runs nothing.
.PHONY: help fleet-lambda fleet-runpod fleet-runpod-down fleet-runpod-logs fleet-runpod-status fleet-runpod-ssh fleet-runpod-smoke-test observability ssh test load logs kv app doctor clean

SHELL := /usr/bin/env bash
SETUP := infra/setup
PY    ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# control/ holds gateway, router and edge; cluster/ holds observability and
# kernels. Both are import roots — same as tests/pytest.ini.
export PYTHONPATH := $(CURDIR)/infra/control:$(CURDIR)/infra/lambda-cluster

help:
	@echo ""
	@echo "  Dodona"
	@echo ""
	@echo "   make fleet-lambda   k3s + HAMi + KEDA + fleet + gateway   (needs a host)"
	@echo "   make fleet-runpod   THE ONE COMMAND on a container box:"
	@echo "                       sync, install, start the fleet, open the tunnel"
	@echo "   make fleet-runpod-smoke-test  prove it works, end to end"
	@echo "   make fleet-runpod-ssh   tunnel — gateway, grafana, prometheus"
	@echo "   make fleet-runpod-status  is it healthy? model, KV, GPUs, restarts"
	@echo "   make fleet-runpod-logs  follow it          (it runs detached)"
	@echo "   make fleet-runpod-down  stop it"
	@echo "   make observability  Prometheus + Grafana + DCGM"
	@echo "   make ssh            tunnel — Grafana :31495, app :30030, fleet :30800"
	@echo "   make doctor         is .env complete? before the first bring-up"
	@echo "   make app            voice app — LiveKit + worker + UI on :3000"
	@echo "   make test           run the control plane here, no GPU needed"
	@echo "   make load           load test ON THE BOX  (ARGS=\"--sessions 32\")"
	@echo "   make logs           pod logs  (ARGS=\"decode --follow\")"
	@echo "   make kv             did the KV hop move bytes? connector + counters"
	@echo "   make clean          wipe the box: workloads, helm, k3s, engines"
	@echo ""
	@echo "  First run:  cp .env.example .env   # set BOX and SSH_KEY"
	@echo "              make fleet-lambda && make observability && make ssh"
	@echo ""
	@echo "  Redo from scratch:  make clean && make fleet-lambda"
	@echo ""
	@echo "  Both bring-up targets are safe to re-run."
	@echo "  Stopping, logs, load, one-off tools: COMMANDS.md"
	@echo ""

# venv first, so this works on a fresh clone with nothing installed.
fleet-lambda:
	@[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -qU pip \
	  && .venv/bin/pip install -qr requirements.txt; }
	@bash $(SETUP)/up.sh fleet-lambda

# One vLLM per GPU, the gateway in front, no orchestrator. For boxes that are
# containers — which is most rented GPUs — where `make fleet-lambda` cannot install
# k3s. Same control plane, same dashboards, same reports; what it drops is
# HAMi's enforced slices and KEDA.
#
# Drives the box over ssh, like every other target here — syncs, installs vLLM
# (which `make fleet-lambda` does not need: there the pods bring their own), then
# starts the workers. Leave it running; Ctrl-C stops the fleet.
fleet-runpod:
	@[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -qU pip \
	  && .venv/bin/pip install -qr requirements.txt; }
	@bash $(SETUP)/up.sh fleet-runpod

# Stops what fleet-runpod started. The Kubernetes path's equivalent is
# `make clean`, which deletes the workloads and k3s with them.
fleet-runpod-down:
	@bash $(SETUP)/up.sh fleet-runpod-down

# The fleet runs detached, so its output is a file on the box, not your terminal.
fleet-runpod-logs:
	@bash $(SETUP)/up.sh fleet-runpod-logs

# Asks each worker what model it serves, how full its KV cache is, which card it
# is on, and whether there is exactly one fleet. Listening ports answer none of
# those, and every one of them has been wrong while the ports looked fine.
fleet-runpod-status:
	@bash $(SETUP)/up.sh fleet-runpod-status

# `make ssh` forwards the Kubernetes NodePorts, which do not exist here — that
# is why :30030 Open WebUI never answered. This forwards what actually runs.
fleet-runpod-ssh:
	@bash $(SETUP)/up.sh fleet-runpod-ssh

# Not "did it start" — "does a request come back, is Prometheus scraping the
# right ports, does Grafana have the dashboards". Every check here maps to
# something that has been broken while looking fine from outside.
fleet-runpod-smoke-test:
	@bash $(SETUP)/up.sh fleet-runpod-smoke-test

observability:
	@bash $(SETUP)/up.sh observability

ssh:
	@bash $(SETUP)/up.sh tunnel

# On the box, so no tunnel sits in the latency path. Bundles land in
# infra/metrics/<run>/load.json. Override anything:  make load ARGS="--sessions 32"
LOAD_ARGS ?= --sessions 8 --seed 1 --run baseline
load:
	@bash $(SETUP)/up.sh load $(if $(ARGS),$(ARGS),$(LOAD_ARGS))

# Keeps the HuggingFace cache; those weights are on disk, not the GPU.
# Prints and exits so it can be piped:  make logs | grep -i hcas
logs:
	@bash $(SETUP)/up.sh logs $(ARGS)

# The one diagnostic that is always wanted and always buried in a log.
kv:
	@bash $(SETUP)/up.sh kv

app: ## the voice app — LiveKit, the agent worker, and the UI
	@bash app/run.sh

# Names every value .env must carry and what its absence breaks, printing no
# secrets. The alternative is finding a missing key twelve minutes into a
# bring-up, reported by whatever happened to need it.
doctor:
	@$(PY) $(SETUP)/check_env.py

clean:
	@bash $(SETUP)/up.sh clean

# No GPU required. Compiles, checks every doc link, runs the suite, then
# exercises the gateway and guardrail behaviours end to end.
# The harnesses are separate processes, so tests/conftest.py cannot strip these
# for them. Same list, read from the same file — see tests/env_isolation.py.
HERMETIC = env $(shell $(PY) -c "import sys; sys.path.insert(0,'tests'); \
  import env_isolation as e; print(' '.join('-u ' + v for v in e.ALL_ENV))")

test:
	@$(PY) -m compileall -q infra tests && echo "  modules compile"
	@$(PY) $(SETUP)/check_docs.py
	@if [ -x .venv/bin/pytest ]; then .venv/bin/pytest tests/ -q; \
	 elif $(PY) -c "import pytest" 2>/dev/null; then $(PY) -m pytest tests/ -q; \
	 else echo "  no pytest — python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; fi
	@echo "  gateway behaviours:"
	@for b in $$($(HERMETIC) $(PY) -m gateway.harness --list | awk '{print $$1}'); do \
	   $(HERMETIC) $(PY) -m gateway.harness --behavior $$b >/dev/null 2>&1 \
	     && printf "    ok    %s\n" $$b || printf "    FAIL  %s\n" $$b; done
	@echo "  guardrails:"
	@for b in $$($(HERMETIC) $(PY) -m edge.harness --list | awk '{print $$1}'); do \
	   $(HERMETIC) $(PY) -m edge.harness --behavior $$b >/dev/null 2>&1 \
	     && printf "    ok    %s\n" $$b || printf "    FAIL  %s\n" $$b; done
