# Dodona — four commands.
#
#   make cluster        k3s + HAMi + KEDA + the fleet + the gateway
#   make observability  Prometheus + Grafana + DCGM
#   make ssh            tunnel — Grafana, the app and the fleet in your browser
#   make test           run the control plane here, no GPU needed
#
# Anything more specific is a plain python or bash line in COMMANDS.md. Make is
# the workflow, not a menu.
.DEFAULT_GOAL := help
.PHONY: help cluster observability ssh test clean

SHELL := /usr/bin/env bash
SETUP := infra/cluster/setup
PY    ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# control/ holds gateway, router and edge; cluster/ holds observability and
# kernels. Both are import roots — same as tests/pytest.ini.
export PYTHONPATH := $(CURDIR)/infra/control:$(CURDIR)/infra/cluster

help:
	@echo ""
	@echo "  Dodona"
	@echo ""
	@echo "   make cluster        k3s + HAMi + KEDA + fleet + gateway + capacity checks"
	@echo "   make observability  Prometheus + Grafana + DCGM"
	@echo "   make ssh            tunnel — Grafana :31495, app :30030, fleet :30800"
	@echo "   make test           run the control plane here, no GPU needed"
	@echo "   make clean          wipe the box: workloads, helm, k3s, engines"
	@echo ""
	@echo "  First run:  cp .env.example .env   # set BOX and SSH_KEY"
	@echo "              make cluster && make observability && make ssh"
	@echo ""
	@echo "  Redo from scratch:  make clean && make cluster"
	@echo ""
	@echo "  Both bring-up targets are safe to re-run."
	@echo "  Stopping, logs, load, one-off tools: COMMANDS.md"
	@echo ""

# venv first, so this works on a fresh clone with nothing installed.
cluster:
	@[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -qU pip \
	  && .venv/bin/pip install -qr requirements.txt; }
	@bash $(SETUP)/up.sh cluster

observability:
	@bash $(SETUP)/up.sh observability

ssh:
	@bash $(SETUP)/up.sh tunnel

# Keeps the HuggingFace cache; those weights are on disk, not the GPU.
clean:
	@bash $(SETUP)/up.sh clean

# No GPU required. Compiles, checks every doc link, runs the suite, then
# exercises the gateway and guardrail behaviours end to end.
test:
	@$(PY) -m compileall -q infra tests && echo "  modules compile"
	@$(PY) $(SETUP)/check_docs.py
	@if [ -x .venv/bin/pytest ]; then .venv/bin/pytest tests/ -q; \
	 elif $(PY) -c "import pytest" 2>/dev/null; then $(PY) -m pytest tests/ -q; \
	 else echo "  no pytest — python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; fi
	@echo "  gateway behaviours:"
	@for b in $$($(PY) -m gateway.harness --list | awk '{print $$1}'); do \
	   $(PY) -m gateway.harness --behavior $$b >/dev/null 2>&1 \
	     && printf "    ok    %s\n" $$b || printf "    FAIL  %s\n" $$b; done
	@echo "  guardrails:"
	@for b in $$($(PY) -m edge.harness --list | awk '{print $$1}'); do \
	   $(PY) -m edge.harness --behavior $$b >/dev/null 2>&1 \
	     && printf "    ok    %s\n" $$b || printf "    FAIL  %s\n" $$b; done
