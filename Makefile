# Dodona. `make` on its own lists everything.
#
# Two kinds of target:
#
#   plain names   run on YOUR MAC and drive the box over ssh. Use these.
#   box-*         run ON THE BOX, for when a step failed and you want to redo
#                 just that one by hand after `make ssh`.
#
# box-* targets refuse to run on a machine with no NVIDIA device rather than
# failing halfway through.
.DEFAULT_GOAL := help
.PHONY: help all engine cluster observability gate sync ssh logs down test lint \
        box-setup box-serve box-gate box-cluster box-observability box-webui box-smoke

SHELL := /usr/bin/env bash
SETUP := cluster/setup
PY    ?= python3

define gpu_only
@command -v nvidia-smi >/dev/null || { \
  echo "  '$@' runs on the GPU box, not here."; \
  echo "  Either:  make $(subst box-,,$@)     # from here, over ssh"; \
  echo "      or:  make ssh   # then on the box:  make $@"; exit 1; }
endef

help:
	@echo ""
	@echo "  Dodona — make <target>.  Everything below runs from your Mac."
	@echo ""
	@echo "  ── bring it up ───────────────────────────────────────────────"
	@echo "   engine           one worker on the whole card + Open WebUI + tunnel  [stage 0-1]"
	@echo "   cluster          k3s + HAMi + 1 prefill + 2 decode, three slices     [stage 2]"
	@echo "   observability    Prometheus + Grafana + DCGM                         [stage 3]"
	@echo "   all              engine, then cluster, then observability"
	@echo ""
	@echo "  ── day to day ────────────────────────────────────────────────"
	@echo "   gate             the four stage-0 checks — run before believing anything"
	@echo "   sync             rsync the project to the box"
	@echo "   ssh              tunnel + a shell on the box"
	@echo "   logs             tail the engine log"
	@echo "   down             stop the engine and Open WebUI"
	@echo "   test             pytest        lint    compile + check doc links"
	@echo ""
	@echo "  ── on the box, to redo one step (make ssh first) ─────────────"
	@echo "   box-setup  box-serve  box-gate  box-cluster"
	@echo "   box-observability  box-webui  box-smoke"
	@echo ""
	@echo "  Stages and gates: RUNBOOK.md     Every command: COMMANDS.md"
	@echo ""

# ── from your Mac, driving the box over ssh ───────────────────────────────
engine:        ; @./up.sh --stage 1
cluster:       ; @./up.sh --stage 2 --only
observability: ; @./up.sh --stage 3 --only
all:           ; @./up.sh --stage 3
gate:          ; @./up.sh --gate
sync:          ; @bash $(SETUP)/sync_to_lambda.sh
ssh:           ; @bash $(SETUP)/ssh.sh
logs:          ; @./up.sh --logs
down:          ; @./up.sh --down

test:
	@if [ -x .venv/bin/pytest ]; then .venv/bin/pytest tests/ -q; \
	 elif $(PY) -c "import pytest" 2>/dev/null; then $(PY) -m pytest tests/ -q; \
	 else echo "  no pytest. python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; fi

lint:
	@$(PY) -m compileall -q control cluster tests && echo "  modules compile"
	@$(PY) $(SETUP)/check_docs.py

# ── on the box ────────────────────────────────────────────────────────────
box-setup:
	$(gpu_only)
	@bash $(SETUP)/lambda_setup.sh

box-serve:
	$(gpu_only)
	@bash $(SETUP)/lambda_vllm.sh

# 5c and 5d decide whether the capacity arithmetic still holds. Worth re-running.
box-gate:
	$(gpu_only)
	@$(PY) $(SETUP)/gate.py

box-cluster:
	$(gpu_only)
	@bash $(SETUP)/lambda_cluster.sh

box-observability:
	$(gpu_only)
	@bash $(SETUP)/day2_observability.sh

box-webui:
	$(gpu_only)
	@docker rm -f open-webui >/dev/null 2>&1 || true
	@docker run -d --name open-webui -p 3000:8080 \
	  -e OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1 \
	  -e OPENAI_API_KEY=dummy -e WEBUI_AUTH=False \
	  --add-host=host.docker.internal:host-gateway \
	  -v open-webui:/app/backend/data \
	  ghcr.io/open-webui/open-webui:main >/dev/null
	@echo "  Open WebUI on :3000 — http://127.0.0.1:3000 through the tunnel"

box-smoke:
	$(gpu_only)
	@bash $(SETUP)/smoke_lambda.sh
	@bash $(SETUP)/smoke_overflow.sh
