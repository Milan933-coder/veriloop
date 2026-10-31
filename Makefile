# Developer shortcuts (GNU make; run from the repo root, Git Bash on Windows).
# The venv lives outside OneDrive in VERILOOP_HOME; override PY to use another interpreter.
VERILOOP_HOME ?= $(HOME)/.veriloop
ifeq ($(OS),Windows_NT)
PY ?= $(VERILOOP_HOME)/venv/Scripts/python.exe
else
PY ?= $(VERILOOP_HOME)/venv/bin/python
endif

.PHONY: help install test test-network test-ml lint run smoke docker-up docker-down

help:
	@echo "install       uv venv + editable backend install (CPU torch)"
	@echo "test          unit tests (no network, no model downloads)"
	@echo "test-network  live aicredits smoke tests (needs LLM_API_KEY in .env)"
	@echo "test-ml       real Embedder/NLI load (downloads weights once into VERILOOP_HOME/models)"
	@echo "lint          ruff check"
	@echo "run           start the API (python -m veriloop)"
	@echo "docker-up     build + start api/db/caddy with docker compose"

install:
	uv venv --python 3.12 $(VERILOOP_HOME)/venv
	uv pip install --python $(PY) -e "backend[ml,dev,c2pa]" \
		--extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match

test:
	$(PY) -m pytest backend/tests -q

test-network:
	$(PY) -m pytest backend/tests -q -m network -s

test-ml:
	$(PY) -m pytest backend/tests -q -m ml -s

lint:
	$(PY) -m ruff check backend

run:
	cd backend && $(PY) -m veriloop

docker-up:
	docker compose -f deploy/docker-compose.yml up -d --build

docker-down:
	docker compose -f deploy/docker-compose.yml down
