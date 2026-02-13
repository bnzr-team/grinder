# GRINDER Makefile
# Shortcuts for common development tasks

.PHONY: help test lint format check determinism replay gates

help:
	@echo "GRINDER Development Shortcuts"
	@echo ""
	@echo "Quality Gates:"
	@echo "  make lint       - Run ruff check"
	@echo "  make format     - Run ruff format --check"
	@echo "  make check      - Run mypy"
	@echo "  make test       - Run pytest"
	@echo "  make gates      - Run all quality gates (lint + format + check + test)"
	@echo ""
	@echo "Determinism:"
	@echo "  make determinism - Run verify_determinism_suite (11 fixtures)"
	@echo "  make replay      - Run verify_replay_determinism"
	@echo ""
	@echo "All:"
	@echo "  make all        - Run gates + determinism"

# Quality gates
lint:
	ruff check .

format:
	ruff format --check .

check:
	mypy .

test:
	pytest -q

gates: lint format check test

# Determinism
determinism:
	python -m scripts.verify_determinism_suite

replay:
	python -m scripts.verify_replay_determinism

# Combined target
all: gates determinism
