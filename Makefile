# Dodona — RunPod, one vLLM per card, data parallel.
#
# The fleet, in order:
#   make doctor         is .env complete? run this first
#   make fleet-runpod   THE command: sync, install, start, open the tunnel
#   make fleet-runpod-smoke-test  prove it works: completion, tools, scrapes, dashboards
#   make fleet-runpod-status      model, KV pressure, card placement, uptime
#   make fleet-runpod-logs        follow it (it runs detached)
#   make fleet-runpod-down        stop it
#   make fleet-runpod-ssh         the tunnel on its own, if you closed it
#
# Then:
#   make app            the voice app — AgentShip and the UI on :3000
#   make load           a load test ON THE BOX, bundle pulled back
#   make dcgm           install real DCGM; says if this host allows profiling
#   make test           the control plane here, no GPU needed
#   make clean          wipe the box back to bare metal
#
# Other topologies are branches, not flags: lambda-cluster (k3s + HAMi + KEDA),
# lambda-pd-disaggregation, runpod-tp-cluster.
#
# Anything more specific is a plain python or bash line in docs/COMMANDS.md.
# Make is the workflow, not a menu.
.DEFAULT_GOAL := help
# `app` must be here: a directory of that name exists, so without it make
# reports "`app' is up to date" and runs nothing.
.PHONY: help fleet-runpod fleet-runpod-down dcgm fleet-runpod-logs fleet-runpod-status fleet-runpod-ssh fleet-runpod-smoke-test test load app doctor clean

SHELL := /usr/bin/env bash
SETUP := infra/setup
PY    ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# infra/control holds gateway, router, edge and kernels — one import root, the
# same one tests/pytest.ini uses.
export PYTHONPATH := $(CURDIR)/infra/control

help:
	@echo ""
	@echo "  Dodona — RunPod, one vLLM per card, data parallel"
	@echo ""
	@echo "  Bring it up"
	@echo "   make doctor         is .env complete? names what is missing"
	@echo "   make fleet-runpod   THE ONE COMMAND:"
	@echo "                       sync, install, start the fleet, open the tunnel"
	@echo ""
	@echo "  Check it"
	@echo "   make fleet-runpod-smoke-test  a completion, a tool call, scrapes, dashboards"
	@echo "   make fleet-runpod-status      model, KV pressure, cards, uptime"
	@echo "   make fleet-runpod-logs        follow it        (it runs detached)"
	@echo ""
	@echo "  Use it"
	@echo "   make app            voice + text app on :3000, through the gateway"
	@echo "   make load           load test ON THE BOX  (ARGS=\"--sessions 32\")"
	@echo "   make dcgm           real DCGM; says if this host allows profiling"
	@echo ""
	@echo "  Stop it"
	@echo "   make fleet-runpod-down  stop the fleet"
	@echo "   make clean              wipe the box back to bare metal"
	@echo ""
	@echo "  No GPU needed"
	@echo "   make test           the control plane, locally"
	@echo ""
	@echo "  First run:  cp .env.example .env   # BOX, SSH_KEY, SSH_PORT"
	@echo "              make doctor && make fleet-runpod"
	@echo ""
	@echo "  fleet-runpod is safe to re-run: it stops a running fleet first."
	@echo "  Grafana is at 127.0.0.1:31495 once the tunnel is open."
	@echo ""


# One vLLM per card, the gateway in front, no orchestrator. Drives the box over
# ssh — syncs, installs vLLM, starts the fleet detached, then opens the tunnel.
# Safe to re-run: it stops a running fleet first.
#
# venv first, so this works on a fresh clone with nothing installed.
fleet-runpod:
	@[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -qU pip \
	  && .venv/bin/pip install -qr requirements.txt; }
	@bash $(SETUP)/up.sh fleet-runpod

# Stops what fleet-runpod started.
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

# The tunnel. Leave it open — every URL is 127.0.0.1 on your Mac.
fleet-runpod-ssh:
	@bash $(SETUP)/up.sh fleet-runpod-ssh

# Not "did it start" — "does a request come back, is Prometheus scraping the
# right ports, does Grafana have the dashboards". Every check here maps to
# something that has been broken while looking fine from outside.
fleet-runpod-smoke-test:
	@bash $(SETUP)/up.sh fleet-runpod-smoke-test

# Install the real DCGM exporter and report whether this box allows profiling.
# nvidia-smi cannot see PIPE_TENSOR_ACTIVE or DRAM_ACTIVE at any privilege
# level — they are a different interface, gated by a driver parameter on the
# HOST that a rented container cannot change. This asks, and says so either way.
dcgm:
	@bash $(SETUP)/up.sh dcgm

# On the box, so no tunnel sits in the latency path. Bundles land in
# infra/metrics/<run>/load.json. Override anything:  make load ARGS="--sessions 32"
LOAD_ARGS ?= --sessions 8 --seed 1 --run baseline
load:
	@bash $(SETUP)/up.sh load $(if $(ARGS),$(ARGS),$(LOAD_ARGS))

app: ## the voice app — AgentShip (agent, ears, mouth) and the UI
	@bash app/run.sh

# Names every value .env must carry and what its absence breaks, printing no
# secrets. The alternative is finding a missing key twelve minutes into a
# bring-up, reported by whatever happened to need it.
doctor:
	@$(PY) $(SETUP)/check_env.py

# Stops the fleet and removes the venv, logs, prometheus/grafana data and the
# binaries we installed. KEEPS the model weights — they are ~19 GB on disk and
# re-downloading them costs the start of your next bring-up:
#   make clean ARGS=--purge-models    drop those too, handing the box back
clean:
	@bash $(SETUP)/up.sh clean $(ARGS)

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
