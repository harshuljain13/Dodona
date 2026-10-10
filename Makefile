# Dodona. `make` on its own lists everything.
#
# Targets are split by WHERE THEY RUN. Getting that wrong is the commonest way
# to lose ten minutes, so the GPU-side ones refuse to run on a machine with no
# NVIDIA device rather than failing halfway through.
.DEFAULT_GOAL := help
.PHONY: help up sync ssh logs down test lint \
        setup serve gate setup-cluster observability webui smoke

SHELL   := /usr/bin/env bash
SETUP   := cluster/setup
PY      ?= python3

# A GPU-side target run on the Mac is a mistake, not an edge case.
define gpu_only
@command -v nvidia-smi >/dev/null || { \
  echo "  '$@' runs on the GPU box, not here."; \
  echo "  make ssh    # then: make $@"; exit 1; }
endef

help:
	@echo ""
	@echo "  Dodona — make <target>"
	@echo ""
	@echo "  ── on your Mac ───────────────────────────────────────────────"
	@echo "   up               everything: sync, setup, serve, gate, webui, tunnel"
	@echo "   sync             rsync the project to the box"
	@echo "   ssh              tunnel + a shell on the box, cd'ed into ~/dodona"
	@echo "   logs             tail the engine log"
	@echo "   down             stop the engine and Open WebUI"
	@echo "   test             pytest"
	@echo "   lint             compile every module, check doc links"
	@echo ""
	@echo "  ── on the GPU box (make ssh first) ───────────────────────────"
	@echo "   setup            venv, requirements, vLLM, import check"
	@echo "   serve            the engine on :8000, in the foreground"
	@echo "   gate             the four stage-0 checks — run this before believing anything"
	@echo "   setup-cluster    k3s + HAMi + the three slices        (stage 2)"
	@echo "   observability    Prometheus + Grafana                  (stage 3)"
	@echo "   webui            Open WebUI on :3000                   (stage 1)"
	@echo "   smoke            smoke the engine and the overflow path"
	@echo ""
	@echo "  Stages and their gates: RUNBOOK.md   Every command: COMMANDS.md"
	@echo ""

# ── Mac ───────────────────────────────────────────────────────────────────
up:    ; @./up.sh
sync:  ; @bash $(SETUP)/sync_to_lambda.sh
ssh:   ; @bash $(SETUP)/ssh.sh
logs:  ; @./up.sh --logs
down:  ; @./up.sh --down

test:
	@if [ -x .venv/bin/pytest ]; then .venv/bin/pytest tests/ -q; \
	 elif $(PY) -c "import pytest" 2>/dev/null; then $(PY) -m pytest tests/ -q; \
	 else echo "  no pytest. python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; fi

lint:
	@$(PY) -m compileall -q control cluster tests && echo "  modules compile"
	@$(PY) $(SETUP)/check_docs.py

# ── GPU box ───────────────────────────────────────────────────────────────
setup:
	$(gpu_only)
	@bash $(SETUP)/lambda_setup.sh

serve:
	$(gpu_only)
	@bash $(SETUP)/lambda_vllm.sh

# The only target worth running twice. 5c and 5d decide whether the capacity
# arithmetic in docs/project/01-capacity.md still holds.
gate:
	$(gpu_only)
	@$(PY) $(SETUP)/gate.py

setup-cluster:
	$(gpu_only)
	@bash $(SETUP)/lambda_cluster.sh

observability:
	$(gpu_only)
	@bash $(SETUP)/day2_observability.sh

webui:
	$(gpu_only)
	@docker rm -f open-webui >/dev/null 2>&1 || true
	@docker run -d --name open-webui -p 3000:8080 \
	  -e OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1 \
	  -e OPENAI_API_KEY=dummy -e WEBUI_AUTH=False \
	  --add-host=host.docker.internal:host-gateway \
	  -v open-webui:/app/backend/data \
	  ghcr.io/open-webui/open-webui:main >/dev/null
	@echo "  Open WebUI on :3000 — reach it at http://127.0.0.1:3000 through the tunnel"

smoke:
	$(gpu_only)
	@bash $(SETUP)/smoke_lambda.sh
	@bash $(SETUP)/smoke_overflow.sh
