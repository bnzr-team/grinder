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
  - `sample_day_topk_v1/`: 6 symbols to test Top-K v1 feature-based selection
    - Paper digest (v1, topk_v1 enabled): `63d981b60a8e9b3a`
    - Selected: LOWUSDT (rank 1), HIGHUSDT (rank 2), MEDUSDT (rank 3)
    - Gate-excluded: THINUSDT (THIN_BOOK)
    - Not selected: TRENDUSDT, WIDEUSDT (lower scores)
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
- **Sizing Units SSOT (ADR-018):**
  - `GridPlan.size_schedule` is ALWAYS **base asset quantity** (e.g., BTC, ETH), NOT notional (USD)
  - `notional_to_qty(notional, price, precision)` utility in `src/grinder/policies/base.py` for explicit conversion
  - Formula: `qty = notional / price`, rounded down to precision
  - Example: `notional_to_qty(Decimal("500"), Decimal("50000"))` → `Decimal("0.01")` (0.01 BTC)
  - All code interpreting `size_schedule` MUST treat values as base asset quantity
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
  - **Fill simulation v1.1 (crossing/touch model):** BUY fills if `mid_price <= limit_price`, SELL fills if `mid_price >= limit_price` (deterministic, see ADR-016)
  - **Tick-delay fills v0.1 (LC-03):**
    - Orders stay OPEN for N ticks before fill-eligible (configurable via `fill_after_ticks`)
    - `fill_after_ticks=0` (default): instant/crossing behavior (backward compatible)
    - `fill_after_ticks=1+`: fill on tick N after placement (if price crosses)
    - Order lifecycle: `PLACE → OPEN → (N ticks) → FILLED`
    - Cancel before fill prevents filling
    - Deterministic: same inputs → same fills (no randomness)
    - Added `placed_tick` to `OrderRecord` for tracking
    - Uses per-symbol `tick_counter` in `ExecutionState`
    - 18 unit tests in `tests/unit/test_paper_realism.py`
    - See ADR-034
  - **CycleEngine v1** (`src/grinder/paper/cycle_engine.py`):
    - Converts fills to TP + replenishment intents (§17.12.2)
    - BUY fill → SELL TP at `p_fill * (1 + step_pct)` for same qty
    - SELL fill → BUY TP at `p_fill * (1 - step_pct)` for same qty
    - Replenishment: same-side order further out (only if `adds_allowed=True`)
    - Deterministic intent IDs: `cycle_{type}_{fill_id}_{side}_{price}`
    - Opt-in: `cycle_enabled=False` default (backward compat)
    - Intents NOT included in digest (backward compat)
    - See ADR-017
  - **Feature Engine v1** (`src/grinder/features/`):
    - Deterministic mid-bar OHLC construction from snapshot stream
    - **Bar building:** floor-aligned boundaries, no synthesized bars for gaps
    - **ATR/NATR (§17.5.2):** True Range + period-based averaging (default 14)
    - **L1 features (§17.5.3):** imbalance_l1_bps, thin_l1, spread_bps
    - **Range/trend (§17.5.5):** sum_abs_returns_bps, net_return_bps, range_score
    - **Warmup handling:** features return 0/None until period+1 bars complete
    - **Determinism:** all calcs use Decimal, outputs as integer bps or Decimal
    - **Unit tests:** 83 tests (test_bar_builder.py, test_indicators.py, test_feature_engine.py)
    - **PaperEngine integration (ASM-P1-02):**
      - `feature_engine_enabled=False` default (backward compat)
      - `PaperOutput.features: dict | None` — computed features per snapshot
      - Features NOT in digest (backward compat, Variant A)
      - Canonical digests unchanged when features enabled
      - 9 integration tests in `test_paper.py::TestFeatureEngineIntegration`
    - See ADR-019
  - **Position tracking:** Per-symbol qty + avg_entry_price via `Ledger` class
  - **PnL tracking:** Realized (on close), Unrealized (mark-to-market), Total
  - **Output schema v1:** `PaperResult` includes `schema_version`, `total_fills`, `final_positions`, `total_realized_pnl`, `total_unrealized_pnl`, `topk_selected_symbols`, `topk_k`, `topk_scores`
  - **Contract tests:** `tests/unit/test_paper_contracts.py` (27 tests) verify schema stability
  - Output format: `Paper trading completed. Events processed: N\nOutput digest: <16-char-hex>`
  - Deterministic digest for fixture-based runs
  - **Canonical digests (v1.1):** `sample_day` = `66b29a4e92192f8f`, `sample_day_allowed` = `3ecf49cd03db1b07`, `sample_day_toxic` = `a31ead72fc1f197e`, `sample_day_multisymbol` = `22acba5cb8b81ab4`
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
    - `build_readyz_body()`: returns tuple of (JSON body, is_ready bool) based on HA role
    - `build_metrics_body()`: returns Prometheus format metrics
    - `REQUIRED_HEALTHZ_KEYS`: stable keys that must appear in /healthz response
    - `REQUIRED_READYZ_KEYS`: stable keys that must appear in /readyz response (`ready`, `role`)
    - `REQUIRED_METRICS_PATTERNS`: stable patterns that must appear in /metrics response
    - **Contract:**
      - `GET /healthz`: status 200, content-type `application/json`, body includes `{"status": "ok", "uptime_s": <float>}`
      - `GET /readyz`: status 200 if ACTIVE, 503 if STANDBY/UNKNOWN, content-type `application/json`, body includes `{"ready": true/false, "role": "<role>"}`
      - `GET /metrics`: status 200, content-type `text/plain`, body includes `grinder_up 1`, `grinder_uptime_seconds`, `grinder_ha_role`, gating metrics
    - **Contract tests**: `tests/unit/test_live_contracts.py` verifies response structure and required patterns
- **Observability stack v0** (`docker-compose.observability.yml`):
  - Docker Compose stack: grinder + Prometheus + Grafana
  - **Commands:**
    - Start: `docker compose -f docker-compose.observability.yml up --build -d`
    - Status: `docker compose -f docker-compose.observability.yml ps`
    - Stop: `docker compose -f docker-compose.observability.yml down -v`
  - **Ports:** grinder:9090, Prometheus:9091, Grafana:3000
  - **Grafana:** http://localhost:3000 (admin/admin), anonymous read access enabled
  - **Dashboard:** GRINDER Overview (auto-provisioned) with status, uptime, gating, kill-switch metrics
  - **Alert rules:** GrinderDown, GrinderTargetDown, HighGatingBlocks, ToxicityTriggers, KillSwitchTripped
  - **Smoke test:** `bash scripts/docker_smoke_observability.sh` validates full stack health
  - **CI:** `docker_smoke.yml` runs smoke test on PRs touching Dockerfile/compose/monitoring/src/scripts
  - See `docs/OBSERVABILITY_STACK.md` for full documentation
- **Metrics Contract v1** (exported via `/metrics`):
  - **System metrics:**
    - `grinder_up` (gauge): 1 if running, 0 if down
    - `grinder_uptime_seconds` (gauge): uptime in seconds since start
  - **Gating metrics:**
    - `grinder_gating_allowed_total{gate}` (counter): allowed decisions by gate name
    - `grinder_gating_blocked_total{gate,reason}` (counter): blocked decisions by gate and reason
    - Gate names: `rate_limiter`, `risk_gate`, `toxicity_gate`
    - Block reasons: `RATE_LIMIT_EXCEEDED`, `COOLDOWN_ACTIVE`, `NOTIONAL_LIMIT`, `DAILY_LOSS_LIMIT`, `SPREAD_SPIKE`, `PRICE_IMPACT_HIGH`, `KILL_SWITCH_ACTIVE`
  - **Risk metrics:**
    - `grinder_kill_switch_triggered` (gauge): 1 if kill-switch is active, 0 otherwise
    - `grinder_kill_switch_trips_total{reason}` (counter): total trips by reason
    - `grinder_drawdown_pct` (gauge): current drawdown percentage (0-100)
    - `grinder_high_water_mark` (gauge): current equity high-water mark
  - **HA metrics:**
    - `grinder_ha_role{role}` (gauge): 1 for current role (active/standby/unknown)
  - **Connector metrics (H5 Observability):**
    - `grinder_connector_retries_total{op, reason}` (counter): retry events by operation and reason
    - `grinder_idempotency_hits_total{op}` (counter): idempotency cache hits
    - `grinder_idempotency_conflicts_total{op}` (counter): idempotency conflicts (INFLIGHT duplicates)
    - `grinder_idempotency_misses_total{op}` (counter): idempotency cache misses
    - `grinder_circuit_state{op, state}` (gauge): circuit breaker state (1 for current, 0 for others)
    - `grinder_circuit_rejected_total{op}` (counter): calls rejected by OPEN circuit
    - `grinder_circuit_trips_total{op, reason}` (counter): circuit trips (CLOSED → OPEN)
    - Labels: `op` = operation name, `reason` = transient/timeout/other/threshold, `state` = closed/open/half_open
    - See ADR-028 for design decisions
  - **Contract tests:** `tests/unit/test_live_contracts.py`, `tests/unit/test_observability.py`, `tests/unit/test_connector_metrics.py`
  - **SSOT:** This section is the canonical list of exported metrics
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
- **DataConnector v1** (`src/grinder/connectors/data_connector.py`):
  - Abstract base class (ABC) defining narrow contract: `connect()`, `close()`, `iter_snapshots()`, `reconnect()`
  - **ConnectorState:** DISCONNECTED → CONNECTING → CONNECTED → RECONNECTING → CLOSED
  - **RetryConfig:** Exponential backoff with cap (`base_delay_ms`, `backoff_multiplier`, `max_delay_ms`)
  - **TimeoutConfig (extended):**
    - `connect_timeout_ms` (5000ms default): timeout for initial connection
    - `read_timeout_ms` (10000ms default): timeout for reading next snapshot
    - `write_timeout_ms` (5000ms default): timeout for write operations
    - `close_timeout_ms` (5000ms default): timeout for graceful shutdown
  - **Error hierarchy** (`src/grinder/connectors/errors.py`):
    - `ConnectorError` — base exception for all connector errors
    - `ConnectorTimeoutError(op, timeout_ms)` — timeout during connect/read/write/close
    - `ConnectorClosedError(op)` — operation attempted on closed connector
    - `ConnectorIOError` — base for I/O errors
    - `ConnectorTransientError` — retryable errors (network, 5xx, 429)
    - `ConnectorNonRetryableError` — non-retryable errors (4xx, auth, validation)
  - **Retry utilities (H2)** (`src/grinder/connectors/retries.py`):
    - `RetryPolicy` — frozen dataclass: `max_attempts`, `base_delay_ms`, `max_delay_ms`, `backoff_multiplier`, `retry_on_timeout`
    - `RetryStats` — tracks `attempts`, `retries`, `total_delay_ms`, `last_error`, `errors`
    - `is_retryable(error, policy)` — classifies errors as retryable/non-retryable
    - `retry_with_policy(op_name, operation, policy, sleep_func, on_retry)` — async retry wrapper with exponential backoff
    - `sleep_func` parameter enables bounded-time testing (no real sleeps)
  - **Idempotency utilities (H3)** (`src/grinder/connectors/idempotency.py`):
    - `IdempotencyStatus` — enum: `INFLIGHT`, `DONE`, `FAILED`
    - `IdempotencyEntry` — dataclass: key, status, op_name, fingerprint, timestamps, result
    - `IdempotencyStore` — protocol for pluggable storage
    - `InMemoryIdempotencyStore` — thread-safe in-memory implementation with injectable clock
    - `compute_idempotency_key(scope, op, **params)` — deterministic key from canonical payload (ts excluded from key)
    - `IdempotencyConflictError` — fast-fail on INFLIGHT duplicates
  - **IdempotentExchangePort (H3)** (`src/grinder/execution/idempotent_port.py`):
    - Wraps `ExchangePort` with idempotency guarantees for place/cancel/replace
    - Same request with same key returns cached result (DONE)
    - Concurrent duplicates fail fast with `IdempotencyConflictError` (INFLIGHT)
    - FAILED entries allow retry (overwritable)
    - Integrates with H2 retries: key created once, all retries use same key → 1 side-effect
    - Stats tracking: `place_calls`, `place_cached`, `place_executed`, `place_conflicts`
  - **Circuit Breaker (H4)** (`src/grinder/connectors/circuit_breaker.py`):
    - `CircuitState` — enum: `CLOSED`, `OPEN`, `HALF_OPEN`
    - `CircuitBreakerConfig` — failure_threshold, open_interval_s, half_open_probe_count, success_threshold, trip_on
    - `CircuitBreaker` — per-operation circuit breaker with injectable clock
    - `before_call(op)` / `allow(op)` — fast-fail when OPEN, limited probes in HALF_OPEN
    - `record_success(op)` / `record_failure(op, reason)` — state transitions
    - `CircuitOpenError` — non-retryable error raised when circuit is OPEN
    - `default_trip_on` — trips on `ConnectorTransientError`, `ConnectorTimeoutError`
    - Per-op isolation: place can be OPEN while cancel stays CLOSED
    - **Status: Wired into IdempotentExchangePort** (H4-02)
    - Integration order: breaker.before_call → idempotency → execute → record_success/failure
  - **Timeout utilities** (`src/grinder/connectors/timeouts.py`):
    - `wait_for_with_op(coro, timeout_ms, op)` — wraps `asyncio.wait_for` with `ConnectorTimeoutError`
    - `cancel_tasks_with_timeout(tasks, timeout_ms)` — clean task cancellation
    - `create_named_task(coro, name, tasks_set)` — tracked task creation
  - **Idempotency:** `last_seen_ts` property for duplicate detection
  - See ADR-012 (design), ADR-024 (H1 hardening), ADR-025 (H2 retries), ADR-026 (H3 idempotency), ADR-027 (H4 circuit breaker)
- **BinanceWsMockConnector v1** (`src/grinder/connectors/binance_ws_mock.py`):
  - Mock connector that reads from fixture files (events.jsonl) and emits `Snapshot`
  - **Features:**
    - Fixture loading from `events.jsonl` or `events.json`
    - Configurable read delay for simulating real-time
    - Symbol filtering (`symbols` parameter)
    - Idempotency via timestamp tracking (skips duplicate/old timestamps)
    - Reconnect with position preservation (`last_seen_ts`)
    - Statistics tracking (`MockConnectorStats`)
  - **Timeout enforcement (H1):**
    - Connect timeout via `wait_for_with_op()` — raises `ConnectorTimeoutError` on timeout
    - Read timeout during iteration — raises `ConnectorTimeoutError` if read_delay exceeds timeout
    - Stats track `timeouts` count and `errors` list
  - **Clean shutdown (H1):**
    - Task tracking via `self._tasks` set with named prefix
    - `close()` cancels all tasks via `cancel_tasks_with_timeout()`
    - Waits for completion within `close_timeout_ms`
    - Clears events, cursor; sets state to CLOSED
    - Stats track `tasks_cancelled` and `tasks_force_killed`
    - **No zombie guarantee:** all tasks awaited or force-killed
  - **Usage:**
    ```python
    connector = BinanceWsMockConnector(Path("tests/fixtures/sample_day"))
    await connector.connect()
    async for snapshot in connector.iter_snapshots():
        print(f"Got {snapshot.symbol} @ {snapshot.mid_price}")
    await connector.close()
    ```
  - **Transient failure simulation (H2):**
    - `TransientFailureConfig(connect_failures, read_failures, failure_message)` for testing retry logic
    - `connect_failures`: N connect attempts fail with `ConnectorTransientError` before success
    - `read_failures`: N read operations fail before success
    - Stats track `transient_failures_injected` count
  - **Unit tests:** `tests/unit/test_data_connector.py` (47 tests), `tests/unit/test_retries.py` (35 tests)
  - **Integration tests:** `tests/integration/test_connector_integration.py` (8 tests)
  - **Limitations:** no live WebSocket
  - See ADR-024 (H1 hardening), ADR-025 (H2 retries)
- **LiveConnectorV0** (`src/grinder/connectors/live_connector.py`):
  - Live WebSocket connector with SafeMode enforcement (read_only/paper/live_trade)
  - **Features:**
    - `SafeMode` enum: `READ_ONLY` (default), `PAPER`, `LIVE_TRADE`
    - Default URL: Binance testnet (safe by design)
    - `assert_mode(required_mode)` — raises `ConnectorNonRetryableError` if insufficient (non-retryable by design)
    - Extends `DataConnector` ABC: `connect()`, `close()`, `stream_ticks()`, `subscribe()`, `reconnect()`
    - H2/H4/H5 hardening: retries, circuit breaker, metrics integration
  - **Configuration** (`LiveConnectorConfig`):
    - `mode`: SafeMode (default: READ_ONLY)
    - `symbols`: List of symbols to subscribe
    - `ws_url`: WebSocket URL (default: testnet)
    - `timeout_config`, `retry_policy`, `circuit_breaker_config`
  - **Bounded-time testing:**
    - Injectable `clock` and `sleep_func` parameters
    - Tests complete in milliseconds with FakeClock/FakeSleep
  - **V0 scope (mock implementation):**
    - `stream_ticks()` yields nothing (placeholder for real WebSocket)
    - Contract verified, hardening wired, tests pass
    - Real WebSocket integration in v1
  - **Paper write-path (PAPER mode only):**
    - `place_order(symbol, side, price, quantity)` → `OrderResult` (instant fill v0)
    - `cancel_order(order_id)` → `OrderResult` (error if filled)
    - `replace_order(order_id, new_price, new_quantity)` → `OrderResult` (cancel+new)
    - Deterministic order IDs: `PAPER_{seq:08d}`
    - No network calls — pure in-memory simulation via `PaperExecutionAdapter`
  - **Unit tests:** `tests/unit/test_live_connector.py` (31 tests)
  - **Integration tests:** `tests/integration/test_live_connector_integration.py` (6 tests)
  - See ADR-029 (live connector v0), ADR-030 (paper write-path v0)
- **PaperExecutionAdapter** (`src/grinder/connectors/paper_execution.py`):
  - In-memory order execution backend for PAPER mode
  - **Features:**
    - Deterministic order ID generation (`{prefix}_{seq:08d}`)
    - V0 semantics: instant fill on place, cancel+new on replace
    - Injectable `clock` for deterministic timestamps
    - No persistence, no network calls
  - **Types:**
    - `OrderRequest`: Place/replace input (frozen dataclass)
    - `OrderResult`: Operation result snapshot (frozen dataclass)
    - `PaperOrder`: Mutable internal order record
    - `OrderType`: LIMIT, MARKET
    - `PaperOrderError`: Non-retryable error for order failures
  - **How to verify paper mode:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_paper_execution.py tests/integration/test_paper_write_path.py -v
    ```
  - **Unit tests:** `tests/unit/test_paper_execution.py` (21 tests)
  - **Integration tests:** `tests/integration/test_paper_write_path.py` (17 tests)
  - See ADR-030 for design decisions
- **BinanceExchangePort v0.1** (`src/grinder/execution/binance_port.py`):
  - Live exchange port implementing ExchangePort protocol for Binance Spot Testnet (LC-04)
  - **Safety by design:**
    - `SafeMode.READ_ONLY` (default): blocks ALL write operations → 0 risk
    - `SafeMode.LIVE_TRADE` required for real API calls (explicit opt-in)
    - Mainnet URLs (`api.binance.com`) forbidden in v0.1 (raises `ConnectorNonRetryableError`)
    - Default URL: `https://testnet.binance.vision` (testnet only)
  - **Injectable HTTP client:**
    - `HttpClient` protocol for HTTP operations
    - `NoopHttpClient` for mock transport testing (records calls but no real HTTP)
    - `dry_run=True` config: returns synthetic results WITHOUT calling http_client (0 calls)
    - Enables deterministic testing without external dependencies
  - **Symbol whitelist:**
    - `symbol_whitelist` config parameter
    - Blocks trades for unlisted symbols (empty = all allowed)
  - **Error mapping:**
    - 5xx → `ConnectorTransientError` (retryable)
    - 429 → `ConnectorTransientError` (rate limit)
    - 418 → `ConnectorNonRetryableError` (IP ban)
    - 4xx → `ConnectorNonRetryableError` (client error)
  - **H2/H3/H4 integration:**
    - Wrap with `IdempotentExchangePort` for idempotency + circuit breaker
    - Replace = cancel + place with shared idempotency key (safe under retries)
  - **Operations:**
    - `place_order()`: POST /api/v3/order
    - `cancel_order()`: DELETE /api/v3/order
    - `replace_order()`: cancel + place
    - `fetch_open_orders()`: GET /api/v3/openOrders
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_binance_port.py -v
    ```
  - **Unit tests:** `tests/unit/test_binance_port.py` (28 tests)
    - Dry-run tests prove NoopHttpClient makes 0 HTTP calls
    - SafeMode tests prove READ_ONLY blocks writes
    - Mainnet tests prove api.binance.com is rejected
    - Error mapping tests prove correct classification
    - Idempotency integration tests prove caching works
  - **Limitations (v0.1):**
    - Testnet only (mainnet forbidden)
    - HTTP REST only (no WebSocket streaming)
    - Spot only (no futures/margin)
    - Real AiohttpClient not implemented (only protocol)
  - See ADR-035 for design decisions
- **LiveEngineV0** (`src/grinder/live/engine.py`):
  - Live write-path wiring from PaperEngine to ExchangePort (LC-05)
  - **Arming model (two-layer safety):**
    - `armed=False` (default): blocks ALL writes before reaching port
    - `mode=SafeMode.LIVE_TRADE` required at port level
    - Both required for actual writes: `armed=True AND mode=LIVE_TRADE`
  - **Intent classification:**
    - PLACE/REPLACE → INCREASE_RISK (blocked in DRAWDOWN)
    - CANCEL → CANCEL (always allowed)
  - **Safety gate ordering:**
    1. Arming check
    2. Mode check
    3. Kill-switch check (blocks INCREASE_RISK, allows CANCEL)
    4. Symbol whitelist check
    5. DrawdownGuardV1.allow(intent)
    6. Execute via exchange_port
  - **Hardening chain:**
    - H3: IdempotentExchangePort for idempotency
    - H4: CircuitBreaker for fast-fail
    - H2: RetryPolicy for transient errors
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_live_engine.py -v
    ```
  - **Unit tests:** `tests/unit/test_live_engine.py` (16 tests)
    - Safety/arming tests prove armed=False blocks writes
    - Drawdown tests prove INCREASE_RISK blocked in DRAWDOWN
    - Idempotency tests prove duplicate→cached
    - Circuit breaker tests prove OPEN→reject
  - See ADR-036 for design decisions
- **LiveFeed v0** (`src/grinder/live/feed.py`):
  - Live read-path pipeline: WS → Snapshot → FeatureEngine → features (LC-06)
  - **Architecture:** DataConnector → Snapshot → FeatureEngine → LiveFeaturesUpdate
  - **Hard read-only constraint:**
    - `feed.py` MUST NOT import from `grinder.execution.*`
    - Enforced by `test_feed_py_has_no_execution_imports` (AST parsing)
    - Violation = CI failure
  - **BinanceWsConnector** (`src/grinder/connectors/binance_ws.py`):
    - Implements `DataConnector` ABC with `iter_snapshots()` async iterator
    - Parses bookTicker JSON → Snapshot objects
    - Idempotency via `last_seen_ts` tracking
    - Auto-reconnect with exponential backoff
    - Testable via `WsTransport` ABC injection (FakeWsTransport for tests)
  - **LiveFeed pipeline:**
    - Symbol filtering (optional)
    - FeatureEngine integration (BarBuilder → indicators)
    - Yields `LiveFeaturesUpdate` with computed features
    - Warmup detection (`is_warmed_up` when bars >= warmup_bars)
  - **Key types:**
    - `LiveFeaturesUpdate`: ts, symbol, features, bar_completed, is_warmed_up, latency_ms
    - `WsMessage`: Raw WebSocket message wrapper
    - `BookTickerData`: Parsed Binance bookTicker fields
    - `LiveFeedStats`: Ticks/bars/errors tracking
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_live_feed.py -v
    ```
  - **Unit tests:** `tests/unit/test_live_feed.py` (21 tests)
    - P0 hard-block tests prove 0 execution imports
    - FakeWsTransport tests prove testable WS behavior
    - Connector tests prove yields snapshots, skips duplicates
    - LiveFeed tests prove features computed correctly
    - Golden output tests prove determinism
  - **Fixtures:** `tests/fixtures/ws/bookticker_btcusdt.json`
  - See ADR-037 for design decisions
- **Testnet Smoke Test** (`scripts/smoke_live_testnet.py`):
  - E2E smoke test for Binance Testnet: place micro order → cancel (LC-07)
  - **Safe-by-construction guards:**
    - `--dry-run` by default (no real HTTP calls)
    - Requires `--confirm TESTNET` for real orders
    - Mainnet FORBIDDEN (blocked in BinanceExchangePort)
    - Requires `ARMED=1` + `ALLOW_TESTNET_TRADE=1` env vars
    - Kill-switch blocks PLACE/REPLACE, allows CANCEL
  - **Failure paths:**
    - Missing keys → clear exit 1 + message
    - Empty whitelist → blocks at port level
    - Kill-switch active → PLACE blocked (expected), CANCEL allowed
  - **How to verify:**
    ```bash
    # Dry-run (default)
    PYTHONPATH=src python -m scripts.smoke_live_testnet

    # Real testnet order
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_TESTNET_TRADE=1 \
        PYTHONPATH=src python -m scripts.smoke_live_testnet --confirm TESTNET
    ```
  - **Runbook:** `docs/runbooks/08_SMOKE_TEST_TESTNET.md`
- **DrawdownGuard v0** (`src/grinder/risk/drawdown.py`):
  - Tracks equity high-water mark (HWM)
  - Computes drawdown: `(HWM - equity) / HWM`
  - Configurable threshold (`max_drawdown_pct`, default 5%)
  - Latching behavior: once triggered, stays triggered until reset
  - **Equity definition:** `equity = initial_capital + total_realized_pnl + total_unrealized_pnl`
  - **HWM initialization:** First equity sample (starts at `initial_capital`)
  - See ADR-013 for design decisions
- **AutoSizer v1** (`src/grinder/sizing/auto_sizer.py`):
  - Risk-budget-based position sizing for grid policies (ASM-P2-01)
  - **Core formula:** `qty_per_level = (equity * dd_budget) / (n_levels * price * adverse_move)`
  - **Risk guarantee:** worst_case_loss <= equity * dd_budget
  - **Sizing modes:** UNIFORM (default), PYRAMID, INVERSE_PYRAMID
  - **Inputs:** equity, dd_budget (e.g., 0.20), adverse_move (e.g., 0.25), grid_shape, price
  - **Output:** SizeSchedule with qty_per_level[], risk_utilization, worst_case_loss
  - **Integration:** AdaptiveGridPolicy (opt-in via `auto_sizing_enabled=True`)
  - **Backward compat:** When disabled, uses legacy uniform `size_per_level`
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_auto_sizer.py tests/unit/test_adaptive_policy.py::TestAutoSizingIntegration -v
    ```
  - **Unit tests:** `tests/unit/test_auto_sizer.py` (36 tests)
  - **Integration tests:** `tests/unit/test_adaptive_policy.py::TestAutoSizingIntegration` (5 tests)
  - See ADR-031 for design decisions
- **DdAllocator v1** (`src/grinder/sizing/dd_allocator.py`):
  - Portfolio-to-symbol DD budget distribution (ASM-P2-02)
  - **Inputs:** equity, portfolio_dd_budget, candidates[] (symbol, tier, weight, enabled)
  - **Output:** AllocationResult with per-symbol dd_budget fractions and residual
  - **Algorithm:** `risk_weight = user_weight / tier_factor`, normalize, distribute, ROUND_DOWN
  - **Tier factors:** LOW=1.0, MED=1.5, HIGH=2.0 (higher = less budget)
  - **Invariants (all tested):**
    1. Non-negativity: all budgets >= 0
    2. Conservation: sum(budgets) + residual == portfolio_budget
    3. Determinism: same inputs → same outputs
    4. Monotonicity: larger budget → no decrease
    5. Tier ordering: HIGH <= MED <= LOW (at equal weights)
  - **Residual policy:** ROUND_DOWN residual stays in cash reserve
  - **Integration:** Output feeds into AdaptiveGridConfig.dd_budget → AutoSizer
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_dd_allocator.py tests/unit/test_adaptive_policy.py::TestDdAllocatorIntegration -v
    ```
  - **Unit tests:** `tests/unit/test_dd_allocator.py` (28 tests)
  - **Integration tests:** `tests/unit/test_adaptive_policy.py::TestDdAllocatorIntegration` (3 tests)
  - See ADR-032 for design decisions
- **DrawdownGuardV1** (`src/grinder/risk/drawdown_guard_v1.py`):
  - Intent-based DD guard for portfolio and per-symbol risk enforcement (ASM-P2-03)
  - **GuardState:** `NORMAL` (all intents allowed) | `DRAWDOWN` (reduce-only)
  - **OrderIntent:** `INCREASE_RISK` | `REDUCE_RISK` | `CANCEL`
  - **Transition triggers:**
    - Portfolio DD >= portfolio_dd_limit, OR
    - Symbol loss >= symbol_dd_budget
  - **No auto-recovery:** Once in DRAWDOWN, stays until explicit `reset()` (deterministic)
  - **Allow decision table:**
    | State | Intent | Allowed | Reason |
    |-------|--------|---------|--------|
    | NORMAL | * | Yes | NORMAL_STATE |
    | DRAWDOWN | INCREASE_RISK | No | DD_PORTFOLIO_BREACH / DD_SYMBOL_BREACH |
    | DRAWDOWN | REDUCE_RISK | Yes | REDUCE_RISK_ALLOWED |
    | DRAWDOWN | CANCEL | Yes | CANCEL_ALWAYS_ALLOWED |
  - **PaperEngine wiring:** `src/grinder/paper/engine.py` Step 3.5 (lines 717-767)
    - Enabled via `dd_guard_v1_enabled=True` in constructor
    - Wiring point: after gating check, before execution
    - Blocks INCREASE_RISK orders when in DRAWDOWN state
  - **Reduce-only path (P2-04b):**
    - `PaperEngine.flatten_position(symbol, price, ts)` — closes entire position
    - Checks `guard.allow(REDUCE_RISK, symbol)` before executing
    - Deterministic: same inputs → same fill output
    - Allows emergency exit when DRAWDOWN is triggered
  - **Reset hook (P2-04c):**
    - `PaperEngine.reset_dd_guard_v1()` — returns guard to NORMAL state
    - Use case: new session/day start
    - Also called by `PaperEngine.reset()` (general reset)
  - **How to verify:**
    ```bash
    PYTHONPATH=src pytest tests/unit/test_drawdown_guard_v1.py -v
    ```
  - **Unit tests:** `tests/unit/test_drawdown_guard_v1.py` (55 tests: 43 guard/wiring + 6 reduce-only + 6 reset)
  - See ADR-033 for design decisions
- **KillSwitch v0** (`src/grinder/risk/kill_switch.py`):
  - Simple emergency halt latch for trading
  - **Idempotent:** triggering twice is a no-op
  - **Reasons:** `DRAWDOWN_LIMIT`, `MANUAL`, `ERROR`
  - **Reset semantics:** does NOT auto-reset; requires explicit `reset()` call
  - **PaperEngine integration:**
    - When triggered, blocks all trading with `KILL_SWITCH_ACTIVE` gating reason
    - Does NOT auto-liquidate positions (configurable in future)
    - Surfaces state in `PaperResult` and `PaperOutput`
  - **Unit tests:** `tests/unit/test_risk.py` (25 tests)
  - **Integration tests:** `tests/integration/test_kill_switch_integration.py` (9 tests)
  - See ADR-013 for design decisions
- **Soak Gate v0** (`scripts/run_soak_fixtures.py`, `.github/workflows/soak_gate.yml`):
  - Fixture-based soak runner for CI release gate
  - **Metrics collected:**
    - Processing latency (p50, p99)
    - Memory usage (RSS max)
    - Fill rate
    - Error counts
    - Digest stability (determinism check)
  - **Threshold policy:** `monitoring/soak_thresholds.yml`
    - Baseline mode: strict thresholds for normal operation
    - Overload mode: relaxed thresholds for stress testing
  - **CI integration:**
    - `soak_gate.yml`: runs on PRs touching src/scripts/tests/monitoring
    - `nightly_soak.yml`: runs daily with synthetic soak (existing)
  - **Commands:**
    - `python -m scripts.run_soak_fixtures --output report.json` — run soak test
    - `python scripts/check_soak_gate.py --report report.json --thresholds monitoring/soak_thresholds.yml --mode baseline` — PR gate (deterministic only)
    - `python scripts/check_soak_thresholds.py --baseline report.json --overload report.json --thresholds monitoring/soak_thresholds.yml` — nightly gate (full)
  - **Unit tests:** `tests/unit/test_soak_thresholds.py`
  - **Test fixtures:** Uses registered fixtures including `sample_day_drawdown` for kill-switch
- **Operations v0** (`docs/runbooks/`, `docs/HOW_TO_OPERATE.md`):
  - Runbooks for common operational tasks:
    - [Startup/Shutdown](runbooks/01_STARTUP_SHUTDOWN.md): docker compose up/down
    - [Health Triage](runbooks/02_HEALTH_TRIAGE.md): quick diagnostics via /healthz
    - [Metrics & Dashboards](runbooks/03_METRICS_DASHBOARDS.md): Prometheus + Grafana
    - [Kill-Switch](runbooks/04_KILL_SWITCH.md): detection, diagnosis, recovery
    - [Soak Gate](runbooks/05_SOAK_GATE.md): running soak tests
    - [Alert Response](runbooks/06_ALERT_RESPONSE.md): responding to Prometheus alerts
    - [HA Operations](runbooks/07_HA_OPERATIONS.md): high availability deployment and failover
  - Operator's guide: [HOW_TO_OPERATE.md](HOW_TO_OPERATE.md)
  - **Quick reference:**
    - Health: `curl -fsS http://localhost:9090/healthz`
    - Metrics: `curl -fsS http://localhost:9090/metrics`
    - Start stack: `docker compose -f docker-compose.observability.yml up --build -d`
    - Stop stack: `docker compose -f docker-compose.observability.yml down -v`
  - **Limitations:** No Kubernetes, no automated runbook execution
- **HA v0** (`src/grinder/ha/`, `docker-compose.ha.yml`):
  - Single-host redundancy with Redis lease-lock coordination
  - **Components:**
    - `HARole` enum: `ACTIVE`, `STANDBY`, `UNKNOWN`
    - `HAState`: thread-safe state container for current role
    - `LeaderElector`: Redis-based lease lock manager with TTL-based coordination
  - **Leader election:**
    - Lock TTL: 10 seconds (configurable via `GRINDER_HA_LOCK_TTL_MS`)
    - Renewal interval: 3 seconds (configurable via `GRINDER_HA_RENEW_INTERVAL_MS`)
    - Lock key: `grinder:leader:lock`
  - **Fail-safe semantics (critical for single-active safety):**
    - Lock renewal failure → immediately become STANDBY
    - Redis unavailable → all instances become STANDBY
    - STANDBY/UNKNOWN → `/readyz` returns 503 (not ready for traffic)
    - Only ACTIVE → `/readyz` returns 200 (ready)
    - **Unit tests:** `tests/unit/test_ha.py::TestFailSafeSemantics` validates these guarantees
  - **Observability:**
    - `/readyz` endpoint: 200 for ACTIVE, 503 for STANDBY/UNKNOWN
    - `/healthz` endpoint: 200 always (liveness, not readiness)
    - `grinder_ha_role{role}` metric: gauge with all roles (current=1, others=0)
  - **Deployment:** `docker-compose.ha.yml` with Redis 7.2 + 2 grinder instances
  - **Environment variables:**
    - `GRINDER_REDIS_URL`: Redis connection URL (default: redis://localhost:6379/0)
    - `GRINDER_HA_ENABLED`: Enable HA mode (default: false)
    - `GRINDER_HA_LOCK_TTL_MS`: Lock TTL in milliseconds (default: 10000)
    - `GRINDER_HA_RENEW_INTERVAL_MS`: Lock renewal interval in milliseconds (default: 3000)
  - **Commands:**
    - Start HA stack: `docker compose -f docker-compose.ha.yml up --build -d`
    - Failover test: `docker stop grinder_live_1` → grinder_live_2 becomes ACTIVE within ~10s
  - **Contract tests:**
    - `tests/unit/test_live_contracts.py`: /readyz response structure
    - `tests/unit/test_ha.py`: HA role, state management, fail-safe semantics (17 tests)
  - See ADR-015 for design decisions
  - **Limitations:** Single-host only (Redis is SPOF), no protection against host/VM failure (v1 scope)
- **HA Runbooks + Release Procedure v1** (PR-M3-012):
  - **HA Operations runbook** (`docs/runbooks/07_HA_OPERATIONS.md`):
    - Rolling restart procedure (zero-downtime HTTP)
    - One node down triage and recovery
    - Redis down/flapping troubleshooting
    - Failover testing procedures
  - **Release Checklist v1** (`docs/HOW_TO_OPERATE.md#deploying-a-release`):
    - Pre-flight: CI gates (checks, soak-gate, docker-smoke, ha-smoke, determinism)
    - Deployment steps: single-instance and HA rolling restart
    - Post-deployment verification checklist
    - Rollback plan with exact commands
  - **Navigation updated:** `docs/DOCS_INDEX.md`, `docs/runbooks/README.md`

## Partially implemented
- Структура пакета `src/grinder/*` (core, protocols/interfaces) — каркас.
- Документация в `docs/*` — SSOT по архитектуре/спекам (но должна совпадать с реализацией).

## Known gaps / mismatches
- Нет реальной торговой логики — только skeleton/stubs.
- **Backtest in Docker fails:** Running `scripts/run_backtest.py` inside a Docker container fails with `ModuleNotFoundError: No module named 'scripts'`. Fixture determinism checks pass; CI passes because it runs in a properly configured Python environment.
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
- Live Connector v1 (real WebSocket integration for LiveConnectorV0).

## Smart Grid Spec Version

| Spec | Location | Status | Proof Anchor |
|------|----------|--------|--------------|
| v1.0 | `docs/smart_grid/SPEC_V1_0.md` | ✅ Implemented | `sample_day`, `sample_day_allowed` fixtures; ADR-019..021 |
| v1.1 | `docs/smart_grid/SPEC_V1_1.md` | ✅ Implemented | FeatureEngine in `sample_day_adaptive`; ADR-019 |
| v1.2 | `docs/smart_grid/SPEC_V1_2.md` | ✅ Implemented | `sample_day_adaptive` digest `1b8af993a8435ee6`; ADR-022 |
| v1.3 | `docs/smart_grid/SPEC_V1_3.md` | ✅ Implemented | `sample_day_topk_v1` digest `63d981b60a8e9b3a`; ADR-023 |
| v2.0 | `docs/smart_grid/SPEC_V2_0.md` | 🔜 Planned | — |
| v3.0 | `docs/smart_grid/SPEC_V3_0.md` | 🔜 Planned | — |

**Verification:** `python -m scripts.verify_determinism_suite` (8/8 fixtures PASS)
**Current target:** `docs/smart_grid/SPEC_V1_3.md`
**Roadmap:** `docs/smart_grid/ROADMAP.md`

---

## Planned (spec exists, not implemented)

### Adaptive Smart Grid v2.0+ (`docs/smart_grid/SPEC_V2_0.md`)
Comprehensive adaptive grid system design:
- **Regime-driven behavior:** RANGE / TREND / VOL_SHOCK / THIN_BOOK / TOXIC / PAUSED / EMERGENCY
- **Auto-sizing:** dynamic step, width, levels, and size schedule from market features
- **L1/L2-aware:** microstructure features from L1 (spread, imbalance) and optional L2 (depth, impact)
- **Risk budgeting:** DD budgets, inventory caps, leverage caps, auto-allocation
- **Top-K 3–5:** symbol selection for tradable chop + safe liquidity
- **Deterministic:** fixture-based testing with stable digests
- **Cross-reference:** See `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` for meta-controller contracts (regime, step, reset)
- **Partial implementation status:**
  - ✅ **Feature Engine v1 (ASM-P1-01):** Mid-bar OHLC + ATR/NATR + L1 microstructure (see ADR-019)
  - ✅ **PaperEngine integration (ASM-P1-02):** Integrated, features NOT in digest (backward compat)
  - ✅ **Policy receives features (ASM-P1-03):** Plumbing complete — features passed to policy when enabled, StaticGridPolicy ignores (backward compat), digests unchanged (see ADR-020)
  - ✅ **Regime classifier (ASM-P1-04):** Deterministic precedence-based classifier — EMERGENCY > TOXIC > THIN_BOOK > VOL_SHOCK > TREND > RANGE (see ADR-021)
  - ✅ **AdaptiveGridPolicy v1 (ASM-P1-05):** Dynamic grid sizing from NATR + regime (see ADR-022)
    - **Opt-in:** `adaptive_policy_enabled=False` default (backward compat with existing digests)
    - **L1-only:** Uses natr_bps, spread_bps, thin_l1, range_score from FeatureEngine
    - **Adapts:** step_bps (volatility-scaled), width_bps (X_stress model), levels (ceil(width/step))
    - **Formulas:** step=max(5, 0.3*NATR*regime_mult), width=clamp(2.0*NATR*sqrt(H/TF), 20, 500), levels=clamp(ceil(width/step), 2, 20)
    - **Regime multipliers:** RANGE=1.0, VOL_SHOCK=1.5, THIN_BOOK=2.0, TREND asymmetric (1.3× on against-trend side)
    - **Units:** All thresholds/multipliers as integer bps (×100 scale) for determinism
    - **NOT included:** DD allocator, L2 features (auto-sizing now available via ASM-P2-01)
    - **Fixture:** `sample_day_adaptive` — paper digest `1b8af993a8435ee6`
  - ✅ **Top-K v1 (ASM-P1-06):** Feature-based symbol selection (see ADR-023)
    - **Opt-in:** `topk_v1_enabled=False` default (backward compat with existing digests)
    - **Requires:** `feature_engine_enabled=True` (needs FeatureEngine for range_score, spread_bps, thin_l1, net_return_bps)
    - **Scoring:** `score = range + liquidity - toxicity_penalty - trend_penalty`
    - **Hard gates:** TOXICITY_BLOCKED, SPREAD_TOO_WIDE, THIN_BOOK, WARMUP_INSUFFICIENT
    - **Tie-breaking:** Deterministic by `(-score, symbol)` for stable ordering
    - **Config:** `TopKConfigV1(k=3, spread_max_bps=100, thin_l1_min=1.0, warmup_min=10)`
    - **Output:** `topk_v1_selected_symbols`, `topk_v1_scores`, `topk_v1_gate_excluded`
    - **Fixture:** `sample_day_topk_v1` — 6 symbols, paper digest `63d981b60a8e9b3a`
    - **NOT included:** real-time re-selection (selects once after warmup), adaptive scoring weights
