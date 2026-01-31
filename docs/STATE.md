# STATE — Current Implementation Status

Цель: фиксировать **что реально работает сейчас** (а не что хотелось бы). Обновлять в каждом PR, если изменилось.

Next steps and progress tracker: `docs/ROADMAP.md`.

## Works now
- `grinder --help` / `grinder-paper --help` / `grinder-backtest --help` — CLI entrypoints работают.
- `python -m scripts.run_live` поднимает `/healthz` и `/metrics`:
  - `/healthz`: JSON health check (status, uptime)
  - `/metrics`: Prometheus format including system metrics + gating metrics
- `python -m scripts.run_soak` генерирует synthetic soak metrics JSON.
- **Test fixtures** (`tests/fixtures/`):
  - `sample_day/`: BTC/ETH prices, orders blocked by gating (notional too high)
    - Replay digest: `453ebd0f655e4920`
    - Paper digest (v1): `66b29a4e92192f8f`
  - `sample_day_allowed/`: Low-price assets (~$1), orders pass prefilter + gating
    - Replay digest: `03253d84cd2604e7`
    - Paper digest (v1): `ec223bce78d7926f`
  - Fixture format: SNAPSHOT events (see ADR-006 for migration from BOOK_TICKER)
  - Schema version: v1 (see ADR-008)
- **End-to-end replay**:
  - CLI: `grinder replay --fixture <path> [-v] [--out <path>]`
  - Script: `python -m scripts.run_replay --fixture <path> [-v] [--out <path>]`
  - Determinism check: `python -m scripts.verify_replay_determinism --fixture <path>`
  - Output format: `Replay completed. Events processed: N\nOutput digest: <16-char-hex>`
- `python -m scripts.secret_guard` проверяет repo на утечки секретов.
- `python scripts/check_unicode.py` сканирует docs на опасный Unicode (bidi, zero-width). См. ADR-005.
- Docker build + healthcheck работают (Dockerfile использует `urllib.request` вместо `curl`).
- Grafana provisioning: `monitoring/grafana/provisioning/` содержит datasource + dashboard.
- Branch protection на `main`: все PR требуют 5 зелёных checks.
- **Domain contracts** (`src/grinder/contracts.py`): Snapshot, Position, PolicyContext, OrderIntent, Decision — typed, frozen, JSON-serializable. См. ADR-003.
- **Prefilter v0** (`src/grinder/prefilter/`): rule-based hard gates returning ALLOW/BLOCK + reason. Limitations: only hard gates, no scoring/ranking/top-K, no stability controls.
- **GridPolicy v0** (`src/grinder/policies/grid/static.py`): StaticGridPolicy producing symmetric bilateral grids. GridPlan includes: regime, width_bps, reset_action, reason_codes. Limitations: no adaptive step, no inventory skew, no regime switching.
- **Execution stub v0** (`src/grinder/execution/`): ExchangePort protocol + NoOpExchangePort stub, ExecutionEngine with reconcile logic (PAUSE/EMERGENCY -> cancel all, HARD reset -> rebuild grid, SOFT reset -> replace non-conforming, NONE -> reconcile). Deterministic order ID generation. ExecutionMetrics for observability. Limitations: no live exchange writes, no rate limiting, no error recovery.
- **Replay engine v0** (`src/grinder/replay/`):
  - **Responsibilities:** Load fixture -> parse SNAPSHOT events -> apply prefilter gates -> evaluate policy -> execute via ExecutionEngine -> compute deterministic digest
  - **Components:** `ReplayEngine` (orchestrator), `ReplayOutput` (per-tick output), `ReplayResult` (full run result)
  - **Pipeline:** `Snapshot` -> `hard_filter()` -> `StaticGridPolicy.evaluate()` -> `ExecutionEngine.evaluate()` -> `ReplayOutput`
  - **Digest:** SHA256 of JSON-serialized outputs, truncated to 16 hex chars
  - **Expected digest:** `453ebd0f655e4920` for `tests/fixtures/sample_day`
  - **Limitations:** single policy (StaticGridPolicy), no custom feature injection, volume/OI assumed sufficient for replay
- **Gating v0** (`src/grinder/gating/`):
  - `RateLimiter`: sliding window rate limit (max orders/minute) + cooldown between orders
  - `RiskGate`: per-symbol and total notional limits + daily loss limit
  - `GatingResult`: standardized result type with allowed/blocked + reason + details
  - `GateName`: stable enum for gate identifiers (metric labels)
  - `GateReason`: stable enum for block reasons (metric labels)
  - **Metrics** (`GatingMetrics`):
    - `grinder_gating_allowed_total{gate=...}`: counter of allowed decisions
    - `grinder_gating_blocked_total{gate=...,reason=...}`: counter of blocked decisions
    - Export: `to_prometheus_lines()` for /metrics endpoint
  - **Contract tests**: `tests/unit/test_gating_contracts.py` fails if reason codes or metric labels change
  - **Limitations:** no circuit breakers, no position-level checks, PnL tracking is simulated
- **Paper trading v1** (`src/grinder/paper/`):
  - CLI: `grinder paper --fixture <path> [-v] [--out <path>]`
  - **Pipeline:** `Snapshot` -> `hard_filter()` -> `gating check` -> `StaticGridPolicy.evaluate()` -> `ExecutionEngine.evaluate()` -> `simulate_fills()` -> `Ledger.apply_fills()` -> `PaperOutput`
  - **Gating gates:** rate limit (orders/minute, cooldown) + risk limits (notional, daily loss)
  - **Fill simulation:** All PLACE orders fill immediately at limit price (deterministic)
  - **Position tracking:** Per-symbol qty + avg_entry_price via `Ledger` class
  - **PnL tracking:** Realized (on close), Unrealized (mark-to-market), Total
  - **Output schema v1:** `PaperResult` includes `schema_version`, `total_fills`, `final_positions`, `total_realized_pnl`, `total_unrealized_pnl`
  - **Contract tests:** `tests/unit/test_paper_contracts.py` (27 tests) verify schema stability
  - Output format: `Paper trading completed. Events processed: N\nOutput digest: <16-char-hex>`
  - Deterministic digest for fixture-based runs
  - **Canonical digests:** `sample_day` = `66b29a4e92192f8f`, `sample_day_allowed` = `ec223bce78d7926f`
  - **Limitations:** no live feed, no real orders, no slippage, no partial fills
- **Backtest protocol v1** (`scripts/run_backtest.py`):
  - CLI: `python -m scripts.run_backtest [--out <path>] [--quiet]`
  - Runs paper trading on registered fixtures and generates JSON report
  - **Registered fixtures:** `sample_day`, `sample_day_allowed`
  - **Report schema v1:** `report_schema_version`, `paper_schema_version`, `fixtures_run`, `fixtures_passed`, `fixtures_failed`, `all_digests_match`, `results`, `report_digest`
  - **Per-fixture result:** `fixture_path`, `schema_version`, `paper_digest`, `expected_paper_digest`, `digest_match`, `total_fills`, `final_positions`, `total_realized_pnl`, `total_unrealized_pnl`, `events_processed`, `orders_placed`, `orders_blocked`, `errors`
  - **Digest validation:** Compares paper_digest against expected_paper_digest in fixture config.json
  - **Exit code:** 0 if all fixtures pass, 1 if any fail or digest mismatch
  - **Contract tests:** `tests/unit/test_backtest.py` verifies schema stability and determinism
  - **Limitations:** no custom fixture list (hardcoded), no parallel execution
- **Observability v0** (`src/grinder/observability/`):
  - `MetricsBuilder`: consolidates all metrics into Prometheus format
  - `build_metrics_output()`: convenience function for /metrics endpoint
  - **Exported via `/metrics`**: system metrics (grinder_up, grinder_uptime_seconds) + gating metrics
  - **Contract tests**: `tests/unit/test_observability.py` verifies metric names and labels

## Partially implemented
- Структура пакета `src/grinder/*` (core, protocols/interfaces) — каркас.
- Документация в `docs/*` — SSOT по архитектуре/спекам (но должна совпадать с реализацией).

## Known gaps / mismatches
- Нет реальной торговой логики — только skeleton/stubs.
- Adaptive Grid Controller (regime selection, adaptive step, auto-reset) — **not implemented**; see `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` (Planned).
- Нет интеграции с Binance API (только интерфейсы).
- ML pipeline (`src/grinder/ml/`) — пустой placeholder.

## Process / governance
- PR template с обязательной секцией `## Proof`.
- CI guard (`pr_body_guard.yml`) блокирует PR без Proof Bundle.
- CLAUDE.md + DECISIONS.md + STATE.md — governance docs.

## Planned next
- Реализовать минимальный data connector (Binance WebSocket mock).
- Расширить тесты до >50% coverage.
- Adaptive Controller implementation (regime + step + reset).
