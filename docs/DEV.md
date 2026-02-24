# DEV.md — Developer Environment Setup

Single source of truth for local development and proof bundle verification.

## Quick Start

### Recommended: Virtual Environment

```bash
# Create and activate venv
python3 -m venv .venv
source .venv/bin/activate

# Upgrade pip and install in editable mode
python -m pip install -U pip
pip install -e .

# Verify installation
grinder --help
```

### Fallback: PYTHONPATH (PEP 668 systems)

On systems with externally-managed Python (Ubuntu 23.04+, Debian 12+), `pip install` may fail with:

```
error: externally-managed-environment
```

**Solution 1 (recommended):** Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
PYTHONPATH=src python3 -m pytest -q
```

**Solution 2 (partial):** Use `PYTHONPATH=src` without full installation:

```bash
PYTHONPATH=src python3 -m pytest tests/unit/test_core.py -q
PYTHONPATH=src python3 -m scripts.run_backtest
```

**Note:** Solution 2 skips HA tests (require `redis` package). For full test suite including HA tests, use Solution 1 with venv.

CI runs with all dependencies installed (see `.github/workflows/ci.yml`).

## Proof Bundle Commands

These are the exact commands required for PR approval. Copy-paste and run in order:

### 0. Prerequisites

Smokes and the trading loop require an editable install with all dependencies.
Without this, imports will fail and smoke assertions will be misleading.

```bash
python3 -m venv .venv
source .venv/bin/activate

# Minimum for smokes + trading loop (runtime deps only):
pip install -e .

# Full dev proof bundle (adds pytest, mypy, ruff, types-pyyaml stubs):
pip install -e ".[dev]"
```

### 1. Quality Gates

```bash
# Tests (3100+ expected)
python3 -m pytest -q

# Linting
ruff check .

# Formatting
ruff format --check .

# Type checking (must be run in dev env — needs types-pyyaml stubs)
mypy .

# Unicode security scan
python3 scripts/check_unicode.py --all
```

### 2. Replay Determinism

```bash
# Verify replay produces identical digests across runs
python3 scripts/verify_replay_determinism.py
```

### 3. Paper Trading Determinism

```bash
# Run paper trading (should produce same digest each run)
PYTHONPATH=src python3 -c "
from grinder.paper.cli import main
import sys
sys.argv = ['grinder-paper', '--fixture', 'tests/fixtures/sample_day']
main()
"
```

Or with installed package:

```bash
grinder paper --fixture tests/fixtures/sample_day
```

### 4. Backtest Report

```bash
# Run full backtest suite (5 fixtures, all must match)
PYTHONPATH=src python3 -m scripts.run_backtest

# Quiet mode (exit code only)
PYTHONPATH=src python3 -m scripts.run_backtest --quiet
```

Expected output includes:
- `fixtures_run: 5`
- `all_digests_match: true`
- `report_digest: d7e17b36d4d2c844`

### 5. Smoke Tests

Smokes require an editable install (see [Prerequisites](#0-prerequisites)).
They start real processes on ephemeral ports and verify metrics/output.

```bash
# Futures rehearsal: proves 0 order attempts + 0 HTTP calls with port=futures
bash scripts/smoke_futures_no_orders.sh

# Clean shutdown: no "Task was destroyed" warnings
bash scripts/smoke_no_task_destroyed.sh

# HA metrics invariants: readyz + HA gauges correct
bash scripts/smoke_ha_metrics_invariants.sh
```

Each smoke exits 0 on success, 1 on failure, and prints `=== ALL CHECKS PASSED ===` when green.

### 6. Alert Rules Validator

```bash
# Validates all Prometheus alert rules (syntax, unique names, no forbidden labels)
python3 -m scripts.verify_alert_rules monitoring/alert_rules.yml
```

## CLI Usage

### With Installation (`pip install -e .`)

```bash
grinder --help
grinder paper --fixture tests/fixtures/sample_day
grinder replay --fixture tests/fixtures/sample_day
```

### Without Installation (PYTHONPATH)

```bash
# Paper trading
PYTHONPATH=src python3 -c "from grinder.paper.cli import main; import sys; sys.argv=['grinder-paper', '--fixture', 'tests/fixtures/sample_day']; main()"

# Replay
PYTHONPATH=src python3 -m scripts.run_replay --fixture tests/fixtures/sample_day

# Backtest
PYTHONPATH=src python3 -m scripts.run_backtest
```

## Canonical Digests

Current locked digests (must not change without ADR):

### Replay Digests

| Fixture | Digest |
|---------|--------|
| sample_day | `453ebd0f655e4920` |
| sample_day_allowed | `03253d84cd2604e7` |
| sample_day_controller | `d10809d9587bee21` |

### Paper Digests (schema v1)

| Fixture | Digest | Controller |
|---------|--------|------------|
| sample_day | `66b29a4e92192f8f` | off |
| sample_day_allowed | `ec223bce78d7926f` | off |
| sample_day_toxic | `66d57776b7be4797` | off |
| sample_day_multisymbol | `7c4f4b07ec7b391f` | off |
| sample_day_controller | `f3a0a321c39cc411` | on |

### Backtest Report Digest

| Fixtures | Digest |
|----------|--------|
| 5 fixtures | `d7e17b36d4d2c844` |

## Adding a New Fixture

1. Create fixture directory: `tests/fixtures/<name>/`

2. Add `events.jsonl` with SNAPSHOT events:
   ```json
   {"type":"SNAPSHOT","ts":1000,"symbol":"TESTUSDT","bid_price":"100.00","ask_price":"100.02","bid_qty":"10","ask_qty":"10","last_price":"100.01","last_qty":"1"}
   ```

3. Add `config.json`:
   ```json
   {
     "name": "<name>",
     "description": "Description of fixture purpose",
     "symbols": ["TESTUSDT"],
     "expected_paper_digest": ""
   }
   ```

4. Run paper trading to get digest:
   ```bash
   PYTHONPATH=src python3 -c "from pathlib import Path; from grinder.paper import PaperEngine; print(PaperEngine().run(Path('tests/fixtures/<name>')).digest)"
   ```

5. Update `config.json` with the digest

6. Register in `scripts/run_backtest.py` FIXTURES list

7. Run backtest to get new report digest:
   ```bash
   PYTHONPATH=src python3 -m scripts.run_backtest
   ```

8. Force-add `events.jsonl` (blocked by .gitignore):
   ```bash
   git add -f tests/fixtures/<name>/events.jsonl
   ```

## Common Pitfalls

### PEP 668 "externally-managed-environment"

**Symptom:**
```
error: externally-managed-environment
This environment is externally managed
```

**Fix:** Use venv (recommended) or PYTHONPATH prefix.

**Do NOT use:** `--break-system-packages` (risks system Python corruption).

### Missing events.jsonl in Git

**Symptom:** CI fails with "No events found in fixture"

**Cause:** `*.jsonl` is in `.gitignore`

**Fix:** Force-add fixture event files:
```bash
git add -f tests/fixtures/<name>/events.jsonl
```

### Digest Mismatch After Code Change

If you modify replay/paper/policy/execution code, digests may change.

**Required steps:**
1. Run all fixtures, record new digests
2. Update `config.json` files with new `expected_paper_digest`
3. Update `docs/STATE.md` canonical digests section
4. Add ADR entry in `docs/DECISIONS.md` explaining the change
5. Re-run backtest to get new `report_digest`

### Import Errors

**Symptom:** `ModuleNotFoundError: No module named 'grinder'`

**Fix:** Either install package or use PYTHONPATH:
```bash
# Option 1: Install
pip install -e .

# Option 2: PYTHONPATH
PYTHONPATH=src python3 -m ...
```

### Network Access During Fixture Run

**Symptom:** `FixtureNetworkBlockedError: FIXTURE NETWORK BLOCKED: connection to api.binance.com denied`

**Cause:** Code attempted external network access while `--fixture` is active. The fixture network guard blocks all non-loopback `socket.connect`, `socket.connect_ex`, and `socket.create_connection` calls (see ADR-075).

**Fix:** This is by design — fixture runs are air-gapped. To use live network, run without `--fixture`. The default `NoOpExchangePort` simulates orders in memory without network calls.

## Forbidden Artifacts (Never Commit)

These are in `.gitignore` and must never be committed:

- `__pycache__/` — Python bytecode cache
- `.pytest_cache/` — pytest cache
- `.mypy_cache/` — mypy cache
- `.ruff_cache/` — ruff cache
- `.venv/`, `venv/` — virtual environments
- `.env`, `.env.local` — environment files
- `*.pem`, `*.key` — private keys
- `credentials.json`, `secrets.json` — secrets
- `*.jsonl` — data files (except fixture events, force-added)

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `PYTHONPATH` | Module search path | (none) |
| `GRINDER_LOG_LEVEL` | Logging verbosity | `INFO` |

## IDE Setup

### VSCode

Recommended extensions:
- Python (ms-python.python)
- Ruff (charliermarsh.ruff)
- Mypy Type Checker (ms-python.mypy-type-checker)

Settings (`.vscode/settings.json`):
```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.analysis.extraPaths": ["${workspaceFolder}/src"],
  "ruff.path": ["${workspaceFolder}/.venv/bin/ruff"]
}
```

### PyCharm

1. Mark `src/` as Sources Root
2. Set interpreter to `.venv/bin/python`
3. Enable Ruff plugin
