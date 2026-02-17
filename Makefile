# GRINDER Makefile
# Shortcuts for common development tasks

# Use venv python if available
VENV_PY := .venv/bin/python
VENV_EXISTS := $(shell [ -x "$(VENV_PY)" ] && echo 1)

.PHONY: help test lint format check determinism replay gates all venv-check fingerprint

# Fail fast if venv doesn't exist
venv-check:
ifndef VENV_EXISTS
	$(error venv not found at $(VENV_PY). Create it: python -m venv .venv && .venv/bin/pip install -e ".[dev]")
endif

help:
	@echo "GRINDER Development Shortcuts"
	@echo ""
	@echo "Quality Gates:"
	@echo "  make fingerprint - Print env fingerprint (python, venv, packages)"
	@echo "  make lint        - Run ruff check"
	@echo "  make format      - Run ruff format --check"
	@echo "  make check       - Run mypy"
	@echo "  make test        - Run pytest"
	@echo "  make gates       - Run all quality gates (fingerprint + lint + format + check + test)"
	@echo ""
	@echo "Determinism:"
	@echo "  make determinism - Run verify_determinism_suite (11 fixtures)"
	@echo "  make replay      - Run verify_replay_determinism"
	@echo ""
	@echo "All:"
	@echo "  make all        - Run gates + determinism"

# Env fingerprint (always first in gates)
fingerprint: venv-check
	$(VENV_PY) -m scripts.env_fingerprint

# Quality gates
lint: venv-check
	$(VENV_PY) -m ruff check .

format: venv-check
	$(VENV_PY) -m ruff format --check .

check: venv-check
	$(VENV_PY) -m mypy .

test: venv-check
	$(VENV_PY) -m pytest -q

gates: fingerprint lint format check test

# Determinism
determinism: venv-check
	$(VENV_PY) -m scripts.verify_determinism_suite

replay: venv-check
	$(VENV_PY) -m scripts.verify_replay_determinism

# Combined target
all: gates determinism
