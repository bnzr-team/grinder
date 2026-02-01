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
  - `sample_day_toxic/`: Low-price assets with 6% price jump triggering toxicity gate
    - Paper digest (v1): `66d57776b7be4797`
    - Tests PRICE_IMPACT_HIGH blocking (600 bps > 500 bps threshold)
  - `sample_day_multisymbol/`: 5 symbols to test Top-K prefilter selection (K=3)
    - Paper digest (v1): `7c4f4b07ec7b391f`
    - Expected Top-K selection: AAAUSDT, BBBUSDT, CCCUSDT (highest volatility)
    - DDDUSDT, EEEUSDT filtered out (lower volatility scores)
  - `sample_day_controller/`: 3 symbols to test Adaptive Controller modes
    - Paper digest (v1, controller enabled): `f3a0a321c39cc411`
    - WIDENUSDT: triggers WIDEN mode (high volatility)
    - TIGHTENUSDT: triggers TIGHTEN mode (low volatility)
    - BASEUSDT: triggers BASE mode (normal volatility)
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
- **Prefilter v0** (`src/grinder/prefilter/`):
  - **Hard gates:** rule-based gates returning ALLOW/BLOCK + reason
  - **Top-K selector v0** (`TopKSelector`): selects top K symbols from multisymbol stream
    - Scoring: volatility proxy — sum of absolute mid-price returns in basis points
    - Tie-breakers: higher score first, then lexicographic symbol ascending (deterministic)
    - Default K=3, window_size=10 events per symbol
    - See ADR-010 for design decisions
  - **Limitations:** no adaptive scoring, no stability controls
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
  - `ToxicityGate`: detects adverse market conditions (spread spike, price impact)
    - `SPREAD_SPIKE`: blocks when spread_bps exceeds threshold (default 50 bps)
    - `PRICE_IMPACT_HIGH`: blocks when price moves too fast (default 500 bps over 5s)
    - Per-symbol price history tracking
  - `GatingResult`: standardized result type with allowed/blocked + reason + details
  - `GateName`: stable enum for gate identifiers (`rate_limiter`, `risk_gate`, `toxicity_gate`)
  - `GateReason`: stable enum for block reasons (metric labels)
  - **Metrics** (`GatingMetrics`):
    - `grinder_gating_allowed_total{gate=...}`: counter of allowed decisions
    - `grinder_gating_blocked_total{gate=...,reason=...}`: counter of blocked decisions
    - Export: `to_prometheus_lines()` for /metrics endpoint
  - **Contract tests**: `tests/unit/test_gating_contracts.py` fails if reason codes or metric labels change
  - **Limitations:** no circuit breakers, no position-level checks, PnL tracking is simulated
- **Paper trading v1** (`src/grinder/paper/`):
  - CLI: `grinder paper --fixture <path> [-v] [--out <path>]`
  - **Pipeline:** `Snapshot` -> `Top-K filter` -> `hard_filter()` -> `gating check (toxicity -> rate limit -> risk)` -> `StaticGridPolicy.evaluate()` -> `ExecutionEngine.evaluate()` -> `simulate_fills()` -> `Ledger.apply_fills()` -> `PaperOutput`
  - **Top-K prefilter:** Two-pass processing — first scan for volatility scores, then filter to top K symbols
  - **Gating gates:** toxicity (spread spike, price impact) + rate limit (orders/minute, cooldown) + risk limits (notional, daily loss)
  - **Fill simulation:** All PLACE orders fill immediately at limit price (deterministic)
  - **Position tracking:** Per-symbol qty + avg_entry_price via `Ledger` class
  - **PnL tracking:** Realized (on close), Unrealized (mark-to-market), Total
  - **Output schema v1:** `PaperResult` includes `schema_version`, `total_fills`, `final_positions`, `total_realized_pnl`, `total_unrealized_pnl`, `topk_selected_symbols`, `topk_k`, `topk_scores`
  - **Contract tests:** `tests/unit/test_paper_contracts.py` (27 tests) verify schema stability
  - Output format: `Paper trading completed. Events processed: N\nOutput digest: <16-char-hex>`
  - Deterministic digest for fixture-based runs
  - **Canonical digests:** `sample_day` = `66b29a4e92192f8f`, `sample_day_allowed` = `ec223bce78d7926f`, `sample_day_toxic` = `66d57776b7be4797`, `sample_day_multisymbol` = `7c4f4b07ec7b391f`
  - **Limitations:** no live feed, no real orders, no slippage, no partial fills
- **Adaptive Controller v0** (`src/grinder/controller/`):
  - Rule-based controller that adjusts policy parameters based on recent market conditions
  - **Controller modes:**
    - `BASE` — Normal operation, no adjustment (spacing_multiplier = 1.0)
    - `WIDEN` — High volatility (> 300 bps), widen grid (spacing_multiplier = 1.5)
    - `TIGHTEN` — Low volatility (< 50 bps), tighten grid (spacing_multiplier = 0.8)
    - `PAUSE` — Wide spread (> 50 bps), no new orders
  - **Priority order:** PAUSE > WIDEN > TIGHTEN > BASE
  - **Window-based metrics:** vol_bps (sum of abs mid returns), spread_bps_max (max spread in window)
  - **Determinism:** All metrics use integer basis points (no floats)
  - **Opt-in:** Disabled by default (`controller_enabled=False`) to preserve backward compatibility
  - **Integration:** Runs after Top-K selection, before policy evaluation
  - **Test fixture:** `sample_day_controller` with 3 symbols triggering WIDEN/TIGHTEN/BASE modes
    - Paper digest (v1, controller enabled): `f3a0a321c39cc411`
  - **Contract tests:** `tests/unit/test_controller.py` (20 tests), `tests/unit/test_backtest.py::TestControllerContract` (6 tests)
  - See ADR-011 for design decisions
  - **Limitations:** no EMA-based adaptive step, no trend detection, no DRAWDOWN mode
- **Backtest protocol v1** (`scripts/run_backtest.py`):
  - CLI: `python -m scripts.run_backtest [--out <path>] [--quiet]`
  - Runs paper trading on registered fixtures and generates JSON report
  - **Registered fixtures:** `sample_day`, `sample_day_allowed`, `sample_day_toxic`, `sample_day_multisymbol`
  - **Report schema v1:** `report_schema_version`, `paper_schema_version`, `fixtures_run`, `fixtures_passed`, `fixtures_failed`, `all_digests_match`, `results`, `report_digest`
  - **Per-fixture result:** `fixture_path`, `schema_version`, `paper_digest`, `expected_paper_digest`, `digest_match`, `total_fills`, `final_positions`, `total_realized_pnl`, `total_unrealized_pnl`, `events_processed`, `orders_placed`, `orders_blocked`, `errors`, `topk_selected_symbols`, `topk_k`
  - **Top-K output:** Each fixture result includes symbols selected by Top-K prefilter and the K value used (see ADR-010)
  - **Digest validation:** Compares paper_digest against expected_paper_digest in fixture config.json
  - **Exit code:** 0 if all fixtures pass, 1 if any fail or digest mismatch
  - **Contract tests:** `tests/unit/test_backtest.py` verifies schema stability, determinism, and Top-K fields
  - **Limitations:** no custom fixture list (hardcoded), no parallel execution
- **Observability v0** (`src/grinder/observability/`):
  - `MetricsBuilder`: consolidates all metrics into Prometheus format
  - `build_metrics_output()`: convenience function for /metrics endpoint
  - **Exported via `/metrics`**: system metrics (grinder_up, grinder_uptime_seconds) + gating metrics
  - **Contract tests**: `tests/unit/test_observability.py` verifies metric names and labels
  - **Live runtime contract** (`src/grinder/observability/live_contract.py`):
    - Pure functions for testable HTTP responses (no network required)
    - `build_healthz_body()`: returns JSON with `status`, `uptime_s`
    - `build_metrics_body()`: returns Prometheus format metrics
    - `REQUIRED_HEALTHZ_KEYS`: stable keys that must appear in /healthz response
    - `REQUIRED_METRICS_PATTERNS`: stable patterns that must appear in /metrics response
    - **Contract:**
      - `GET /healthz`: status 200, content-type `application/json`, body includes `{"status": "ok", "uptime_s": <float>}`
      - `GET /metrics`: status 200, content-type `text/plain`, body includes `grinder_up 1`, `grinder_uptime_seconds`, gating metrics
    - **Contract tests**: `tests/unit/test_live_contracts.py` verifies response structure and required patterns
- **Observability stack v0** (`docker-compose.observability.yml`):
  - Docker Compose stack: grinder + Prometheus + Grafana
  - **Commands:**
    - Start: `docker compose -f docker-compose.observability.yml up --build -d`
    - Status: `docker compose -f docker-compose.observability.yml ps`
    - Stop: `docker compose -f docker-compose.observability.yml down -v`
  - **Ports:** grinder:9090, Prometheus:9091, Grafana:3000
  - **Grafana:** http://localhost:3000 (admin/admin), anonymous read access enabled
  - **Dashboard:** GRINDER Overview (auto-provisioned) with status, uptime, gating metrics
  - **Alert rules:** GrinderDown, GrinderTargetDown, HighGatingBlocks, ToxicityTriggers
  - **Smoke test:** `bash scripts/docker_smoke_observability.sh` validates full stack health
  - **CI:** `docker_smoke.yml` runs smoke test on PRs touching Dockerfile/compose/monitoring/src/scripts
  - See `docs/OBSERVABILITY_STACK.md` for full documentation
- **Determinism Gate v1** (`scripts/verify_determinism_suite.py`):
  - CI gate that catches silent drift across all fixtures and backtest
  - **Checks performed:**
    - For each fixture: run replay twice, assert identical digest
    - For each fixture: run paper twice, assert identical digest
    - For each fixture: assert digests match expected values in `config.json`
    - Run backtest twice, assert identical `report_digest`
  - **CLI:** `python -m scripts.verify_determinism_suite [-v] [-q]`
  - **Exit codes:** 0 if all pass, 1 on any mismatch/drift
  - **CI:** `determinism_suite.yml` runs on PRs touching `src/**`, `scripts/**`, `tests/**`, `docs/DECISIONS.md`, `docs/STATE.md`
  - **Fixture discovery:** auto-discovers fixtures with `config.json` under `tests/fixtures/`
  - **Output:** per-fixture summary table + final PASS/FAIL verdict
- **Live HTTP Integration Tests** (`tests/integration/test_live_http.py`):
  - End-to-end tests that spawn `scripts/run_live.py` and validate HTTP contracts
  - **Endpoints tested:**
    - `GET /healthz` - JSON with required keys, `status="ok"`, `uptime_s >= 0`
    - `GET /metrics` - Prometheus text format with all `REQUIRED_METRICS_PATTERNS`
  - **Run:** `PYTHONPATH=src pytest tests/integration -v`
  - **Marker:** `@pytest.mark.integration` (run with `-m integration` to filter)
  - **Dependencies:** Uses `REQUIRED_HEALTHZ_KEYS` and `REQUIRED_METRICS_PATTERNS` from `live_contract.py`
  - **Included in CI:** runs as part of standard `pytest` invocation
- **DataConnector protocol v0** (`src/grinder/connectors/data_connector.py`):
  - Abstract base class defining narrow contract: `connect()`, `close()`, `iter_snapshots()`, `reconnect()`
  - **ConnectorState:** DISCONNECTED → CONNECTING → CONNECTED → RECONNECTING → CLOSED
  - **RetryConfig:** Exponential backoff with cap (`base_delay_ms`, `backoff_multiplier`, `max_delay_ms`)
  - **TimeoutConfig:** Connection and read timeouts (`connect_timeout_ms`, `read_timeout_ms`)
  - **Idempotency:** `last_seen_ts` property for duplicate detection
  - See ADR-012 for design decisions
- **BinanceWsMockConnector v0** (`src/grinder/connectors/binance_ws_mock.py`):
  - Mock connector that reads from fixture files (events.jsonl) and emits `Snapshot`
  - **Features:**
    - Fixture loading from `events.jsonl` or `events.json`
    - Configurable read delay for simulating real-time
    - Symbol filtering (`symbols` parameter)
    - Idempotency via timestamp tracking (skips duplicate/old timestamps)
    - Reconnect with position preservation (`last_seen_ts`)
    - Statistics tracking (`MockConnectorStats`)
  - **Usage:**
    ```python
    connector = BinanceWsMockConnector(Path("tests/fixtures/sample_day"))
    await connector.connect()
    async for snapshot in connector.iter_snapshots():
        print(f"Got {snapshot.symbol} @ {snapshot.mid_price}")
    await connector.close()
    ```
  - **Unit tests:** `tests/unit/test_data_connector.py` (28 tests)
  - **Integration tests:** `tests/integration/test_connector_integration.py` (8 tests)
  - **Limitations:** no live WebSocket, retry logic is interface-only (not used in mock)

## Partially implemented
- Структура пакета `src/grinder/*` (core, protocols/interfaces) — каркас.
- Документация в `docs/*` — SSOT по архитектуре/спекам (но должна совпадать с реализацией).

## Known gaps / mismatches
- Нет реальной торговой логики — только skeleton/stubs.
- Adaptive Grid Controller v1+ (EMA-based adaptive step, trend detection, DRAWDOWN mode, auto-reset) — **not implemented**; see `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` (Planned). Controller v0 implemented with rule-based modes (see ADR-011).
- Нет интеграции с Binance API (только интерфейсы).
- ML pipeline (`src/grinder/ml/`) — пустой placeholder.

## Process / governance
- PR template с обязательной секцией `## Proof`.
- CI guard (`pr_body_guard.yml`) блокирует PR без Proof Bundle.
- CLAUDE.md + DECISIONS.md + STATE.md — governance docs.

## Planned next
- Расширить тесты до >50% coverage.
- Adaptive Controller v1 (EMA-based adaptive step, trend detection, DRAWDOWN mode).
- Live Binance WebSocket connector (using DataConnector protocol).
