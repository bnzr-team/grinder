# DECISIONS (ADR Log)

Этот файл фиксирует **почему** были приняты важные решения. Цель — избежать “памяти на словах” и дрейфа архитектуры.

## Формат записи

- **ID:** ADR-XXX
- **Date:** YYYY-MM-DD
- **Status:** proposed | accepted | superseded
- **Context:** почему возник вопрос
- **Decision:** что выбрали
- **Consequences:** что это означает для кода/процесса
- **Alternatives:** что рассматривали

---

## ADR-001 — Детерминизм replay обязателен
- **Date:** 2026-01-30
- **Status:** accepted
- **Context:** изменения в policy/risk/execution легко ломают воспроизводимость бэктестов.
- **Decision:** любой PR, затрагивающий replay/fixtures/policy/risk/execution, обязан проходить `scripts.verify_replay_determinism` и прикладывать proof.
- **Consequences:** флак-тесты запрещены; случайность только с фиксированными seed.
- **Alternatives:** “доверять на слово” / “проверять вручную”.

## ADR-002 — README/pyproject не опережают реализацию
- **Date:** 2026-01-30
- **Status:** accepted
- **Context:** несостыковки документации и кода ломают онбординг и доверие.
- **Decision:** любые команды/CLI/пути в README и entrypoints в `pyproject.toml` должны быть реально работоспособны. Всё остальное помечать как Planned.
- **Consequences:** прежде чем добавить новый entrypoint — добавить модуль+тест.
- **Alternatives:** "дописать позже".

## ADR-003 — Domain contracts as SSOT
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** M1 vertical slice требует единых контрактов данных для pipeline (Snapshot → Policy → Decision → Execution). Без SSOT контрактов легко получить рассинхрон между компонентами.
- **Decision:**
  - Контракты живут в `src/grinder/contracts.py`
  - Все контракты — frozen dataclasses (immutable)
  - Все контракты имеют `to_dict()`/`from_dict()` для JSON-сериализации
  - JSON-сериализация детерминистична (`sort_keys=True`)
  - Breaking changes требуют: (1) обновления fixtures, (2) bump версии, (3) обновления STATE.md
- **Consequences:**
  - Prefilter/Policy/Execution импортируют контракты из `contracts.py`
  - Тесты проверяют roundtrip сериализации
  - Golden fixtures в `tests/fixtures/contracts/` служат regression-тестами
- **Alternatives:** "протоколы/интерфейсы без конкретных типов" — отвергнуто, т.к. ломает детерминизм и type safety.

## ADR-004 — Adaptive Controller is rule-based MVP with explicit resets
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** grid parameters and mode must adapt to regime shifts; silent drift and implicit resets cause churn and non-determinism.
- **Decision:** introduce an Adaptive Controller contract that selects market regime, computes adaptive step, and emits explicit auto-reset actions with reason-codes (SSOT: `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md`).
- **Consequences:** `GridPlan` contract expands (regime/width/reset/reason_codes); observability must expose regime/step/reset; implementation PRs must include baseline backtests.
- **Alternatives:** black-box ML controller; implicit resets hidden in execution; keeping static spacing.

## ADR-005 — Unicode policy for docs
- **Date:** 2026-01-31 (updated 2026-02-01)
- **Status:** accepted
- **Context:** GitHub flags "hidden or bidirectional Unicode" in PRs (CVE-2021-42574 Trojan Source protection). Need clear policy on allowed vs dangerous chars, and documented mitigation for the warning.
- **Decision:**
  - **Forbidden (security risk):** bidi controls (U+202A-E, U+2066-9), zero-width (U+200B-D, U+FEFF), soft hyphen (U+00AD), category `Cf` (Format) except allowed below
  - **Allowed (non-security):** box-drawing (U+2500-257F) for diagrams, Cyrillic for Russian text in docs, em-dash (U+2014) for typography
  - Use `scripts/check_unicode.py` to verify before merge (scans for forbidden bidi/zero-width chars)
  - **GitHub warning mitigation:** The repo-wide scan (`scripts/check_unicode.py`) is the authoritative security gate, NOT the GitHub UI warning. GitHub may show "hidden or bidirectional Unicode" warning due to:
    - Em-dashes (U+2014) in documentation — allowed, not a security risk
    - Cyrillic text in `docs/STATE.md` — allowed, Russian documentation
    - Box-drawing characters — allowed for diagrams
  - PRs are mergeable when: (1) `scripts/check_unicode.py` passes, (2) all CI checks green
  - **Enforced in CI:** `.github/workflows/ci.yml` → step "Unicode security scan"
- **Consequences:** PRs must pass Unicode scan; GitHub warning is informational, not blocking, when scan passes.
- **Alternatives:** replace all non-ASCII with ASCII — rejected for readability and i18n support.

## ADR-006 — Fixture event format: SNAPSHOT replaces BOOK_TICKER
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** M1 end-to-end replay requires events that match the `Snapshot` domain contract (`src/grinder/contracts.py`). The old `BOOK_TICKER` format used `bid`/`ask` (floats) and lacked `last_price`/`last_qty` fields required by `Snapshot.from_dict()`. ReplayEngine needs typed Snapshot objects to compute `mid_price` and `spread_bps` for prefilter and policy.
- **Decision:**
  - Fixture events use `type: "SNAPSHOT"` with fields matching `Snapshot` contract:
    - `ts`, `symbol`, `bid_price`, `ask_price`, `bid_qty`, `ask_qty`, `last_price`, `last_qty`
  - All price/quantity fields are string-encoded Decimals (not floats) for determinism
  - Old `BOOK_TICKER` format is **not supported** by ReplayEngine
  - Expected digest for `tests/fixtures/sample_day` is now `453ebd0f655e4920` (was `9397b7200c09a7d2` with old format)
- **Consequences:**
  - Existing fixtures must be migrated to SNAPSHOT format
  - Digest values in `config.json` must be updated after migration
  - ReplayEngine only processes `type: "SNAPSHOT"` events (other types ignored)
- **Alternatives:**
  - Support both formats with adapter — rejected for complexity and non-determinism risk
  - Keep BOOK_TICKER and adapt Snapshot contract — rejected because BOOK_TICKER lacks required fields

## ADR-007 — Gating metrics contract
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Gating decisions (allow/block) need observability for production monitoring and debugging. Metric names and label keys become implicit contracts once Grafana dashboards and alerts depend on them.
- **Decision:**
  - Stable enums for gate identifiers (`GateName`) and block reasons (`GateReason`)
  - Metric names are constants: `grinder_gating_allowed_total`, `grinder_gating_blocked_total`
  - Label keys are constants: `gate`, `reason`
  - Contract tests in `tests/unit/test_gating_contracts.py` fail if any of these change
  - Breaking changes require: (1) update contract tests, (2) add ADR entry, (3) update dashboards
- **Consequences:**
  - Adding new gates/reasons is safe (append-only)
  - Renaming or removing existing values is a breaking change
  - Dashboards can rely on stable label values
- **Alternatives:**
  - Free-text reasons — rejected for label cardinality explosion
  - No contract tests — rejected because silent breaks are worse than loud failures

## ADR-008 — Paper trading output schema v1
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Paper trading needs stable output schema for: (1) replay determinism verification, (2) downstream analysis tools, (3) dashboard integration. Adding fields without versioning risks silent breaking changes.
- **Decision:**
  - Introduce `SCHEMA_VERSION = "v1"` constant in `src/grinder/paper/engine.py`
  - `PaperResult` includes `schema_version` field in serialized output
  - **PaperOutput v1 contract** (required keys): `ts`, `symbol`, `prefilter_result`, `gating_result`, `plan`, `actions`, `events`, `blocked_by_gating`, `fills`, `pnl_snapshot`
  - **PaperResult v1 contract** (required keys): `schema_version`, `fixture_path`, `outputs`, `digest`, `events_processed`, `events_gated`, `orders_placed`, `orders_blocked`, `total_fills`, `final_positions`, `total_realized_pnl`, `total_unrealized_pnl`, `errors`
  - All monetary values serialized as strings (Decimal → str)
  - Contract tests in `tests/unit/test_paper_contracts.py` fail on breaking changes
  - Canonical digests locked: `sample_day` = `66b29a4e92192f8f`, `sample_day_allowed` = `ec223bce78d7926f`, `sample_day_toxic` = `66d57776b7be4797`
- **Consequences:**
  - Adding new fields is safe (append-only)
  - Removing/renaming existing fields is breaking change requiring version bump
  - Downstream tools can rely on field presence
  - Digests change when output structure changes (intentional)
- **Alternatives:**
  - No versioning — rejected because silent breaks are worse
  - Semantic versioning — deferred, simple "v1" sufficient for now

## ADR-009 — ToxicityGate v0 with per-symbol price tracking
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Markets can become "toxic" during periods of stress (flash crashes, spread spikes). Trading during toxic conditions leads to adverse selection and losses. Need a gate to block orders when market conditions are dangerous.
- **Decision:**
  - Introduce `ToxicityGate` class in `src/grinder/gating/toxicity_gate.py`
  - **Spread spike detection**: Block when `spread_bps > max_spread_bps` (default 50 bps)
  - **Price impact detection**: Block when price moves > `max_price_impact_bps` (default 500 bps = 5%) within `lookback_window_ms` (default 5000ms)
  - Threshold set high (500 bps) to avoid triggering on normal volatility (existing fixtures have up to 300 bps moves)
  - Price history is tracked **per-symbol** (not global) to avoid false positives across different assets
  - ToxicityGate is checked **first** in gating pipeline (fail-fast on market conditions)
  - Extend `GateName` with `TOXICITY_GATE`, `GateReason` with `SPREAD_SPIKE` and `PRICE_IMPACT_HIGH`
  - New fixture `sample_day_toxic` tests price impact blocking (600 bps move)
  - Toxicity details NOT included in allow result to preserve backward compatibility with existing digests
- **Consequences:**
  - Paper engine gating order: toxicity → rate limit → risk gate
  - Existing fixture digests UNCHANGED: `sample_day` = `66b29a4e92192f8f`, `sample_day_allowed` = `ec223bce78d7926f`
  - New fixture: `sample_day_toxic` = `66d57776b7be4797`
  - `/metrics` now includes `toxicity_gate` label values
- **Alternatives:**
  - Global price history across symbols — rejected because comparing BTC to ETH prices causes false positives
  - ML-based toxicity detection — deferred for v1, rule-based is deterministic and sufficient for v0
  - Lower threshold (100 bps) — rejected because it would trigger on existing "happy path" fixtures
  - Include toxicity details in allow result — rejected to preserve backward compatibility with existing digests

## ADR-010 — Top-K Prefilter v0 with volatility scoring
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Need to select a candidate set of symbols (Top-K) from a multisymbol stream for grid trading. Selection must be deterministic to ensure replay reproducibility.
- **Decision:**
  - Introduce `TopKSelector` class in `src/grinder/prefilter/topk.py`
  - **Scoring method:** Volatility proxy — sum of absolute mid-price returns in integer basis points over a window
    - `score_bps = int(Σ |(mid_t - mid_{t-1}) / mid_{t-1}| * 10000)` (quantized to int for determinism)
  - **Tie-breakers (deterministic):**
    1. Higher score first
    2. Lexicographic symbol ascending
  - **Parameters:**
    - `K` = 3 (default, configurable)
    - `window_size` = 10 events per symbol (default, configurable)
  - Top-K runs **before** policy/execution/gating in paper trading pipeline
  - Top-K results included in `PaperResult` (`topk_selected_symbols`, `topk_k`, `topk_scores`) and `FixtureResult` in backtest report
  - **NOT** included in paper digest computation to preserve backward compatibility with existing canonicals
  - New fixture `sample_day_multisymbol` with 5 symbols tests Top-K filtering
- **Consequences:**
  - Existing fixture digests UNCHANGED (all have ≤3 symbols, K=3 selects all)
    - `sample_day` = `66b29a4e92192f8f`
    - `sample_day_allowed` = `ec223bce78d7926f`
    - `sample_day_toxic` = `66d57776b7be4797`
  - New fixture: `sample_day_multisymbol` = `7c4f4b07ec7b391f`
  - Backtest report now includes `topk_selected_symbols` and `topk_k` per fixture
  - Report digest changes due to new fixture: `cd622b8e55764b5b`
- **Alternatives:**
  - Activity proxy (event count) — rejected for being less "market-y"
  - ML-based scoring — deferred for v1, rule-based is deterministic and sufficient for v0
  - Include Top-K in digest — rejected to preserve backward compatibility

## ADR-011 — Adaptive Controller v0 with rule-based parameter adjustment
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** Grid spacing must adapt to changing market conditions. Static spacing leads to suboptimal performance: too tight in volatile markets (fills too fast, adverse selection), too wide in calm markets (misses opportunities). Need a controller that adjusts policy parameters based on recent market conditions.
- **Decision:**
  - Introduce `AdaptiveController` class in `src/grinder/controller/adaptive.py`
  - **Controller modes:**
    - `BASE` — Normal operation, no adjustment (spacing_multiplier = 1.0)
    - `WIDEN` — High volatility, widen grid (spacing_multiplier = 1.5)
    - `TIGHTEN` — Low volatility, tighten grid (spacing_multiplier = 0.8)
    - `PAUSE` — Wide spread, no new orders
  - **Decision reasons:**
    - `NORMAL` — Metrics within normal thresholds
    - `HIGH_VOL` — Volatility above threshold (> 300 bps)
    - `LOW_VOL` — Volatility below threshold (< 50 bps)
    - `WIDE_SPREAD` — Spread above threshold (> 50 bps)
  - **Priority order:** PAUSE > WIDEN > TIGHTEN > BASE
  - **Window-based metrics:**
    - `vol_bps` = sum of absolute mid-price returns in integer bps over window
    - `spread_bps_max` = maximum spread observed in window
    - Window size default = 10 events per symbol
  - **Determinism:** All metrics use integer basis points (no floats)
  - **Opt-in:** Controller disabled by default (`controller_enabled=False`) to preserve backward compatibility
  - New fixture `sample_day_controller` tests all three volatility modes
- **Consequences:**
  - Controller is wired into paper engine after prefilter/Top-K, before policy evaluation
  - When enabled, controller applies spacing_multiplier to base spacing
  - Controller decisions recorded in `PaperResult.controller_decisions` (NOT in digest)
  - Existing fixture digests UNCHANGED (controller disabled by default):
    - `sample_day` = `66b29a4e92192f8f`
    - `sample_day_allowed` = `ec223bce78d7926f`
    - `sample_day_toxic` = `66d57776b7be4797`
    - `sample_day_multisymbol` = `7c4f4b07ec7b391f`
  - New fixture: `sample_day_controller` = `f3a0a321c39cc411`
- **Alternatives:**
  - EMA-based adaptive step (as per spec 16) — deferred for v1, rule-based is simpler and sufficient for v0
  - ML-based regime detection — rejected for determinism concerns
  - Controller always-on — rejected to preserve backward compatibility with existing digests

## ADR-012 — DataConnector abstract base class with narrow contract
- **Date:** 2026-02-01
- **Status:** accepted
- **Context:** Need a data connector interface for ingesting market data from various sources (live WebSocket, mock fixtures, replay files). The connector must support production hardening (timeouts, retries, idempotency) while remaining simple for testing.
- **Decision:**
  - Introduce `DataConnector` abstract base class in `src/grinder/connectors/data_connector.py`
  - **Narrow contract** (only essential methods):
    - `connect()` — establish connection
    - `close()` — release resources
    - `iter_snapshots()` — async iterator yielding `Snapshot`
    - `reconnect()` — resume from last position
  - **State machine:** DISCONNECTED → CONNECTING → CONNECTED → (RECONNECTING) → CLOSED
  - **Hardening configurations:**
    - `RetryConfig`: exponential backoff with cap (base_delay_ms, backoff_multiplier, max_delay_ms)
    - `TimeoutConfig`: connection and read timeouts
  - **Idempotency:** `last_seen_ts` property for duplicate detection; snapshots with ts ≤ last_seen_ts are skipped
  - **Assumption:** Timestamps in a single stream are monotonically increasing (per-stream, not per-symbol). Multi-symbol streams share one `last_seen_ts` guard. This is valid for fixture-based testing; live connectors may need per-symbol cursors if exchange delivers out-of-order across symbols.
  - **First implementation:** `BinanceWsMockConnector` reads from fixture files for testing
- **Consequences:**
  - New connectors must implement `DataConnector` interface
  - Idempotency is connector's responsibility (via `last_seen_ts` guard)
  - Mock connector enables deterministic testing without network
  - Retry/timeout configs are present but actual retry logic is implementation-specific
- **Alternatives:**
  - Callback-based interface — rejected for complexity and testing difficulty
  - Pull-based (get_next_snapshot) — rejected because async iterator is more Pythonic
  - Global connector registry — rejected as premature abstraction

## ADR-013 — DrawdownGuard and KillSwitch for equity protection
- **Date:** 2026-02-01
- **Status:** accepted
- **Context:** Need risk controls to halt trading when equity drawdown exceeds threshold. Essential for protecting capital in adverse market conditions. Must be deterministic for replay testing.
- **Decision:**
  - **Equity definition:**
    - `equity = initial_capital + total_realized_pnl + total_unrealized_pnl`
    - Sampled per snapshot (after fills are applied)
    - `initial_capital` is configurable parameter (default 10000 USD)
  - **DrawdownGuard** (`src/grinder/risk/drawdown.py`):
    - Tracks high-water mark (HWM), initialized to `initial_capital`
    - Drawdown computed as: `(HWM - equity) / HWM * 100`
    - Configurable threshold (`max_drawdown_pct`, default 5%)
    - **Latching:** once triggered, stays triggered until explicit reset
  - **KillSwitch** (`src/grinder/risk/kill_switch.py`):
    - Simple latch: once triggered, stays triggered
    - **Idempotent:** triggering twice is a no-op (returns existing state)
    - **Reasons:** `DRAWDOWN_LIMIT`, `MANUAL`, `ERROR`
    - **Reset semantics:** does NOT auto-reset within a run; requires new engine run or explicit `reset()` call
  - **PaperEngine integration:**
    - Optional: `kill_switch_enabled` parameter (default False for backward compatibility)
    - When kill-switch is triggered, blocks all trading with `KILL_SWITCH_ACTIVE` gating reason
    - **No auto-liquidation:** positions are NOT force-closed when kill-switch trips (default behavior)
    - State exposed in `PaperOutput` (per-snapshot) and `PaperResult` (final)
  - **New gating reasons added:**
    - `KILL_SWITCH_ACTIVE` — trading blocked because kill-switch is triggered
    - `DRAWDOWN_LIMIT_EXCEEDED` — for future use (currently triggers kill-switch instead)
- **Consequences:**
  - Trading halts deterministically when drawdown exceeds threshold
  - Trigger point is deterministic (exact snapshot where threshold crossed)
  - Backward compatible: disabled by default
  - No automatic position management (manual intervention required after trip)
- **Alternatives:**
  - Auto-liquidation on trip — rejected for simplicity; can be added as opt-in feature later
  - Soft warning before hard stop — rejected; keep v0 simple
  - Per-symbol drawdown tracking — rejected; track total portfolio equity for v0

## ADR-014 — Soak Gate as CI release gate
- **Date:** 2026-02-01
- **Status:** accepted
- **Context:** Need CI gate to catch performance regressions and determinism issues before merge. Synthetic soak (nightly) exists but doesn't use real PaperEngine execution. Need fixture-based soak that validates actual behavior.
- **Decision:**
  - **Soak runner** (`scripts/run_soak_fixtures.py`):
    - Runs PaperEngine on all registered fixtures multiple times (default 3 runs)
    - Collects metrics: latency (p50, p99), RSS memory, fill_rate, errors, digest stability
    - Outputs JSON report compatible with `check_soak_thresholds.py`
  - **Gating vs informational metrics:**
    - **Gating (causes failure):** `errors_total`, `all_digests_stable`, `fill_rate` range
    - **Informational (logged but not gated):** `decision_latency_p99_ms`, `order_latency_p99_ms`, `rss_mb_max`, `event_queue_depth_max`
    - Latency/memory are NOT gated because CI runners have variable performance
  - **Determinism check:**
    - Each fixture runs N times, all N digests must match
    - `all_digests_stable: true` required for pass
    - Any digest mismatch = hard failure
  - **Why 3 runs per fixture:**
    - Balances coverage vs CI time
    - Enough to detect non-determinism
    - Not so many that CI becomes slow
  - **Fill rate semantics:**
    - Computed as: `total_fills / total_orders_placed`
    - Value between 0 and 1
    - If no orders placed, fill_rate = 1.0 (nothing to fill)
    - Threshold: min 0.4, max 1.0 (baseline)
  - **Deterministic gate script** (`scripts/check_soak_gate.py`):
    - Reads thresholds from `monitoring/soak_thresholds.yml` (SSOT for thresholds)
    - Gates ONLY deterministic metrics: `all_digests_stable`, `errors_total`, `fill_rate`, `events_dropped`
    - Logs latency/memory as informational (not gated)
    - Usage: `python scripts/check_soak_gate.py --report report.json --thresholds monitoring/soak_thresholds.yml --mode baseline`
  - **CI workflow** (`.github/workflows/soak_gate.yml`):
    - Triggers on PRs touching `src/**`, `scripts/**`, `tests/fixtures/**`, `monitoring/soak_thresholds.yml`
    - Runs fixture-based soak with 3 runs per fixture
    - Calls `check_soak_gate.py` for deterministic-only validation
    - Uploads JSON report as artifact for inspection
  - **Nightly synthetic soak** (`.github/workflows/nightly_soak.yml`):
    - Uses `check_soak_thresholds.py` with full threshold validation (latency/memory included)
    - Appropriate for controlled environment with consistent runner performance
- **Consequences:**
  - PRs that break determinism will be blocked
  - PRs that cause errors will be blocked
  - Latency/memory regressions are visible in PR artifacts but not blocking (CI variance)
  - Nightly soak provides full threshold validation in stable environment
- **Alternatives:**
  - Gate on latency/memory — rejected; too flaky in CI
  - Only nightly soak — rejected; too slow feedback loop
  - Single run — rejected; doesn't catch non-determinism

## ADR-015 — HA v0 with Redis lease-lock for single-active safety
- **Date:** 2026-02-01
- **Status:** accepted
- **Context:** Production deployments need high availability to avoid single-point-of-failure. Full multi-node HA is complex; v0 focuses on single-host redundancy with single-active safety to prevent split-brain scenarios.
- **Decision:**
  - **Architecture:** Single-host redundancy with 2+ instances + Redis for coordination
  - **Leader election:** TTL-based lease lock using Redis SET with NX/XX and PX options
    - Lock TTL: 10 seconds
    - Renewal interval: 3 seconds (< TTL to prevent expiry during normal operation)
    - Lock key: `grinder:leader:lock`
  - **HA roles:**
    - `ACTIVE` — Instance holds the lock, actively trading
    - `STANDBY` — Instance is healthy but waiting to acquire lock
    - `UNKNOWN` — Initial state before first lock attempt
  - **Fail-safe semantics:**
    - If lock renewal fails → immediately become STANDBY (fail-safe)
    - Redis unavailable → all instances become STANDBY
    - Only ACTIVE instance executes trading logic
  - **Observability:**
    - `/readyz` endpoint: Returns 200 for ACTIVE, 503 for STANDBY/UNKNOWN
    - `/healthz` endpoint: Always 200 if process is alive (liveness, not readiness)
    - `grinder_ha_role{role}` metric: Gauge with all roles, current=1, others=0
  - **Deployment:** `docker-compose.ha.yml` with Redis 7.2 + 2 grinder instances
  - **Environment variables:**
    - `GRINDER_REDIS_URL` — Redis connection URL (default: redis://localhost:6379/0)
    - `GRINDER_HA_ENABLED` — Enable HA mode (default: false)
    - `GRINDER_HA_LOCK_TTL_MS` — Lock TTL in ms (default: 10000)
    - `GRINDER_HA_RENEW_INTERVAL_MS` — Renewal interval in ms (default: 3000)
- **Consequences:**
  - Single-active safety: Only one instance is ACTIVE at any time
  - Failover on leader crash: ~10s (lock TTL expiry)
  - Redis is SPOF for coordination (acceptable for single-host deployment)
  - No protection against host/VM failure (that's HA v1 scope)
- **Alternatives:**
  - Docs-only (no code) — rejected; need working HA for production readiness
  - File-based lock — rejected; not atomic, no TTL support
  - Active-active with partitioning — rejected; too complex for v0
  - Full Raft/Paxos consensus — rejected; overkill for single-host

## ADR-016 — Fill model v1: crossing/touch with determinism constraints
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** Paper trading v0 used "instant fill" model where all PLACE orders fill immediately at their limit price regardless of market state. This is unrealistic: real limit orders fill only when price reaches the limit. Need a more realistic fill model while preserving determinism for replay.
- **Decision:**
  - **Crossing/touch fill model (v1):**
    - LIMIT BUY fills if `mid_price <= limit_price` (price came down to our buy level)
    - LIMIT SELL fills if `mid_price >= limit_price` (price came up to our sell level)
    - Orders that don't cross/touch the mid price do NOT fill
  - **No partial fills:** v1 fills are all-or-nothing (max 1 fill per order)
  - **Determinism:** Fill simulation uses only `mid_price` and `limit_price` (both Decimal), no randomness
  - **Backward compatibility:**
    - `fill_mode="crossing"` (default) — new crossing/touch model
    - `fill_mode="instant"` — legacy instant-fill for backward compat
  - **Implementation:** `src/grinder/paper/fills.py::simulate_fills()` function
  - **Tests:** `tests/unit/test_fills.py` with crossing/touch and instant mode coverage
- **Consequences:**
  - Fixture digests updated (orders that don't cross now produce 0 fills):
    - `sample_day` = `66b29a4e92192f8f` (unchanged — blocked by gating, 0 fills)
    - `sample_day_allowed` = `3ecf49cd03db1b07` (was `ec223bce78d7926f`)
    - `sample_day_toxic` = `a31ead72fc1f197e` (was `66d57776b7be4797`)
    - `sample_day_multisymbol` = `22acba5cb8b81ab4` (was `7c4f4b07ec7b391f`)
    - `sample_day_controller` = `f3a0a321c39cc411` (unchanged — 0 fills with controller)
  - Fills are now more realistic: buy orders only fill when price drops, sell orders only fill when price rises
  - This is foundation for ASM (Adaptive Smart Grid v1) per §17.21 roadmap
- **Alternatives:**
  - Keep instant fill — rejected; too unrealistic for meaningful backtests
  - Probabilistic fill based on queue position — rejected; breaks determinism
  - Slippage model — deferred to v2; crossing/touch is sufficient for v1

## ADR-017 — CycleEngine v1: fill → TP + replenishment
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** Grid trading requires automatic cycle management: when a fill occurs, a take-profit (TP) order should be placed on the opposite side, and optionally a replenishment order to maintain grid depth. This is specified in §17.12.2 of the ASM v1 spec.
- **Decision:**
  - **CycleEngine module:** `src/grinder/paper/cycle_engine.py`
  - **TP generation logic:**
    - BUY fill at `p_fill` with `qty` → SELL TP at `p_fill * (1 + step_pct)` for same `qty`
    - SELL fill at `p_fill` with `qty` → BUY TP at `p_fill * (1 - step_pct)` for same `qty`
  - **Replenishment logic:**
    - If `adds_allowed=True`: place new order further out (same side as fill, opposite direction)
    - If `adds_allowed=False`: only TP orders, no new risk added
  - **Determinism constraints:**
    - Intent IDs: `cycle_{type}_{source_fill_id}_{side}_{price}`
    - Ordering: fills processed in order; TP before replenishment for each fill
    - Price/quantity: Decimal with configurable precision, ROUND_DOWN
  - **Integration with PaperEngine:**
    - `cycle_enabled` parameter (default False for backward compat)
    - `cycle_step_pct` parameter (default 0.001 = 10 bps)
    - `cycle_intents` field in PaperOutput (NOT included in digest for backward compat)
  - **adds_allowed determination (v1):**
    - False if controller mode is PAUSE
    - False if kill-switch is triggered
    - True otherwise
- **Consequences:**
  - CycleEngine generates CycleIntent objects representing desired TP/replenishment orders
  - Intents are recorded in output for observability but don't affect canonical digests
  - Foundation for full grid cycling in ASM v1 (next: intents → actual order placement)
  - Backward compatible: existing digests unchanged when cycle_enabled=False
- **Alternatives:**
  - Immediate order placement from fills — rejected; need intent layer for gating/validation
  - Fixed TP distance — rejected; step_pct allows adaptive sizing per controller
  - Martingale sizing — rejected; bounded replenishment is safer per §17.11.3

## ADR-018 — Sizing units SSOT: size_schedule is base quantity, NOT notional
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** `GridPlan.size_schedule` had ambiguous unit semantics. The `StaticGridPolicy` docstring incorrectly stated "quote currency (USD)" while execution engine treated it as base asset quantity. This ambiguity could lead to order sizing bugs (e.g., placing $0.01 orders instead of 0.01 BTC orders).
- **Decision:**
  - **SSOT:** `GridPlan.size_schedule` is ALWAYS interpreted as **base asset quantity** (e.g., BTC, ETH), NOT notional (USD)
  - **Documentation updates:**
    - `GridPlan` docstring: explicit Units section referencing this ADR
    - `StaticGridPolicy.size_per_level` docstring: fixed to "base asset quantity"
    - `ExecutionEngine._compute_grid_levels`: already correct ("quantity per level")
  - **Conversion utility:** `notional_to_qty(notional, price, precision)` in `src/grinder/policies/base.py`
    - Formula: `qty = notional / price`, rounded down to precision
    - Use this when configuring size_schedule from notional budget
  - **Reference:** docs/smart_grid/SPEC_V1_0.md §17.12.4 (canonical)
- **Consequences:**
  - All code interpreting `size_schedule` MUST treat values as base asset quantity
  - To configure from notional (USD) budget, explicitly convert: `notional_to_qty(500, 50000) = 0.01`
  - No backward compat impact: execution engine already uses qty interpretation
- **Alternatives:**
  - Allow both units with a flag — rejected; too error-prone, single SSOT is safer
  - Default to notional — rejected; qty is more intuitive for trading (you buy/sell qty, not USD)
  - No conversion utility — rejected; explicit helper prevents division-order bugs

## ADR-019 — Feature Engine v1: deterministic mid-bar OHLC + L1 microstructure
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** ASM v1 (Adaptive Smart Grid) requires computed market features for dynamic policy adjustment. Per §17.5, the policy needs:
  - **Volatility:** NATR (Normalized ATR) for adaptive grid spacing
  - **L1 microstructure:** imbalance, thin_l1 for order placement decisions
  - **Range/trend:** range_score for regime detection (choppy vs trending)
  Features must be deterministic for replay fidelity.
- **Decision:**
  - **Module structure:** `src/grinder/features/` with:
    - `bar.py` — MidBar frozen dataclass + BarBuilder for OHLC construction
    - `indicators.py` — compute_atr, compute_natr_bps, compute_imbalance_l1_bps, compute_thin_l1, compute_range_trend
    - `types.py` — FeatureSnapshot frozen dataclass with all computed features
    - `engine.py` — FeatureEngine orchestrator (maintains per-symbol state)
  - **Bar building (§17.5.1):**
    - Bar boundaries: floor division on bar_interval_ms (default 60s)
    - No synthesized bars for gaps (correct behavior)
    - MidBar: bar_ts, open, high, low, close, tick_count
  - **ATR/NATR (§17.5.2):**
    - True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    - ATR = SMA of TRs over period (default 14)
    - NATR = ATR / close, in integer bps for determinism
  - **L1 features (§17.5.3):**
    - imbalance_l1_bps = (bid_qty - ask_qty) / (bid_qty + ask_qty + eps) * 10000
    - thin_l1 = min(bid_qty, ask_qty)
    - spread_bps = (ask - bid) / mid * 10000
  - **Range/trend (§17.5.5):**
    - sum_abs_returns_bps = sum of |return_i| over horizon
    - net_return_bps = |p_end/p_start - 1|
    - range_score = sum_abs_returns_bps / (net_return_bps + 1)
    - High range_score = choppy; low = trending
  - **Warmup handling:**
    - ATR/NATR return 0/None until period+1 bars complete
    - FeatureSnapshot.is_warmed_up = warmup_bars >= 15
  - **Determinism guarantees:**
    - All intermediate calcs use Decimal
    - Final outputs: integer bps or Decimal (no floats)
    - Same snapshot sequence → identical features
  - **Backward compatibility:**
    - FeatureEngine is standalone; not yet integrated into PaperEngine
    - No changes to existing digests
    - Integration deferred to ASM-P1-02
- **Consequences:**
  - FeatureSnapshot can be passed to policy via to_policy_features()
  - Unit tests: test_bar_builder.py (25 tests), test_indicators.py (32 tests), test_feature_engine.py (26 tests)
  - Memory bounded: max_bars=1000 per symbol
  - Foundation for ASM v1 policy adaptation (§17.6, §17.7)
- **Alternatives:**
  - Use floating point — rejected; breaks determinism
  - External TA library (TA-Lib) — rejected; Decimal precision lost, extra dependency
  - Time-weighted VWAP bars — deferred to v2; mid-bars sufficient for v1
  - Volume bars — deferred; tick-based sufficient for initial ASM

## ADR-020 — Policy features plumbing (ASM-P1-03)
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** FeatureEngine computes market features (ADR-019), and PaperEngine integrates it (ASM-P1-02). The next step is to pass features to the policy for adaptive behavior. Per ASM v1 spec §17.6, policies need access to volatility (NATR), L1 microstructure (imbalance, thin_l1), and range metrics for dynamic parameter adjustment. However, this must be done without changing existing behavior (StaticGridPolicy ignores extra features).
- **Decision:**
  - **Plumbing approach:**
    - `FeatureSnapshot.to_policy_features()` returns typed dict with: mid_price, spread_bps, imbalance_l1_bps, natr_bps, thin_l1, range_score, net_return_bps, sum_abs_returns_bps, is_warmed_up
    - PaperEngine merges these features into `policy_features` dict when `feature_engine_enabled=True`
    - Policy receives full features dict via existing `evaluate(features: dict[str, Any])` interface
  - **Backward compatibility:**
    - `StaticGridPolicy` only uses `mid_price` from features dict, ignores extra keys
    - When `feature_engine_enabled=False` (default), policy receives only `{"mid_price": ...}` (existing behavior)
    - Canonical digests unchanged — features affect policy input but StaticGridPolicy ignores them
  - **No behavior change:**
    - This PR is pure plumbing — StaticGridPolicy behavior is identical with or without extra features
    - Future policies (e.g., AdaptiveGridPolicy) will use the additional features
  - **Testing:**
    - `test_policy_receives_features_when_enabled`: verifies full features dict passed to policy
    - `test_policy_receives_only_mid_price_when_disabled`: verifies minimal dict when disabled
    - `test_static_grid_policy_ignores_extra_features`: verifies StaticGridPolicy works with extra keys
    - `test_digests_unchanged_with_policy_features`: regression test for digest stability
- **Consequences:**
  - Policy interface unchanged (`GridPolicy.evaluate(features: dict[str, Any])` already accepts generic dict)
  - Foundation for ASM v1 adaptive policies that will use features
  - Determinism maintained — same inputs produce same outputs
- **Alternatives:**
  - Add explicit features parameter to policy — rejected; dict is flexible for future features
  - Pass FeatureSnapshot directly — rejected; dict is more generic and allows policy to not depend on features module
  - Change StaticGridPolicy to use features — deferred to AdaptiveGridPolicy (ASM-P1-05)

## ADR-021 — Deterministic regime classifier v1 (ASM-P1-04)
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** ASM v1 requires regime-driven policy behavior (§17.3). Before implementing adaptive policies, we need a deterministic regime classifier that classifies market conditions from FeatureSnapshot and gating state. The regime determines which policy behavior to activate (e.g., pause trading in EMERGENCY, widen grids in VOL_SHOCK, tighten in RANGE).
- **Decision:**
  - **Module:** `src/grinder/controller/regime.py` with:
    - `Regime` enum: RANGE, TREND_UP, TREND_DOWN, VOL_SHOCK, THIN_BOOK, TOXIC, PAUSED, EMERGENCY
    - `RegimeReason` enum: reason codes for each classification
    - `RegimeConfig` frozen dataclass: threshold configuration (no magic numbers)
    - `RegimeDecision` frozen dataclass: classified regime + reason + confidence + features_used
    - `classify_regime()` function: precedence-based classification
  - **Precedence order (first match wins):**
    1. kill_switch_active → EMERGENCY
    2. toxicity_result blocked → TOXIC
    3. thin_l1 < threshold OR spread_bps > threshold → THIN_BOOK
    4. natr_bps > vol_shock_threshold → VOL_SHOCK
    5. |net_return_bps| > trend_threshold AND range_score <= choppy_max → TREND_UP/DOWN
    6. else → RANGE
  - **Thresholds (configurable via RegimeConfig):**
    - `thin_l1_qty=0.1` — minimum L1 depth on thin side
    - `spread_thin_bps=100` — spread threshold for THIN_BOOK
    - `vol_shock_natr_bps=500` — NATR threshold for VOL_SHOCK
    - `trend_net_return_bps=200` — net return threshold for trend
    - `trend_range_score_max=3` — max range_score for trending (vs choppy)
  - **Determinism guarantees:**
    - All thresholds are integer bps (no floats)
    - Same inputs → same regime classification
    - Confidence is integer 0-100
  - **Boundary conditions (strict > for thresholds):**
    - At threshold → NOT triggered (need to exceed threshold)
    - Exception: range_score uses <= for max (at or below is trending)
- **Consequences:**
  - Regime classifier is standalone; can be used independently of policy
  - Future policies will use `classify_regime()` to determine behavior
  - Unit tests: 28 tests in `test_regime.py` covering all precedence branches + boundary cases
  - No changes to existing digests — classifier is not yet integrated into PaperEngine
- **Alternatives:**
  - ML-based regime detection — deferred to v2; rule-based is deterministic and interpretable
  - Continuous regime probability — rejected; discrete regimes are simpler for policy logic
  - Integrate directly into AdaptiveGridPolicy — rejected; separation of concerns, easier to test

## ADR-022 — AdaptiveGridPolicy v1 (L1-only step/width/levels, deterministic)
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** ASM v1 requires dynamic grid parameters based on market conditions (§17.8-17.10). StaticGridPolicy uses fixed spacing/levels which doesn't adapt to volatility changes. We need an adaptive policy that computes step/width/levels from NATR and regime while maintaining determinism.
- **Decision:**
  - **Module:** `src/grinder/policies/grid/adaptive.py` with:
    - `AdaptiveGridConfig`: threshold configuration (integer bps for determinism)
    - `AdaptiveGridPolicy`: policy class with `evaluate()` method
    - Helper functions: `compute_step_bps()`, `compute_width_bps()`, `compute_levels()`
  - **Step computation (§17.9):**
    - `step_bps = max(step_min_bps, alpha * NATR * regime_mult)`
    - `alpha` stored as integer (30 = 0.30) for determinism
    - Regime multipliers: RANGE=1.0, VOL_SHOCK=1.5, THIN_BOOK=2.0
  - **Width/X_stress computation (§17.8):**
    - `sigma_H = NATR * sqrt(H / TF)` (horizon volatility)
    - `X_stress = clamp(k_tail * sigma_H, X_min, X_cap)`
    - Asymmetric in TREND: more width on against-trend side
  - **Levels computation (§17.10):**
    - `levels = ceil(width / step)`, clamped to [levels_min, levels_max]
  - **Regime integration:**
    - Uses `classify_regime()` from ADR-021 for market condition detection
    - EMERGENCY/TOXIC/PAUSED → pause plan (levels=0, reset=HARD)
    - VOL_SHOCK → wider step
    - TREND_UP/DOWN → asymmetric width
  - **PaperEngine integration:**
    - `adaptive_policy_enabled` flag (default False for backward compat)
    - When enabled, uses AdaptiveGridPolicy instead of StaticGridPolicy
    - Requires `feature_engine_enabled=True` for features
  - **Sizing:** Legacy (fixed size_per_level from config) — auto-sizing deferred to P1-05b
  - **Determinism guarantees:**
    - All thresholds and multipliers are integer bps
    - Integer arithmetic for all intermediate calculations
    - Same inputs → same GridPlan
- **Consequences:**
  - AdaptiveGridPolicy produces different GridPlan than StaticGridPolicy
  - New fixture `sample_day_adaptive` with separate digest (existing fixtures unchanged)
  - Unit tests: 23 tests in `test_adaptive_policy.py` for step/width/levels + boundaries
  - Existing digests unchanged (adaptive_policy_enabled=False by default)
- **Alternatives:**
  - Float arithmetic — rejected; breaks determinism
  - Single unified policy — rejected; StaticGridPolicy useful for comparison/debugging
  - Auto-sizing in v1 — deferred to P1-05b to keep PR scope manageable

## ADR-023 — Top-K v1: Feature-Based Symbol Selection (ASM-P1-06)
- **Date:** 2026-02-03
- **Status:** accepted
- **Context:** ASM v1 requires selecting the best K symbols for trading based on market characteristics. The existing Top-K v0 uses simple price volatility which doesn't consider range quality, liquidity, toxicity, or trend strength. We need a feature-based selection that integrates with FeatureEngine outputs.
- **Decision:**
  - **Module:** `src/grinder/selection/topk_v1.py` with:
    - `TopKConfigV1`: configuration (k, spread_max_bps, thin_l1_min, warmup_min, weights)
    - `SelectionCandidate`: input data structure (symbol, range_score, spread_bps, thin_l1, net_return_bps, warmup_bars, toxicity_blocked)
    - `SymbolScoreV1`: output with score components and gate status
    - `SelectionResult`: result with selected symbols and all scores
    - `select_topk_v1()`: main selection function
  - **Scoring formula (integer-only for determinism):**
    - `score = range_component + liquidity_component - toxicity_penalty - trend_penalty`
    - `range_component = w_range * range_score / 100`
    - `liquidity_component = ilog10(thin_l1 + 1) * liq_scale * w_liquidity / 100`
      - `ilog10(x) = len(str(x)) - 1` (floor of log10, deterministic via digit counting)
      - No float operations — fully portable across platforms/compilers
    - `toxicity_penalty = w_toxicity * 100` (fixed penalty when blocked)
    - `trend_penalty = w_trend * abs(net_return_bps) / 100`
  - **Hard gates (exclude before scoring):**
    - `TOXICITY_BLOCKED`: toxicity_blocked=True from ToxicityGate
    - `SPREAD_TOO_WIDE`: spread_bps > spread_max_bps
    - `THIN_BOOK`: thin_l1 < thin_l1_min
    - `WARMUP_INSUFFICIENT`: warmup_bars < warmup_min
  - **Tie-breaking:** deterministic sort by `(-score, symbol)` for stable ordering
  - **PaperEngine integration:**
    - `topk_v1_enabled` flag (default False for backward compat)
    - `topk_v1_config` parameter for configuration
    - First pass: feed all events to FeatureEngine for warmup
    - Build candidates from cached features + toxicity state
    - Run selection, filter symbols
    - Symbols not in Top-K get `not_in_topk=True` in output
  - **FeatureEngine extension:**
    - Added `_latest_snapshots` cache for per-symbol feature tracking
    - `get_latest_snapshot(symbol)` and `get_all_latest_snapshots()` methods
  - **Determinism guarantees:**
    - All weights and thresholds are integers
    - Integer-only arithmetic throughout scoring (no float operations)
    - Liquidity uses `ilog10` (digit counting) instead of `math.log10` to avoid float
    - Deterministic tie-breaking by `(-score, symbol)` for stable ordering
    - Same inputs → identical selection across all platforms
- **Consequences:**
  - New fixture `sample_day_topk_v1` with 6 symbols demonstrating selection
  - When enabled, replaces v0 volatility-based selection
  - Gate-excluded symbols marked in scores (gates_failed list)
  - PaperResult includes `topk_v1_selected_symbols`, `topk_v1_scores`, `topk_v1_gate_excluded`
  - PaperOutput includes `not_in_topk`, `topk_v1_rank` fields
  - 20 unit tests in `test_topk_v1.py`
- **Alternatives:**
  - Float weights — rejected; breaks determinism
  - `math.log10` for liquidity — rejected; float not bit-identical across platforms/libc
  - Single-pass selection — rejected; need FeatureEngine warmup first
  - Real-time re-selection — deferred; current approach selects once after warmup

## ADR-024 — Connector Hardening v1: Timeouts + Error Hierarchy + Clean Shutdown (PR-H1)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** DataConnector and BinanceWsMockConnector need production-grade hardening before live deployment. Per M3 roadmap, this includes explicit timeouts, typed error hierarchy, and clean shutdown semantics to prevent zombie tasks.
- **Decision:**
  - **TimeoutConfig extended:** Added `write_timeout_ms` (5000ms default) and `close_timeout_ms` (5000ms default) alongside existing `connect_timeout_ms` and `read_timeout_ms`
  - **Error hierarchy:** Created `src/grinder/connectors/errors.py` with:
    - `ConnectorError` — base exception for all connector errors
    - `ConnectorTimeoutError(op, timeout_ms)` — timeout during connect/read/write/close
    - `ConnectorClosedError(op)` — operation attempted on closed connector
    - `ConnectorIOError` — base for I/O errors
    - `ConnectorTransientError` — retryable errors (scaffolding for H2)
    - `ConnectorNonRetryableError` — non-retryable errors (scaffolding for H2/H4)
  - **Timeout utilities:** Created `src/grinder/connectors/timeouts.py` with:
    - `wait_for_with_op(coro, timeout_ms, op)` — wraps `asyncio.wait_for` with `ConnectorTimeoutError`
    - `cancel_tasks_with_timeout(tasks, timeout_ms)` — clean task cancellation with timeout
    - `create_named_task(coro, name, tasks_set)` — tracked task creation with auto-removal on completion
  - **Task tracking:** BinanceWsMockConnector tracks background tasks in `self._tasks` set with named prefix for clean shutdown
  - **Clean shutdown:** `close()` cancels all tracked tasks via `cancel_tasks_with_timeout()`, waits for completion within `close_timeout_ms`, clears events and cursor
  - **Stats tracking:** `MockConnectorStats` extended with `tasks_cancelled` and `tasks_force_killed` counters
- **Consequences:**
  - All timeout-related errors raise `ConnectorTimeoutError` (not `asyncio.TimeoutError`)
  - Closed connector operations raise `ConnectorClosedError` (not generic `RuntimeError`)
  - No zombie tasks after `close()` — all tasks cancelled and awaited
  - Tests: 47 tests in `test_data_connector.py` (was 28), including `TestConnectorTimeouts`, `TestConnectorCleanShutdown`, `TestConnectorErrorHierarchy`
  - Error hierarchy prepares for H2 (retries) and H4 (circuit breaker)
- **Alternatives:**
  - Per-call timeout parameters — rejected; TimeoutConfig as connector attribute is cleaner and consistent
  - Raw `asyncio.TimeoutError` — rejected; typed errors enable better error handling upstream
  - Fire-and-forget task cancellation — rejected; must await to ensure clean shutdown

## ADR-025 — Connector Hardening v2: Retry Utilities (PR-H2)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Following H1 (Timeouts + Error Hierarchy), H2 adds retry-with-backoff utilities for transient connector failures. Per M3 hardening roadmap, retry logic must be centralized, testable, and support deterministic testing (no real sleeps in unit tests).
- **Decision:**
  - **RetryPolicy** (`src/grinder/connectors/retries.py`): frozen dataclass configuring retry behavior:
    - `max_attempts` (default 3): total attempts (1 = no retries)
    - `base_delay_ms` (default 100): initial delay between retries
    - `max_delay_ms` (default 5000): maximum delay cap for backoff
    - `backoff_multiplier` (default 2.0): exponential backoff factor
    - `retry_on_timeout` (default True): whether to retry `ConnectorTimeoutError`
    - `compute_delay_ms(attempt)`: computes delay for given attempt with exponential backoff and cap
  - **RetryStats**: tracks `attempts`, `retries`, `total_delay_ms`, `last_error`, `errors` list
  - **is_retryable(error, policy)**: error classification function:
    - `ConnectorTransientError` → always retryable
    - `ConnectorTimeoutError` → retryable if `policy.retry_on_timeout`
    - `ConnectorNonRetryableError`, `ConnectorClosedError` → never retryable
    - Unknown exceptions → fail fast (not retried)
  - **retry_with_policy()**: async utility wrapping operations with retry logic:
    - `sleep_func` parameter for injecting fake sleep in tests (bounded-time testing)
    - `on_retry` callback for observability/logging
    - Returns `tuple[T, RetryStats]` for telemetry
  - **TransientFailureConfig**: mock connector configuration for simulating failures:
    - `connect_failures`: N connect attempts fail before success
    - `read_failures`: N read operations fail before success
    - `failure_message`: custom message for simulated errors
  - **BinanceWsMockConnector updated**: accepts `transient_failure_config` parameter, injects failures in `_do_connect()` and `iter_snapshots()`, tracks via `stats.transient_failures_injected`
- **Consequences:**
  - Retry logic is centralized and reusable for any connector
  - Tests are bounded-time (no real sleeps) via `sleep_func` injection
  - Mock connector can simulate transient failures for testing retry behavior
  - Tests: 35 new tests in `test_retries.py` covering policy validation, error classification, retry behavior, and mock integration
  - Prepares for H3 (idempotency) and H4 (circuit breaker)
- **Alternatives:**
  - Inline retry loops in connector methods — rejected; centralized utility is cleaner and testable
  - Real sleeps in tests — rejected; bounded-time tests are faster and deterministic
  - Retry all exceptions — rejected; fail fast on unknown errors prevents masking bugs

## ADR-026 — Connector Hardening v3: Idempotency (PR-H3)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Following H2 (Retries), H3 adds idempotency guarantees for write operations (place/cancel/amend). Without idempotency, retries can cause duplicate side-effects (double orders, double cancels). Per M3 hardening roadmap, write-path must be safe for retries.
- **Decision:**
  - **Idempotency Key Format**: `{scope}:{op}:{sha256_hex[:32]}` — deterministic hash of canonical payload, NOT random UUID
    - Key components for place: `symbol`, `side`, `price`, `quantity`, `level_id`
    - Key components for cancel: `order_id`
    - Key components for replace: `order_id`, `price`, `quantity`
    - **EXCLUDED from key**: `ts` (timestamp), nonces, random values — same intent at different times MUST produce same key
    - Canonicalization: JSON with sorted keys, Decimal normalized, None excluded
  - **IdempotencyStatus enum**: `INFLIGHT`, `DONE`, `FAILED`
  - **IdempotencyEntry** (`src/grinder/connectors/idempotency.py`): dataclass tracking:
    - `key`, `status`, `op_name`, `request_fingerprint`
    - `created_at`, `expires_at`, `result`, `error_code`
  - **IdempotencyStore protocol**: pluggable interface for storage:
    - `get(key)` → entry or None (expired entries return None)
    - `put_if_absent(key, entry, ttl_s)` → True if stored, False if exists
    - `mark_done(key, result)` → update status and extend TTL
    - `mark_failed(key, error_code)` → mark for retry
    - `purge_expired(now)` → clean up old entries
  - **InMemoryIdempotencyStore**: thread-safe in-memory implementation:
    - Injectable clock for testing (`_clock` parameter)
    - FAILED entries can be overwritten (retry allowed)
    - Default TTLs: 300s for INFLIGHT, 86400s for DONE
  - **IdempotentExchangePort** (`src/grinder/execution/idempotent_port.py`): wrapper for ExchangePort:
    - Wraps `place_order`, `cancel_order`, `replace_order` with idempotency checks
    - Flow: check store → if DONE return cached → if INFLIGHT raise conflict → put_if_absent → execute → mark_done
    - `fetch_open_orders` passes through (no idempotency for reads)
    - Stats tracking: `place_calls`, `place_cached`, `place_executed`, `place_conflicts`
  - **IdempotencyConflictError**: fast-fail on INFLIGHT duplicates (non-retryable)
  - **Integration with retries**: key created ONCE before retries, all retry attempts use same key → at most 1 side-effect
- **Consequences:**
  - Write operations are safe for retries — duplicate requests return cached result
  - Concurrent duplicate requests get deterministic behavior (fast-fail)
  - FAILED operations allow retry (entry overwritten)
  - Tests: 32 tests in `test_idempotency.py` covering key generation, store operations, port behavior, retry integration
  - Prepares for H4 (circuit breaker) and production Redis store
- **Alternatives:**
  - Random UUID keys — rejected; would break idempotency on retry (new key = new operation)
  - Wait-for-INFLIGHT pattern — rejected; fast-fail is simpler and avoids deadlocks in v1
  - Retry INFLIGHT conflicts — rejected; conflicts indicate concurrent duplicates, should fail fast

## ADR-027 — Connector Hardening v4: Circuit Breaker (PR-H4)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Following H2 (Retries) and H3 (Idempotency), H4 adds circuit breaker pattern to prevent cascading failures. Without circuit breaker, retries can hammer a degraded upstream indefinitely ("thundering herd"). Per M3 hardening roadmap, write-path must have fail-fast protection.
- **Decision:**
  - **Circuit States**: `CLOSED`, `OPEN`, `HALF_OPEN`
    - CLOSED: normal operation, failures counted toward threshold
    - OPEN: fast-fail all requests, cooldown timer running
    - HALF_OPEN: allow limited probes, success → CLOSED, failure → OPEN
  - **Per-operation tracking**: each operation (place/cancel/replace) has independent circuit state
    - Rationale: one operation failing (e.g., place) shouldn't block unrelated operations (e.g., cancel)
  - **CircuitBreakerConfig** (`src/grinder/connectors/circuit_breaker.py`):
    - `failure_threshold`: consecutive failures to trip OPEN (default: 5)
    - `open_interval_s`: seconds to stay OPEN before HALF_OPEN (default: 30)
    - `half_open_probe_count`: max probes allowed in HALF_OPEN (default: 1)
    - `success_threshold`: consecutive successes to close (default: 1)
    - `trip_on`: callable to determine if error counts as breaker-worthy
  - **trip_on semantics**:
    - `default_trip_on` counts: `ConnectorTransientError`, `ConnectorTimeoutError`
    - Does NOT count: `ConnectorNonRetryableError`, `IdempotencyConflictError`, `CircuitOpenError`
    - Rationale: only upstream failures should trip breaker, not client-side errors
  - **CircuitBreaker methods**:
    - `allow(op_name) -> bool`: check if operation allowed
    - `before_call(op_name)`: raises `CircuitOpenError` if not allowed
    - `record_success(op_name)`: count success, may close circuit
    - `record_failure(op_name, reason)`: count failure, may open circuit
    - `state(op_name) -> CircuitState`: get current state
  - **CircuitOpenError**: non-retryable error raised when circuit is OPEN
  - **Injectable clock**: `clock` parameter for deterministic testing
  - **Integration order** for write-path (implemented in IdempotentExchangePort):
    1. `breaker.before_call(op)` — fast-fail if OPEN
    2. Idempotency check (DONE → return, INFLIGHT → conflict)
    3. Execute operation
    4. `breaker.record_success(op)` or `breaker.record_failure(op, reason)` based on outcome
  - Rationale: breaker sits BEFORE idempotency to prevent any interaction with degraded upstream
  - **H4-01**: CircuitBreaker module implemented as library
  - **H4-02**: Wired into IdempotentExchangePort with optional breaker/trip_on parameters
- **Consequences:**
  - Degraded upstream triggers fast-fail, not retry storms
  - Per-operation isolation prevents one bad endpoint from blocking all operations
  - HALF_OPEN probes allow automatic recovery detection
  - Tests: 20+ tests in `test_circuit_breaker.py` covering state transitions, fast-fail, per-op isolation
  - Prepares for H5 (observability metrics for circuit state)
- **Alternatives:**
  - Global circuit (not per-op) — rejected; too coarse, one bad endpoint shouldn't block all
  - No HALF_OPEN, manual reset only — rejected; auto-recovery is essential for hands-off operation
  - Retry OPEN calls with backoff — rejected; defeats purpose of circuit breaker, use HALF_OPEN probes instead

## ADR-028 — Connector Observability v1: H2/H3/H4 Metrics (PR-H5)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Following H2 (Retries), H3 (Idempotency), and H4 (Circuit Breaker), we need Prometheus metrics to observe these components in production. Without metrics, operators cannot detect retry storms, idempotency conflicts, or circuit breaker trips until they cause visible failures.
- **Decision:**
  - **Metric naming convention:**
    - Prefix: `grinder_` (consistent with existing metrics)
    - Counter metrics: `_total` suffix
    - Gauge metrics: no suffix (state)
  - **H2 Retry metrics:**
    - `grinder_connector_retries_total{op, reason}` (counter): total retry events
    - Labels: `op` = operation name (place, cancel, replace, etc.), `reason` = transient, timeout, other
  - **H3 Idempotency metrics:**
    - `grinder_idempotency_hits_total{op}` (counter): cached result returned (no upstream call)
    - `grinder_idempotency_conflicts_total{op}` (counter): INFLIGHT duplicate rejected
    - `grinder_idempotency_misses_total{op}` (counter): new key, upstream call made
  - **H4 Circuit Breaker metrics:**
    - `grinder_circuit_state{op, state}` (gauge): 1 for current state, 0 for others
    - States: closed, open, half_open
    - `grinder_circuit_rejected_total{op}` (counter): calls rejected due to OPEN circuit
    - `grinder_circuit_trips_total{op, reason}` (counter): circuit trips (CLOSED → OPEN)
    - Reason: threshold (consecutive failures exceeded)
  - **Cardinality rules (low cardinality only):**
    - `op` — operation name only (place, cancel, replace, test_op, etc.)
    - `reason` — enum values only (transient, timeout, other, threshold)
    - `state` — enum values only (closed, open, half_open)
    - **EXCLUDED from labels:** symbol, order_id, idempotency_key, timestamps, client_id
    - Rationale: high-cardinality labels cause Prometheus scrape timeouts and memory issues
  - **Module:** `src/grinder/connectors/metrics.py` with:
    - `ConnectorMetrics` — dataclass tracking all counters/gauges
    - `to_prometheus_lines()` — Prometheus text format output
    - `get_connector_metrics()` / `reset_connector_metrics()` — global singleton
    - `CircuitMetricState` — enum for state gauge (CLOSED, OPEN, HALF_OPEN)
  - **Integration points:**
    - `retries.py` — records retry metric after each retry event
    - `idempotent_port.py` — records hit/conflict/miss metrics
    - `circuit_breaker.py` — records state/rejected/trip metrics
    - `metrics_builder.py` — includes connector metrics in `/metrics` output
    - `live_contract.py` — updated REQUIRED_METRICS_PATTERNS
  - **Compatibility policy:**
    - Metric names and label keys are stable contracts (same as gating metrics)
    - Adding new label values is safe (append-only)
    - Removing/renaming metrics or labels is a breaking change requiring ADR
    - Dashboards/alerts can rely on these metric names
- **Consequences:**
  - Operators can monitor retry rates, idempotency behavior, and circuit breaker health
  - Alerts can trigger on: high retry rate, frequent conflicts, circuit trips
  - Low cardinality ensures Prometheus stability at scale
  - Unit tests: 45+ tests in `test_connector_metrics.py` covering all metric types
- **Alternatives:**
  - High-cardinality labels (symbol, order_id) — rejected; Prometheus cardinality explosion
  - Separate metrics endpoints — rejected; single `/metrics` is simpler and standard
  - Histogram for retry delays — deferred; counters sufficient for v1

## ADR-029 — Live Connector v0: SafeMode + Hardening (M3-LC-01)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Production deployment requires a live WebSocket connector for real-time market data from Binance. Connector must integrate existing hardening (H2 retries, H4 circuit breaker, H5 metrics) while enforcing safety constraints to prevent accidental live trading.
- **Decision:**
  - **SafeMode enum** (`src/grinder/connectors/live_connector.py`):
    - `READ_ONLY` (default): Only read market data, no trading operations
    - `PAPER`: Read data + simulated trading (paper mode)
    - `LIVE_TRADE`: Full trading capability (requires explicit opt-in)
  - **Safe defaults:**
    - Default mode is `READ_ONLY` — must explicitly opt into trading
    - Default URL is Binance testnet (`wss://testnet.binance.vision/ws`) — must explicitly configure mainnet
    - Method `assert_mode(required_mode)` — raises `ConnectorNonRetryableError` if current mode is insufficient
  - **LiveConnectorV0** extends `DataConnector` ABC:
    - `connect()`: establish WebSocket connection with H2 retries and H4 circuit breaker
    - `close()`: clean shutdown with task cancellation
    - `stream_ticks()` / `iter_snapshots()`: yield `Snapshot` objects (DataConnector compliance)
    - `subscribe(symbols)`: add symbols to subscription
    - `reconnect()`: reconnect after failure, preserving `last_seen_ts` for idempotency
  - **H2/H4/H5 integration:**
    - `RetryPolicy` for transient connection failures (configurable attempts, backoff)
    - `CircuitBreaker` for fast-fail when upstream is degraded
    - `get_connector_metrics()` integration for observability
  - **Bounded-time testing:**
    - Injectable `clock` parameter for deterministic time control
    - Injectable `sleep_func` parameter for instant test execution
    - FakeClock + FakeSleep utilities in tests
  - **Configuration** (`LiveConnectorConfig`):
    - `mode`: SafeMode (default: READ_ONLY)
    - `symbols`: List of symbols to subscribe
    - `ws_url`: WebSocket URL (default: testnet)
    - `timeout_config`: TimeoutConfig for connect/read/close
    - `retry_policy`: RetryPolicy for connection retries
    - `circuit_breaker_config`: CircuitBreakerConfig for fast-fail
  - **Statistics** (`LiveConnectorStats`):
    - `ticks_received`, `connection_attempts`, `reconnections`, `retries`, `circuit_trips`, `timeouts`, `errors`
  - **V0 scope** (mock implementation):
    - `stream_ticks()` yields nothing (placeholder for real WebSocket integration)
    - Contract verified, hardening wired, integration tests pass
    - Real WebSocket integration deferred to v1
- **Consequences:**
  - Live connector is safe by default — must explicitly opt into trading
  - Existing hardening (H2/H4/H5) reused without duplication
  - Bounded-time tests complete in milliseconds, no flaky waits
  - Unit tests: 31 tests in `test_live_connector.py`
  - Integration tests: 6 tests in `test_live_connector_integration.py`
- **Alternatives:**
  - No SafeMode, rely on URL config only — rejected; too easy to accidentally trade on mainnet
  - Separate read/write connectors — rejected; unnecessary complexity for v0
  - Real WebSocket in v0 — rejected; contract-first approach, real integration in v1

## ADR-030 — Paper Write-Path v0: Simulated Trading (M3-LC-02)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Paper trading mode (`SafeMode.PAPER`) was defined in ADR-029 but had no write operations implemented. Need to add `place_order`, `cancel_order`, `replace_order` to `LiveConnectorV0` while maintaining safety guarantees (no network calls, deterministic behavior).
- **Decision:**
  - **PaperExecutionAdapter** (`src/grinder/connectors/paper_execution.py`):
    - In-memory order storage (no persistence)
    - Deterministic order ID generation: `{prefix}_{seq:08d}` (e.g., `PAPER_00000001`)
    - Injectable `clock` for deterministic timestamps
    - No network calls — pure simulation
  - **V0 Order Semantics (instant fill):**
    - `place_order`: Creates order → immediately FILLED (no market simulation)
    - `cancel_order`: If OPEN/PENDING → CANCELLED; if FILLED → `PaperOrderError`; if CANCELLED → idempotent return
    - `replace_order`: Cancel old order + place new order (cancel+new pattern), returns NEW order_id
  - **SafeMode Enforcement:**
    - `READ_ONLY`: All write operations raise `ConnectorNonRetryableError` (non-retryable by design)
    - `PAPER`: Write operations delegated to `PaperExecutionAdapter`
    - `LIVE_TRADE`: Not implemented in v0, raises `ConnectorNonRetryableError`
  - **Wire-up in LiveConnectorV0:**
    - `paper_adapter` property — `PaperExecutionAdapter | None`, initialized only in PAPER mode
    - `place_order(symbol, side, price, quantity, client_order_id)` → `OrderResult`
    - `cancel_order(order_id)` → `OrderResult`
    - `replace_order(order_id, new_price, new_quantity)` → `OrderResult`
    - All methods check `ConnectorState.CLOSED` first
  - **Types:**
    - `OrderRequest`: Frozen dataclass for place/replace input
    - `OrderResult`: Frozen dataclass for operation result (snapshot of order state)
    - `PaperOrder`: Mutable internal order record
    - `OrderType`: Enum (LIMIT, MARKET)
    - `PaperOrderError`: Non-retryable error for paper order failures
  - **No H2/H4 Wiring for Paper:**
    - Paper execution is synchronous and never fails transiently
    - Retries and circuit breaker not needed for in-memory simulation
    - Errors are logical (invalid state) not transient (network)
- **Mode → Operations → Backend Table:**
  | Mode        | stream_ticks | place_order | cancel_order | replace_order | Backend           |
  |-------------|--------------|-------------|--------------|---------------|-------------------|
  | READ_ONLY   | ✓            | ✗           | ✗            | ✗             | N/A               |
  | PAPER       | ✓            | ✓           | ✓            | ✓             | PaperExecutionAdapter |
  | LIVE_TRADE  | ✓            | ✗ (v0)      | ✗ (v0)       | ✗ (v0)        | Not implemented   |
- **Consequences:**
  - Paper mode provides full order lifecycle without exchange dependency
  - Order IDs are deterministic for replay/testing
  - No network calls in PAPER mode — tests are fast and reliable
  - Unit tests: 21 tests in `test_paper_execution.py`
  - Integration tests: 17 tests in `test_paper_write_path.py`
- **Out of Scope (v0):**
  - Real trading (testnet/mainnet) — deferred to M3-LC-03
  - Partial fills / slippage / L2 market simulation — deferred
  - PnL calculation and commission modeling — deferred
  - Persistent order storage — deferred
- **Alternatives:**
  - Shared adapter for PAPER and LIVE_TRADE — rejected; different requirements (mock vs real API)
  - Random order IDs — rejected; breaks determinism for replay
  - Async paper operations — rejected; unnecessary complexity for in-memory simulation


## ADR-031 — Auto-Sizing v1: Risk-Budget-Based Position Sizing (ASM-P2-01)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** Grid policies used uniform `size_per_level` without risk awareness. Position sizes were manually configured and didn't adapt to account equity, drawdown limits, or adverse market scenarios. This made it hard to ensure portfolio-level risk stayed within bounds.
- **Decision:**
  - **AutoSizer Module** (`src/grinder/sizing/auto_sizer.py`):
    - Pure function computing size_schedule from risk parameters
    - Inputs: `equity`, `dd_budget`, `adverse_move`, `grid_shape`, `price`
    - Output: `SizeSchedule` with `qty_per_level[]` and risk metrics
  - **Core Formula:**
    ```
    max_loss_usd = equity * dd_budget
    total_qty_allowed = max_loss_usd / (price * adverse_move)
    qty_per_level = total_qty_allowed / n_levels  (for uniform mode)
    ```
    - Worst-case assumption: All levels fill on one side, price moves by `adverse_move`
    - Always rounds DOWN to stay within risk budget
  - **Sizing Modes:**
    - `UNIFORM`: Equal quantity at each level (default)
    - `PYRAMID`: Larger quantities at outer levels
    - `INVERSE_PYRAMID`: Smaller quantities at outer levels
  - **Integration with AdaptiveGridPolicy:**
    - `auto_sizing_enabled: bool` flag in config (default False for backward compat)
    - When enabled, `_compute_size_schedule()` uses AutoSizer instead of legacy uniform sizing
    - Falls back to legacy if equity/dd_budget/adverse_move are missing
  - **Determinism:**
    - Pure function: same inputs → same outputs
    - Integer bps for risk parameters
    - Decimal arithmetic with explicit rounding
- **Risk Bound Guarantee:**
  ```
  worst_case_loss = sum(qty_i * price) * adverse_move
  constraint: worst_case_loss <= equity * dd_budget
  ```
  - Due to ROUND_DOWN, actual utilization is always <= 100%
  - `risk_utilization` metric tracks efficiency (should be ~90-99%)
- **Configuration:**
  ```python
  AdaptiveGridConfig(
      auto_sizing_enabled=True,
      equity=Decimal("10000"),          # Account equity
      dd_budget=Decimal("0.20"),        # 20% max drawdown
      adverse_move=Decimal("0.25"),     # 25% worst-case price move
      auto_sizer_config=AutoSizerConfig(
          sizing_mode=SizingMode.UNIFORM,
          min_qty=Decimal("0.0001"),    # Exchange minimum
          qty_precision=8,
      ),
  )
  ```
- **Consequences:**
  - Size schedules automatically adapt to account equity and risk parameters
  - Risk bound is enforced: worst-case loss stays within dd_budget
  - Backward compatible: legacy uniform sizing when auto_sizing_enabled=False
  - Unit tests: 36 tests in `test_auto_sizer.py`
  - Integration tests: 5 tests in `test_adaptive_policy.py::TestAutoSizingIntegration`
- **Out of Scope (v1):**
  - Multi-symbol DD allocation (deferred to ASM-P2-02)
  - Dynamic equity tracking (equity is static config, not live P&L)
  - Position-aware sizing (doesn't account for existing inventory)
  - L2 depth-aware sizing
- **Alternatives:**
  - Kelly criterion sizing — rejected; requires probability estimates we don't have
  - VaR-based sizing — rejected; needs distribution assumptions
  - Fixed notional sizing — rejected; doesn't scale with account size

## ADR-032 — DD Allocator v1: Portfolio-to-Symbol Budget Distribution (ASM-P2-02)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** ASM-P2-01 (AutoSizer) computes per-symbol size schedules from `dd_budget`, but doesn't address how to distribute a portfolio-level drawdown budget across multiple symbols. Manual allocation is error-prone and doesn't account for relative risk (volatility tiers).
- **Decision:**
  - **DdAllocator Module** (`src/grinder/sizing/dd_allocator.py`):
    - Pure function distributing portfolio DD budget across symbols
    - Inputs: `equity`, `portfolio_dd_budget`, `candidates[]` (symbol, tier, weight, enabled)
    - Output: `AllocationResult` with per-symbol budgets and residual
  - **Algorithm:**
    1. Filter to enabled symbols only
    2. Compute `risk_weight = user_weight / tier_factor` for each symbol
    3. Normalize weights to sum to 1.0
    4. Multiply normalized weights by `portfolio_budget_usd`
    5. ROUND_DOWN to `budget_precision` decimal places
    6. Residual goes to cash reserve (not reallocated)
  - **Tier Factors (v1):**
    - `LOW`: 1.0 (lowest risk, gets most budget)
    - `MED`: 1.5
    - `HIGH`: 2.0 (highest risk, gets least budget)
    - Higher factor = higher risk = smaller budget allocation
  - **Invariants (must always hold):**
    1. **Non-negativity:** All budgets >= 0
    2. **Conservation:** `sum(budgets) + residual == portfolio_budget` (exact with Decimal)
    3. **Determinism:** Same inputs → same outputs (sorted by symbol)
    4. **Monotonicity:** Larger portfolio budget → no symbol budget decreases
    5. **Tier ordering:** At equal weights, HIGH <= MED <= LOW budget
  - **Integration with AutoSizer:**
    - DdAllocator output: `allocations[symbol]` = per-symbol dd_budget fraction
    - Feed to AdaptiveGridConfig: `dd_budget=allocations[symbol]`
    - AutoSizer then computes size_schedule using this per-symbol budget
  - **Residual Policy:**
    - ROUND_DOWN creates small residual (< sum of rounding errors)
    - Residual stays in `residual_usd` field (cash reserve)
    - Not reallocated to avoid complexity; caller can add to lowest-risk symbol if desired
- **Configuration:**
  ```python
  allocator = DdAllocator(DdAllocatorConfig(
      tier_factors={
          RiskTier.LOW: Decimal("1.0"),
          RiskTier.MED: Decimal("1.5"),
          RiskTier.HIGH: Decimal("2.0"),
      },
      budget_precision=2,      # Round to cents
      min_budget_usd=Decimal("1.0"),  # Below this = 0
  ))

  result = allocator.allocate(
      equity=Decimal("100000"),
      portfolio_dd_budget=Decimal("0.20"),  # 20% total
      candidates=[
          SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
          SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED),
          SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW),
      ],
  )
  # result.allocations = {"BNBUSDT": 0.0869..., "BTCUSDT": 0.0434..., "ETHUSDT": 0.0580...}
  ```
- **Example (3 symbols, equal weights, default tiers):**
  | Symbol | Tier | Factor | Risk Weight | Normalized | Budget USD |
  |--------|------|--------|-------------|------------|------------|
  | BNBUSDT | LOW | 1.0 | 1.0 | 0.4348 | $8,695.65 |
  | ETHUSDT | MED | 1.5 | 0.667 | 0.2899 | $5,797.10 |
  | BTCUSDT | HIGH | 2.0 | 0.5 | 0.2174 | $4,347.83 |
  | **Total** | | | | 1.0 | $18,840.58 |
  | Residual | | | | | $1,159.42 |
- **Consequences:**
  - Portfolio-level risk is automatically distributed to symbols
  - Higher-risk symbols get smaller budgets (conservative)
  - Custom weights allow overriding tier-based allocation
  - Unit tests: 28 tests in `test_dd_allocator.py` (all 5 invariants covered)
  - Integration tests: 3 tests in `test_adaptive_policy.py::TestDdAllocatorIntegration`
- **Out of Scope (v1):**
  - Dynamic correlation-aware allocation (Markowitz-style)
  - Real-time volatility estimation for tier assignment
  - Learning/EMA adaptation of tier factors
  - Residual reallocation strategies
- **Alternatives:**
  - Equal allocation — rejected; ignores risk differences
  - Inverse-volatility weighting — rejected; requires vol estimates we may not have
  - Risk parity — rejected; needs covariance matrix

## ADR-033 — Drawdown Guard Wiring v1: Intent-Based Risk Blocking (ASM-P2-03)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** ASM-P2-01/02 provide auto-sizing and DD allocation, but there's no mechanism to enforce risk limits at runtime. When portfolio or symbol DD exceeds limits, the system should block risk-increasing orders while allowing risk-reducing ones. This requires a deterministic guard that can be wired into the policy/execution pipeline.
- **Decision:**
  - **DrawdownGuardV1 Module** (`src/grinder/risk/drawdown_guard_v1.py`):
    - Tracks DD at portfolio level AND per-symbol level
    - GuardState: `NORMAL` | `DRAWDOWN`
    - OrderIntent: `INCREASE_RISK` | `REDUCE_RISK` | `CANCEL`
    - Transition NORMAL → DRAWDOWN when:
      - Portfolio DD >= portfolio_dd_limit, OR
      - Symbol loss >= symbol_dd_budget
  - **Intent Classification (v1 rules):**
    - `INCREASE_RISK`: New positions, grid entries, orders that increase exposure
    - `REDUCE_RISK`: Closes, exits, flatten intents that decrease exposure
    - `CANCEL`: Cancellation of existing orders
  - **Allow Decision Logic:**
    | State | Intent | Allowed | Reason |
    |-------|--------|---------|--------|
    | NORMAL | INCREASE_RISK | ✓ | NORMAL_STATE |
    | NORMAL | REDUCE_RISK | ✓ | NORMAL_STATE |
    | NORMAL | CANCEL | ✓ | CANCEL_ALWAYS_ALLOWED |
    | DRAWDOWN | INCREASE_RISK | ✗ | DD_PORTFOLIO_BREACH or DD_SYMBOL_BREACH |
    | DRAWDOWN | REDUCE_RISK | ✓ | REDUCE_RISK_ALLOWED |
    | DRAWDOWN | CANCEL | ✓ | CANCEL_ALWAYS_ALLOWED |
  - **No Auto-Recovery:**
    - Once in DRAWDOWN, stays there until explicit `reset()` call
    - Prevents flapping and ensures deterministic replay behavior
    - Reset intended for new session/day start only
  - **Global DRAWDOWN State (P2-04a):**
    - Guard state is GLOBAL, not per-symbol
    - When ANY symbol breaches its DD budget → entire guard transitions to DRAWDOWN
    - In DRAWDOWN, INCREASE_RISK is blocked for ALL symbols, not just the breached one
    - Rationale: Portfolio risk is correlated; if one symbol is losing, reducing exposure everywhere is prudent
    - Example: BTCUSDT breaches $1000 budget → ETHUSDT INCREASE_RISK also blocked with reason `DD_SYMBOL_BREACH`
  - **Reduce-Only Semantics (P2-04b):**
    - In DRAWDOWN, `REDUCE_RISK` intent is always allowed
    - Reduce-only action: `PaperEngine.flatten_position(symbol, price, ts)`
    - Closes entire position at given price (no partial reduce in v0)
    - If LONG → generates SELL fill; if SHORT → generates BUY fill
    - Deterministic: same inputs → same fill output
    - Guards checked: `guard.allow(REDUCE_RISK, symbol)` → always allowed in any state
    - Use case: emergency position exit when DD limit breached
  - **Reset Hook (P2-04c):**
    - `PaperEngine.reset_dd_guard_v1()` — returns guard to NORMAL state
    - Use case: new session/day start
    - Transition: DRAWDOWN → NORMAL (or NORMAL → NORMAL, safe no-op)
    - After reset: `INCREASE_RISK` is allowed again
    - Also called by `PaperEngine.reset()` (general reset)
    - Returns: `{reset: bool, state_before: str, state_after: str, reason: str}`
  - **Reason Codes (stable, low-cardinality):**
    - `NORMAL_STATE`: All intents allowed in normal operation
    - `REDUCE_RISK_ALLOWED`: Reduce-only allowed in DRAWDOWN
    - `CANCEL_ALWAYS_ALLOWED`: Cancels always permitted
    - `DD_PORTFOLIO_BREACH`: Blocked due to portfolio DD limit
    - `DD_SYMBOL_BREACH`: Blocked due to symbol DD limit
  - **Wiring Point:** `src/grinder/paper/engine.py` Step 3.5 (lines 717-767)
    - Guard sits BETWEEN gating check AND execution (BEFORE `ExecutionEngine.evaluate()`)
    - Location: After `if not gating_result.allowed: return ...` block
    - Flow: gating → **DD guard check** → execution
    - On each snapshot:
      1. Compute current equity from ledger (initial_capital + realized + unrealized)
      2. Compute symbol losses (negative total PnL → positive loss value)
      3. Call `guard.update(equity_start, equity_current, symbol_losses)`
      4. If plan has entry levels, call `guard.allow(OrderIntent.INCREASE_RISK, symbol)`
      5. If blocked, return early with `blocked_by_dd_guard_v1=True`
    - Enabled via `dd_guard_v1_enabled=True` in PaperEngine constructor
  - **Loss Calculation (v1):**
    - Uses realized PnL (simpler, deterministic)
    - Portfolio DD = (equity_start - equity_current) / equity_start
    - Symbol loss = absolute USD loss (positive value)
- **Configuration:**
  ```python
  guard = DrawdownGuardV1(DrawdownGuardV1Config(
      portfolio_dd_limit=Decimal("0.20"),  # 20%
      symbol_dd_budgets={
          "BTCUSDT": Decimal("1000"),
          "ETHUSDT": Decimal("500"),
      },
  ))

  # On each tick
  guard.update(
      equity_current=equity,
      equity_start=session_start_equity,
      symbol_losses=ledger.get_realized_losses(),
  )

  # Before placing order
  decision = guard.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")
  if not decision.allowed:
      logger.warning("Blocked by DD guard: %s", decision.reason.value)
      return  # Skip order
  ```
- **Consequences:**
  - Risk limits are enforced deterministically at runtime
  - Policy can't accidentally increase risk beyond limits
  - Reduce-only orders always pass (allows position unwinding)
  - Unit tests: 39 tests in `test_drawdown_guard_v1.py` (34 guard + 5 wiring)
  - All 5 invariants from v0 DrawdownGuard preserved
  - Wiring integration tests verify blocking behavior in PaperEngine
- **Out of Scope (v1):**
  - Auto-recovery / hysteresis / cooldown
  - Partial degradation states (WARN, DEGRADED)
  - Mark-to-market PnL (uses realized only)
  - Kill-switch integration (separate from DD guard)
- **Alternatives:**
  - Auto-recovery with cooldown — rejected; non-deterministic, risk of flapping
  - Single state (just block all in DD) — rejected; need reduce-only for position exit
  - Probabilistic blocking — rejected; breaks determinism

---

## ADR-034: Paper Realism v0.1 — Tick-Delay Fills (LC-03)

- **Status:** Accepted
- **Context:**
  - Paper trading previously used instant fills (v0) or immediate crossing fills (v1)
  - Real exchanges have latency: orders stay OPEN before being matched
  - Instant fills make backtesting overly optimistic (no adverse selection modeling)
  - Need deterministic fill model that's more realistic without randomness
- **Decision:**
  - Implement **tick-delay fill model** in `PaperEngine`
  - New parameter: `fill_after_ticks: int = 0` (0 = current behavior, 1+ = delay)
  - Order lifecycle: `PLACE → OPEN → (N ticks) → FILLED` (if price crosses)
  - Cancel semantics: Cancel OPEN order before fill-eligible prevents fill
  - Replace semantics: Replace OPEN order = cancel + place new (both get new tick count)
- **Fill Rule (tick-count model):**
  ```
  Order placed at tick T fills at tick T + N (where N = fill_after_ticks)
  IF price crossing condition is met:
    - BUY fills if mid_price <= limit_price
    - SELL fills if mid_price >= limit_price
  ```
- **Order State Tracking:**
  - Added `placed_tick: int` to `OrderRecord` (tracks tick when placed)
  - Each symbol has its own `tick_counter` in `ExecutionState`
  - Orders in `FILLED` state are not re-checked for fills
  - Orders in `CANCELLED` state are skipped
- **Implementation:**
  - `check_pending_fills()` in `fills.py` — checks existing OPEN orders
  - `_update_orders_to_filled()` in `engine.py` — transitions order state
  - `_snapshot_counter` in `PaperEngine` — global counter (for debugging)
- **Determinism:**
  - Same inputs → same fills (no randomness, no wall-clock)
  - Fill order is deterministic (sorted by order_id)
  - Tick count is discrete (integer), not time-based
- **Backward Compatibility:**
  - `fill_after_ticks=0` preserves existing behavior (instant/crossing)
  - Default is 0 to avoid breaking existing fixtures/digests
  - `placed_tick` defaults to 0 in `OrderRecord.from_dict()` for old data
- **Configuration Example:**
  ```python
  engine = PaperEngine(
      fill_after_ticks=1,  # Fill on next tick after placement
      fill_mode="crossing",  # Price must still cross for fill
  )
  ```
- **Consequences:**
  - More realistic simulation (orders don't fill instantly)
  - Cancel-before-fill is now possible (order management testing)
  - Grid reconciliation works correctly with OPEN orders
  - 18 unit tests in `test_paper_realism.py`
  - Determinism preserved (replay produces identical results)
- **Out of Scope (v0.1):**
  - Partial fills
  - Slippage / fees
  - Order book depth (L2) simulation
  - Probabilistic models (random delays)
  - Time-based delays (uses tick count, not milliseconds)
- **Future Extensions:**
  - v0.2: Price-sensitive delay (further orders = longer delay)
  - v0.3: Partial fills based on available liquidity
  - v1.0: L2-based fill simulation

## ADR-035: BinanceExchangePort v0.1 — Live Write-Path (LC-04)
- **Date:** 2026-02-04
- **Status:** Accepted
- **Context:**
  - Paper trading uses NoOpExchangePort (no real exchange calls)
  - Need real Binance Spot API integration for live trading
  - Must be impossible to accidentally trade real money (safety by default)
  - Must integrate with existing H2/H3/H4/H5 hardening (retries, idempotency, circuit breaker, metrics)
  - DoD v2 requires: testnet only in v0.1, symbol whitelist, injectable HTTP client for testing
- **Decision:**
  - **BinanceExchangePort** implements `ExchangePort` protocol (`src/grinder/execution/binance_port.py`)
  - **SafeMode enforcement:**
    - `SafeMode.READ_ONLY` (default): blocks all write operations → 0 risk
    - `SafeMode.PAPER`: blocks write operations (use PaperExecutionAdapter instead)
    - `SafeMode.LIVE_TRADE`: explicit opt-in required for real API calls
    - Mode validation happens BEFORE any HTTP call
  - **Mainnet forbidden in v0.1:**
    - Config rejects any URL containing `api.binance.com`
    - Default URL: `https://testnet.binance.vision` (safe by design)
    - Raises `ConnectorNonRetryableError` if mainnet URL detected
  - **Injectable HTTP client:**
    - `HttpClient` protocol for HTTP operations
    - `NoopHttpClient` for dry-run testing (0 real HTTP calls)
    - Allows proving dry-run mode makes no network I/O
  - **Symbol whitelist:**
    - `symbol_whitelist` config parameter
    - Blocks trades for symbols not in list (empty = all allowed)
    - Raises `ConnectorNonRetryableError` if symbol blocked
  - **Error mapping:**
    - 5xx → `ConnectorTransientError` (retryable)
    - 429 → `ConnectorTransientError` (rate limit, retryable)
    - 418 → `ConnectorNonRetryableError` (IP ban, not retryable)
    - 4xx → `ConnectorNonRetryableError` (client error, not retryable)
    - Binance -1000 series → `ConnectorTransientError` (WAF, overload)
    - Binance -1100/-2000 series → `ConnectorNonRetryableError` (validation)
  - **H3 idempotency via IdempotentExchangePort wrapper:**
    - Wrap `BinanceExchangePort` with `IdempotentExchangePort` for production use
    - Replace = cancel + place with shared idempotency key
    - Safe under retries: same request key → 1 side-effect
  - **H4 circuit breaker via IdempotentExchangePort:**
    - Optional `breaker` parameter in wrapper
    - Fast-fail when upstream degraded (OPEN state)
- **Implementation:**
  - `BinanceExchangePort.place_order()`: POST /api/v3/order
  - `BinanceExchangePort.cancel_order()`: DELETE /api/v3/order
  - `BinanceExchangePort.replace_order()`: cancel + place (with contextlib.suppress)
  - `BinanceExchangePort.fetch_open_orders()`: GET /api/v3/openOrders
  - Order ID format: `grinder_{symbol}_{level_id}_{ts}_{counter}`
  - HMAC-SHA256 signing for authenticated endpoints
- **Testing:**
  - 28 unit tests in `test_binance_port.py`
  - Dry-run tests prove `NoopHttpClient` makes 0 HTTP calls
  - SafeMode tests prove READ_ONLY/PAPER block writes
  - Mainnet tests prove api.binance.com is rejected
  - Whitelist tests prove unlisted symbols are blocked
  - Error mapping tests prove correct classification
  - Idempotency integration tests prove caching works
  - Circuit breaker integration tests prove fast-fail works
- **Consequences:**
  - Live trading possible with explicit `SafeMode.LIVE_TRADE` opt-in
  - Cannot accidentally trade on mainnet (forbidden in v0.1)
  - Injectable HTTP client enables deterministic testing
  - Integrates cleanly with existing H2/H3/H4/H5 stack
- **SafeMode vs KillSwitch (Clarification):**
  - **SafeMode** is a static, per-run configuration that controls whether the port CAN make HTTP calls
    - Set at construction time, doesn't change during a run
    - `READ_ONLY` → 0 writes allowed (safe by default)
    - `LIVE_TRADE` → writes allowed (explicit opt-in)
  - **KillSwitch** is a dynamic, runtime latch that blocks trading when triggered (ADR-013)
    - Checked at orchestrator level (PaperEngine), NOT built into BinanceExchangePort
    - Can trip mid-run (e.g., drawdown exceeded)
    - Once triggered, stays triggered until explicit reset
  - **Difference from RISK_SPEC.md "arming":**
    - RISK_SPEC.md describes KillSwitch with `armed` flag (must arm before trigger works)
    - Current implementation is simpler: `trip()` always works, `is_triggered` is the guard
    - SafeMode does NOT replace arming - they're orthogonal:
      - SafeMode = "can the port make API calls at all?"
      - KillSwitch = "should we block trading right now?"
  - **Integration pattern:**
    - Orchestrator checks `kill_switch.is_triggered` BEFORE calling `port.place_order()`
    - If triggered, skip the call (0 HTTP calls, no SafeMode check needed)
    - If not triggered, SafeMode validation happens inside the port
- **dry_run mode:**
  - `BinanceExchangePortConfig.dry_run=True` returns synthetic results WITHOUT calling http_client
  - Distinct from NoopHttpClient (which still receives calls for recording)
  - `dry_run=True` guarantees 0 `http_client.request()` calls
- **Out of Scope (v0.1):**
  - WebSocket streaming (uses HTTP REST only)
  - Mainnet support (testnet only)
  - Futures/margin trading (spot only)
  - Real AiohttpClient implementation (only protocol defined)
  - Rate limiting (handled by H4 circuit breaker)

## ADR-036 — LiveEngineV0: Live Write-Path Wiring (LC-05)
- **Date:** 2026-02-04
- **Status:** accepted
- **Context:** We have individual components (BinanceExchangePort, IdempotentExchangePort, DrawdownGuardV1, KillSwitch) but no orchestration layer that wires them together for live trading. PaperEngine handles paper mode but shouldn't be polluted with live I/O concerns.
- **Decision:**
  - **New module:** `grinder.live` with `LiveEngineV0` class
  - **Architecture:** Thin wrapper around PaperEngine that forwards execution to real ExchangePort
  - **Arming model (two-layer safety):**
    - `LiveEngineConfig.armed: bool = False` — master switch, blocks ALL writes when False
    - `LiveEngineConfig.mode: SafeMode = READ_ONLY` — secondary check via port
    - Both `armed=True` AND `mode=LIVE_TRADE` required for actual writes
    - Engine arming is checked BEFORE port SafeMode (faster rejection)
  - **Intent classification:**
    - `ActionType.CANCEL` → `OrderIntent.CANCEL` (always allowed)
    - `ActionType.PLACE` → `OrderIntent.INCREASE_RISK` (blocked in DRAWDOWN)
    - `ActionType.REPLACE` → `OrderIntent.INCREASE_RISK` (blocked in DRAWDOWN)
    - `ActionType.NOOP` → `OrderIntent.CANCEL` (safe, skipped)
  - **Kill-switch semantics:**
    - `kill_switch_active=True` → blocks PLACE/REPLACE, allows CANCEL
    - Enables "reduce only" mode to exit positions when triggered
  - **Safety gate ordering (checked in this order):**
    1. Arming check (`armed=False` → blocked)
    2. Mode check (`mode≠LIVE_TRADE` → blocked)
    3. Kill-switch check (if active, blocks INCREASE_RISK)
    4. Symbol whitelist check
    5. DrawdownGuardV1.allow(intent) check
    6. Execute via exchange_port
  - **Hardening chain (H2/H3/H4):**
    - H3: IdempotentExchangePort wraps base port for idempotency
    - H4: CircuitBreaker integrated into IdempotentExchangePort for fast-fail
    - H2: RetryPolicy for transient errors (exponential backoff)
    - Chain: `LiveEngineV0 → IdempotentExchangePort(H3+H4) → BinanceExchangePort`
  - **Error handling:**
    - `ConnectorNonRetryableError` → fail immediately (no retries)
    - `ConnectorTransientError` → retry with backoff
    - `CircuitOpenError` → fail immediately (breaker OPEN)
    - Other `ConnectorError` → check `is_retryable()` to decide
- **Implementation:**
  - `src/grinder/live/config.py`: `LiveEngineConfig` dataclass
  - `src/grinder/live/engine.py`: `LiveEngineV0` class
  - `LiveEngineV0.process_snapshot(snapshot)` → `LiveEngineOutput`
  - `LiveAction` dataclass tracks status, block_reason, attempts
  - `BlockReason` enum for gate-specific rejection codes
- **Testing:**
  - 16 unit tests in `test_live_engine.py`
  - A) Safety/arming (4): armed=False, mode=READ_ONLY, kill-switch, whitelist
  - B) Drawdown guard (3): NORMAL allows, DRAWDOWN blocks INCREASE_RISK, allows CANCEL
  - C) Idempotency+retry (3): duplicate→cached, transient→retry, non-retryable→no-retry
  - D) Circuit breaker (2): trip→reject, half-open→close
- **Consequences:**
  - Live trading possible with explicit `armed=True` + `mode=LIVE_TRADE`
  - By default nothing writes (`armed=False`)
  - DrawdownGuardV1 blocks risk-increasing actions in DRAWDOWN state
  - Idempotency ensures 1 side-effect even under retries
  - Circuit breaker fast-fails when upstream degraded
- **Out of Scope (v0):**
  - Reconciliation via `fetch_open_orders()` (deferred to LC-06)
  - Multi-symbol support (single-symbol focus)
  - Persistent state recovery (deferred)
  - Real E2E testnet testing (requires LC-07 runbook)
  - Engine-level metrics (H5) beyond existing port metrics

## ADR-038 — Testnet Smoke Harness (LC-07)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Need an E2E smoke test for Binance Testnet to verify live trading connectivity and order flow. Must be safe-by-construction: cannot accidentally trade on mainnet or place real orders without explicit opt-in.
- **Decision:**
  - **Safe-by-construction guards:**
    - `--dry-run` by default (no real HTTP calls, simulated place/cancel)
    - Requires `--confirm TESTNET` for real orders
    - Mainnet FORBIDDEN (blocked in BinanceExchangePort)
    - Requires `ARMED=1` + `ALLOW_TESTNET_TRADE=1` env vars for real trades
    - Kill-switch blocks PLACE/REPLACE, allows CANCEL
  - **Script:** `scripts/smoke_live_testnet.py`
    - `RequestsHttpClient`: Real HTTP via requests library
    - `SmokeResult`: Tracks simulated vs real outcomes
    - Clear output: `** SIMULATED - No real HTTP calls made **` in dry-run
    - Order ID prefixed with `SIM_` in dry-run mode
  - **Runbook:** `docs/runbooks/08_SMOKE_TEST_TESTNET.md`
    - Step-by-step procedure for testnet smoke test
    - Failure scenarios and resolution
    - Operator checklist
  - **Kill-switch extension:** `docs/runbooks/04_KILL_SWITCH.md`
    - Added kill-switch behavior table (PLACE blocked, CANCEL allowed)
    - Testnet verification procedure
- **E2E Run Status:**
  - Smoke harness is READY and tested in dry-run mode
  - Real E2E run is OPERATOR-DEPENDENT (requires Binance testnet credentials)
  - Binance testnet may require KYC verification for API key generation
  - Real E2E run NOT executed as part of this PR
- **Verification (dry-run):**
  ```bash
  PYTHONPATH=src python -m scripts.smoke_live_testnet  # dry-run
  PYTHONPATH=src python -m scripts.smoke_live_testnet --kill-switch  # kill-switch test
  ```
- **Consequences:**
  - Operators can verify testnet connectivity when they have credentials
  - Dry-run mode proves script logic works without external dependencies
  - Mainnet protection hardcoded at BinanceExchangePort level
  - Kill-switch behavior documented and testable
- **Out of Scope (v0.1):**
  - Mainnet trading (superseded by ADR-039)
  - Automated CI execution of real testnet orders (no credentials in CI)
  - Fill verification (order is far-from-market, should not fill)

---

## ADR-039 — Mainnet Trade Smoke v0.1 (LC-08b)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Testnet unavailable due to KYC requirements. Mainnet trading is available with a dedicated test budget. Need safe-by-construction guards to enable mainnet smoke testing without risk of accidental large trades.
- **Decision:**
  - **Multi-layer safety guards:**
    1. `allow_mainnet=False` by default (must explicitly opt-in in config)
    2. `ALLOW_MAINNET_TRADE=1` env var required (prevents accidental mainnet)
    3. `symbol_whitelist` REQUIRED for mainnet (no wildcard trading)
    4. `max_notional_per_order` REQUIRED for mainnet (caps each order notional)
    5. `max_orders_per_run=1` default (single order per script run)
    6. `max_open_orders=1` default (single concurrent order)
    7. `ARMED=1` env var required (same as testnet)
  - **BinanceExchangePort changes:**
    - Conditional mainnet allow (was: unconditional block)
    - `is_mainnet()` method for URL detection
    - `_validate_notional()` enforces notional limit
    - `_validate_order_count()` enforces order count limit
    - `reset()` clears order count for new runs
  - **Smoke script changes:**
    - `--confirm MAINNET_TRADE` flag for mainnet mode
    - `--max-notional` argument (default: $50)
    - Clear banner: `*** LIVE MAINNET MODE ***`
    - Output shows `is_mainnet: True`, `base_url: api.binance.com`
  - **Guard validation order (fail-fast):**
    1. Config validation (allow_mainnet, env var, whitelist, max_notional)
    2. Per-order validation (notional limit, order count)
  - **Runbook:** `docs/runbooks/09_MAINNET_TRADE_SMOKE.md`
    - Prerequisites (credentials, test budget, env vars)
    - Step-by-step procedure
    - Verification checklist
    - Emergency procedures
- **Test coverage:**
  - `TestMainnetGuards` class (7 tests) in `tests/unit/test_binance_port.py`
  - Tests: env var required, whitelist required, max_notional required, limit enforcement
- **Verification:**
  ```bash
  # Dry-run (default)
  PYTHONPATH=src python3 -m scripts.smoke_live_testnet

  # Real mainnet order (budgeted)
  BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \
      PYTHONPATH=src python3 -m scripts.smoke_live_testnet --confirm MAINNET_TRADE
  ```
- **Consequences:**
  - Mainnet smoke testing enabled with strict guardrails
  - 7+ layers of safety prevent accidental large trades
  - Order count limits prevent runaway scripts
  - Notional limits cap worst-case per-order loss
  - All guards verified by unit tests
- **Out of Scope (v0.1):**
  - Multi-symbol mainnet trading (single symbol per run)
  - Automated mainnet E2E in CI (manual operator runs only)
  - Fill verification (order placed far from market)

## ADR-040 — Futures USDT-M Mainnet Smoke v0.1 (LC-08b-F)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Target execution venue is Binance Futures USDT-M (`fapi.binance.com`), not Spot. ADR-039 implemented Spot mainnet smoke (`api.binance.com`), but this does not validate the actual execution path. Need futures-specific port and smoke harness.
- **Decision:**
  - **New module:** `src/grinder/execution/binance_futures_port.py`
    - `BinanceFuturesPortConfig`: Configuration with futures-specific guards
    - `BinanceFuturesPort`: Exchange port implementing futures API
    - Base URL: `https://fapi.binance.com` (mainnet), `https://testnet.binancefuture.com` (testnet)
  - **Futures-specific safety guards:**
    - Same 7 layers as ADR-039 (allow_mainnet, env var, whitelist, notional, order count)
    - `target_leverage`: Enforce leverage setting (default: 1x = no leverage)
    - Position mode logging (hedge vs one-way)
    - Margin type logging (isolated vs cross)
  - **Position cleanup on fill:**
    - After order placement + cancel, check for residual position
    - If position exists → close with market order (`reduceOnly=True`)
    - Final verification: position should be 0
  - **Smoke script:** `scripts/smoke_futures_mainnet.py`
    - `--confirm FUTURES_MAINNET_TRADE` flag for live mode
    - 7-step procedure: account info → leverage → position check → order → cancel → cleanup → verify
    - Clear output: leverage, position mode, order details, cleanup status
  - **API endpoints:**
    - `POST /fapi/v1/order` (place order)
    - `DELETE /fapi/v1/order` (cancel order)
    - `POST /fapi/v1/leverage` (set leverage)
    - `GET /fapi/v2/positionRisk` (check position)
    - `GET /fapi/v2/account` (account info)
    - `GET /fapi/v1/positionSide/dual` (position mode)
- **Test coverage:**
  - `tests/unit/test_binance_futures_port.py` (30 tests)
  - Dry-run tests (0 HTTP calls)
  - SafeMode enforcement tests
  - Mainnet guard tests
  - Notional/order count limit tests
  - Leverage validation tests
- **Consequences:**
  - Futures USDT-M execution path now validated
  - Same guardrails as Spot (ADR-039)
  - Leverage enforced at 1x by default (no margin amplification)
  - Position cleanup ensures no residual exposure
- **Runbook:** `docs/runbooks/10_FUTURES_MAINNET_TRADE_SMOKE.md`
- **Spot vs Futures:**
  - ADR-039 (LC-08b): Spot mainnet smoke → validates Spot path
  - ADR-040 (LC-08b-F): Futures mainnet smoke → validates Futures USDT-M path
  - Target production venue: Futures USDT-M

---

## ADR-037 — LiveFeed: Live Read-Path Pipeline (LC-06)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** LiveEngineV0 (ADR-036) handles write-path (order execution), but we need a read-only data pipeline to convert Binance WebSocket bookTicker stream into FeatureSnapshot objects for the policy layer. This must be strictly read-only with ZERO imports from execution module.
- **Decision:**
  - **New modules:**
    - `grinder.connectors.binance_ws`: WebSocket client for Binance bookTicker stream
    - `grinder.live.types`: LiveFeaturesUpdate, WsMessage, BookTickerData, LiveFeedStats
    - `grinder.live.feed`: LiveFeed pipeline orchestrator
  - **Architecture:** DataConnector → Snapshot → FeatureEngine → LiveFeaturesUpdate
  - **Hard read-only constraint:**
    - `feed.py` MUST NOT import from `grinder.execution.*`
    - Enforced by `test_feed_py_has_no_execution_imports` using AST parsing
    - Violation = CI failure
  - **BinanceWsConnector:**
    - Implements `DataConnector` ABC with `iter_snapshots()` async iterator
    - Parses bookTicker JSON → Snapshot objects
    - Idempotency via `last_seen_ts` tracking (skips old/duplicate)
    - Auto-reconnect with exponential backoff
    - Testable via `WsTransport` ABC injection
  - **FakeWsTransport:**
    - Pre-loaded messages queue for testing
    - Simulated delays (`delay_ms`)
    - Error injection (`error_after=N`)
    - Injectable clock for deterministic timestamps
  - **LiveFeed pipeline:**
    - Receives Snapshots from DataConnector
    - Filters by configured symbols
    - Feeds through FeatureEngine (BarBuilder → indicators)
    - Yields LiveFeaturesUpdate with computed features
    - Tracks stats (ticks, bars, errors, latency)
  - **LiveFeaturesUpdate:**
    - `ts`: Snapshot timestamp
    - `symbol`: Trading symbol
    - `features`: FeatureSnapshot from engine
    - `bar_completed`: Whether a new bar was completed
    - `bars_available`: Count of completed bars
    - `is_warmed_up`: Whether enough bars for full feature computation
    - `latency_ms`: Processing latency
  - **Configuration:**
    - `LiveFeedConfig`: symbols filter, feature_config, warmup_bars
    - `BinanceWsConfig`: symbols, use_testnet, timeout, retry
- **Implementation:**
  - `src/grinder/connectors/binance_ws.py`: WsTransport, BinanceWsConnector
  - `src/grinder/live/types.py`: LiveFeaturesUpdate, WsMessage, BookTickerData
  - `src/grinder/live/feed.py`: LiveFeed, LiveFeedConfig, LiveFeedRunner
  - `tests/unit/test_live_feed.py`: 21 tests
  - `tests/fixtures/ws/bookticker_btcusdt.jsonl`: Golden fixture
- **Testing:**
  - **P0 Hard-block tests (2):** AST check for 0 execution imports in feed.py and types.py
  - **FakeWsTransport tests (3):** Message ordering, not-connected error, error injection
  - **BinanceWsConnector tests (4):** Connect/subscribe, yields snapshots, skip subscription response, idempotency
  - **LiveFeed tests (7):** Process snapshot, bars tracking, symbol filtering, warmup detection, stats, reset
  - **Determinism tests (2):** Same input → same output, golden fixture SHA256 match
  - **LiveFeaturesUpdate tests (1):** to_dict serialization
- **Consequences:**
  - Live data pipeline ready for integration with LiveEngineV0
  - Strictly read-only (no accidental trades from data plane)
  - Deterministic testing with fake WS transport
  - Golden fixtures enable regression detection
  - FeatureEngine produces features for policy evaluation
- **Out of Scope (v0):**
  - Real WebSocket connection to mainnet (testnet only)
  - Multi-symbol concurrent streaming (single pipeline)
  - WebSocket heartbeat/ping handling (delegated to websockets library)
  - Persistence of WS messages for replay (deferred)

---

## ADR-041 — Futures User-Data Stream v0.1 (LC-09a)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Reconciliation between expected order state (from execution) and actual state (from exchange) requires real-time user-data stream. Binance Futures USDT-M uses a listenKey-based WebSocket for ORDER_TRADE_UPDATE and ACCOUNT_UPDATE events. Need event normalization, listenKey lifecycle management, and deterministic testing.
- **Decision:**
  - **New types** (`src/grinder/execution/futures_events.py`):
    - `FuturesOrderEvent`: Normalized order update (ts, symbol, order_id, client_order_id, side, status, price, qty, executed_qty, avg_price)
    - `FuturesPositionEvent`: Normalized position update (ts, symbol, position_amt, entry_price, unrealized_pnl)
    - `UserDataEventType`: Enum for ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, UNKNOWN
    - `UserDataEvent`: Tagged union wrapper for event dispatch
    - `BINANCE_STATUS_MAP`: Binance status → OrderState mapping (NEW→OPEN, CANCELED→CANCELLED, etc.)
  - **ListenKey lifecycle** (`src/grinder/connectors/binance_user_data_ws.py`):
    - `ListenKeyConfig`: API base URL, API key, timeout
    - `ListenKeyManager`: HTTP operations for listenKey (POST create, PUT keepalive, DELETE close)
    - 401 → `ConnectorNonRetryableError` (invalid API key)
    - 5xx → `ConnectorTransientError` (retryable)
  - **WebSocket connector** (`FuturesUserDataWsConnector`):
    - Lifecycle: connect (create listenKey → WS connect → start keepalive), close (cancel keepalive → WS close → delete listenKey)
    - `iter_events()`: AsyncIterator yielding `UserDataEvent`
    - Auto-keepalive: PUT every 30 seconds (configurable)
    - Auto-reconnect: exponential backoff on transient errors
    - Unknown events: yield as UNKNOWN with raw_data (don't crash)
    - listenKeyExpired: log warning, trigger reconnect
  - **Testing infrastructure**:
    - `FakeListenKeyManager`: Injectable mock for listenKey operations
    - Reuses `FakeWsTransport` from binance_ws.py for WS testing
    - Injectable clock for keepalive timing tests
  - **Fixtures** (`tests/fixtures/user_data/`):
    - `order_lifecycle.jsonl`: NEW → PARTIALLY_FILLED → FILLED
    - `position_lifecycle.jsonl`: 0 → position → 0
    - Golden tests verify deterministic normalization (same input → same output)
- **API message format (Binance reference)**:
  - ORDER_TRADE_UPDATE: `o.s`→symbol, `o.c`→client_order_id, `o.X`→status, `o.i`→order_id, etc.
  - ACCOUNT_UPDATE: `a.P[]`→positions array, `a.P[].s`→symbol, `a.P[].pa`→position_amt, etc.
- **Test coverage:**
  - `tests/unit/test_futures_events.py` (41 tests): serialization, parsing, lifecycle golden tests
  - `tests/unit/test_listen_key_manager.py` (17 tests): HTTP operations, error handling
  - `tests/unit/test_user_data_ws.py` (21 tests): connection, events, stats, FakeListenKeyManager
- **Consequences:**
  - User-data stream infrastructure ready for reconciliation integration (LC-09b)
  - Event normalization provides stable types for strategy logic
  - Deterministic testing without network dependencies
  - listenKey lifecycle managed automatically (no manual keepalive needed)
- **Out of Scope (LC-09b):**
  - Reconciliation logic (comparing expected vs observed state)
  - REST snapshot fallback
  - Active actions (cancel-all, flatten on mismatch)
  - Metrics/counters for stream health

---

## ADR-042 — Passive Reconciliation v0.1 (LC-09b)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Need to detect mismatches between expected state (what we sent to exchange) and observed state (from user-data stream + REST snapshots) for Binance Futures USDT-M. v0.1 is **passive only**: logs + metrics + action plan text. No actual remediation actions.
- **Decision:**
  - **New types** (`src/grinder/reconcile/types.py`):
    - `ExpectedOrder`: Frozen dataclass for order we expect on exchange (client_order_id, symbol, side, order_type, price, orig_qty, ts_created, expected_status)
    - `ExpectedPosition`: Frozen dataclass for expected position (symbol, expected_position_amt, ts_updated)
    - `ObservedOrder`: Frozen dataclass for order seen via stream/REST (includes order_id, executed_qty, avg_price, source)
    - `ObservedPosition`: Frozen dataclass for observed position (position_amt, entry_price, unrealized_pnl, source)
    - `MismatchType`: Enum with 4 stable values (metric labels):
      - `ORDER_MISSING_ON_EXCHANGE`: Expected OPEN order not found after grace period
      - `ORDER_EXISTS_UNEXPECTED`: Order on exchange (grinder_ prefix) not in expected state
      - `ORDER_STATUS_DIVERGENCE`: Expected vs observed status differs
      - `POSITION_NONZERO_UNEXPECTED`: Position != 0 when expected = 0
    - `Mismatch`: Frozen dataclass for detected mismatch (type, symbol, client_order_id, expected, observed, ts_detected, action_plan)
  - **Configuration** (`src/grinder/reconcile/config.py`):
    - `ReconcileConfig`: order_grace_period_ms (5s), snapshot_interval_sec (60s), expected_max_orders (200), expected_ttl_ms (24h), symbol_filter, enabled
  - **State stores**:
    - `ExpectedStateStore` (`src/grinder/reconcile/expected_state.py`):
      - Ring buffer (max_orders=200) with OrderedDict for FIFO eviction
      - TTL eviction (24h) - terminal orders evicted first
      - Methods: record_order, mark_filled, mark_cancelled, get_active_orders, get_all_orders
      - Injectable clock for deterministic testing
    - `ObservedStateStore` (`src/grinder/reconcile/observed_state.py`):
      - Updated from FuturesOrderEvent/FuturesPositionEvent (stream) and REST snapshots
      - Methods: update_from_order_event, update_from_position_event, update_from_rest_orders, update_from_rest_positions
      - Tracks last_snapshot_ts for staleness detection
  - **Reconciliation engine** (`src/grinder/reconcile/engine.py`):
    - `ReconcileEngine`: Compares expected vs observed state
    - `reconcile()` returns `list[Mismatch]`, logs warnings (RECONCILE_MISMATCH), updates metrics
    - Grace period: ORDER_MISSING only fires after order_grace_period_ms (prevents false positives during network lag)
    - Only detects grinder_ prefixed orders (ignores third-party orders)
    - **Passive only**: action_plan is text describing what v1.0 *would* do
  - **Metrics** (`src/grinder/reconcile/metrics.py`):
    - `grinder_reconcile_mismatch_total{type="..."}`: Counter by mismatch type
    - `grinder_reconcile_last_snapshot_age_seconds`: Gauge for REST snapshot staleness
    - `grinder_reconcile_runs_total`: Counter for reconcile runs
    - Thread-safe via GIL, Prometheus text export via `to_prometheus_lines()`
    - Global singleton: `get_reconcile_metrics()`, `reset_reconcile_metrics()`
  - **Snapshot client** (`src/grinder/reconcile/snapshot_client.py`):
    - `SnapshotClient`: Periodic REST polling of /fapi/v1/openOrders and /fapi/v2/positionRisk
    - Retry with exponential backoff on 429/5xx (max_retries=3, base_delay=1s, max_delay=10s)
    - Injectable HttpClient for testing
    - `fetch_snapshot()` updates ObservedStateStore
    - `should_fetch()` checks if interval elapsed
- **Test coverage:**
  - `tests/unit/test_reconcile_types.py` (22 tests): serialization roundtrips, MismatchType contract
  - `tests/unit/test_expected_state.py` (16 tests): ring buffer, TTL eviction, mark_filled/cancelled
  - `tests/unit/test_observed_state.py` (15 tests): stream/REST updates, symbol filtering
  - `tests/unit/test_reconcile_engine.py` (13 tests): all 4 mismatch types, grace period, metrics
  - `tests/unit/test_snapshot_client.py` (16 tests): retry logic, backoff, fetch_snapshot
  - Total: 82 tests, all passing
- **Fixtures** (`tests/fixtures/reconcile/`):
  - `expected_orders.jsonl`: Sample expected orders
  - `rest_open_orders.json`: GET /openOrders response
  - `rest_position_risk.json`: GET /positionRisk response
  - `mismatch_scenarios.jsonl`: Test scenarios for golden tests
- **Consequences:**
  - Passive reconciliation infrastructure ready for integration
  - Mismatch detection available via ReconcileEngine.reconcile()
  - Metrics enable alerting on reconciliation issues
  - Memory bounded by ring buffer + TTL eviction
  - Deterministic testing with injectable clocks
- **Out of Scope (LC-10):**
  - Automatic remediation actions (cancel-all, flatten) → implemented in ADR-043
  - `RECONCILE_ACTION=cancel_all` execution → implemented in ADR-043
  - Multi-symbol reconcile optimization
  - HA leader election for reconcile loop
  - Integration with LiveEngineV0 event loop

---

## ADR-043 — Active Remediation v0.1 (LC-10)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Passive reconciliation (ADR-042) detects mismatches but takes no action. Need active remediation to cancel unexpected orders and flatten unexpected positions, but with strict safety gates to prevent accidental execution.
- **Decision:**
  - **Actions:**
    - `cancel_all`: Cancel unexpected grinder_ prefixed orders
    - `flatten`: Close unexpected positions with reduceOnly market orders
  - **9 Safety Gates (ALL must pass for real execution):**
    | # | Gate | Config/Env | Default |
    |---|------|------------|---------|
    | 1 | action != none | `action` | `none` |
    | 2 | dry_run == False | `dry_run` | `True` |
    | 3 | allow_active_remediation | `allow_active_remediation` | `False` |
    | 4 | armed == True | passed from LiveEngine | `False` |
    | 5 | ALLOW_MAINNET_TRADE=1 | env var | not set |
    | 6 | cooldown elapsed | `cooldown_seconds` | 60s |
    | 7 | symbol in whitelist | `symbol_whitelist` | required |
    | 8 | grinder_ prefix (cancel) | hardcoded | required |
    | 9 | notional <= limit (flatten) | `max_flatten_notional_usdt` | 500 |
  - **Additional limits:**
    - `max_orders_per_action=10`: Max cancels per reconcile run
    - `max_symbols_per_action=3`: Max symbols per reconcile run
    - `require_whitelist=True`: Require non-empty symbol whitelist
  - **Kill-switch semantics:** Remediation ALLOWED under kill-switch (reduces risk exposure)
  - **Default behavior:** dry-run only — plans but doesn't execute
  - **grinder_ prefix:** Required for cancel operations (protects manual orders placed outside grinder)
  - **Notional cap:** Required for flatten operations (limits exposure per remediation)
  - **New types** (`src/grinder/reconcile/remediation.py`):
    - `RemediationBlockReason`: Enum with 13 stable values for why remediation was blocked
    - `RemediationStatus`: Enum (PLANNED, EXECUTED, BLOCKED, FAILED)
    - `RemediationResult`: Frozen dataclass for remediation outcome
    - `RemediationExecutor`: Class implementing safety gates and remediation logic
  - **New metrics:**
    - `grinder_reconcile_action_planned_total{action}`: Counter for dry-run plans
    - `grinder_reconcile_action_executed_total{action}`: Counter for real executions
    - `grinder_reconcile_action_blocked_total{reason}`: Counter for blocked actions
  - **New config fields** (`ReconcileConfig`):
    - `action: RemediationAction = NONE`
    - `dry_run: bool = True`
    - `allow_active_remediation: bool = False`
    - `max_orders_per_action: int = 10`
    - `max_symbols_per_action: int = 3`
    - `cooldown_seconds: int = 60`
    - `max_flatten_notional_usdt: Decimal = 500`
    - `require_whitelist: bool = True`
- **Test coverage:**
  - `tests/unit/test_remediation.py` (28 tests):
    - 9 safety gate tests (one per gate)
    - 4 execution tests (cancel, flatten, max_orders, max_symbols)
    - 2 kill-switch tests (allows cancel, allows flatten)
    - 3 metrics tests (planned, executed, blocked counters)
    - Contract tests for enum values and constants
- **Consequences:**
  - Active remediation available with explicit opt-in
  - 9 layers of safety prevent accidental execution
  - grinder_ prefix protects manual orders from accidental cancel
  - Notional cap limits worst-case exposure per flatten
  - Deterministic testing via injectable port
- **Runbook:** `docs/runbooks/12_ACTIVE_REMEDIATION.md`
- **Out of Scope (v0.1):**
  - Smart order replacement/modification
  - Automatic strategy recovery after remediation
  - Multi-venue / COIN-M support
  - HA orchestration for remediation loop
  - ML-based decision making

---

## ADR-044 — Remediation Wiring + Routing Policy (LC-11)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** ADR-043 introduced RemediationExecutor with 9 safety gates, but it operates on individual orders/positions. Need an orchestration layer to wire ReconcileEngine (detects mismatches) → RemediationExecutor (takes actions), with clear routing policy (mismatch type → action mapping).
- **Decision:**
  - **ReconcileRunner** (`src/grinder/reconcile/runner.py`): Orchestrates reconciliation flow:
    1. Call `engine.reconcile()` → `list[Mismatch]`
    2. Route each mismatch via ROUTING_POLICY to action
    3. Execute via `executor.remediate_cancel/flatten()`
    4. Return `ReconcileRunReport` with full audit trail
  - **Routing Policy (SSOT):**
    | Mismatch Type | Action | Notes |
    |---------------|--------|-------|
    | `ORDER_EXISTS_UNEXPECTED` | CANCEL | grinder_ prefix required (Gate 8) |
    | `ORDER_STATUS_DIVERGENCE` | CANCEL | Only if not terminal status |
    | `POSITION_NONZERO_UNEXPECTED` | FLATTEN | Notional cap applies (Gate 9) |
    | `ORDER_MISSING_ON_EXCHANGE` | NO ACTION | v0.1: alert only, no retry |
  - **Terminal statuses (skip cancel):** FILLED, CANCELLED, REJECTED, EXPIRED
  - **Actionable statuses (allow cancel):** OPEN, PARTIALLY_FILLED
  - **Bounded execution:**
    - One action type per run (cancel OR flatten, determined by priority)
    - Respects executor's max_orders_per_action / max_symbols_per_action
  - **Deterministic ordering:**
    - Mismatches are sorted before processing for predictable action-type selection
    - Sort key: priority → symbol → client_order_id
    - Priority (lower = processed first): ORDER_EXISTS=10, ORDER_STATUS_DIVERGENCE=20, ORDER_MISSING=90, POSITION_NONZERO=100
    - Cancel always wins over flatten when both exist (cancel has lower priority numbers)
    - Ensures same result regardless of ReconcileEngine's detection order
  - **Routing constants (frozenset for performance):**
    ```python
    ORDER_MISMATCHES_FOR_CANCEL = frozenset({
        MismatchType.ORDER_EXISTS_UNEXPECTED,
        MismatchType.ORDER_STATUS_DIVERGENCE,
    })
    POSITION_MISMATCHES_FOR_FLATTEN = frozenset({
        MismatchType.POSITION_NONZERO_UNEXPECTED,
    })
    NO_ACTION_MISMATCHES = frozenset({
        MismatchType.ORDER_MISSING_ON_EXCHANGE,
    })
    TERMINAL_STATUSES = frozenset({
        OrderState.FILLED, OrderState.CANCELLED,
        OrderState.REJECTED, OrderState.EXPIRED,
    })
    ACTIONABLE_STATUSES = frozenset({
        OrderState.OPEN, OrderState.PARTIALLY_FILLED,
    })
    ```
  - **ReconcileRunReport:** Frozen dataclass with:
    - `ts_start`, `ts_end`: Run timestamps
    - `mismatches_detected`: Total from engine
    - `cancel_results`, `flatten_results`: Tuples of RemediationResult
    - `skipped_terminal`, `skipped_no_action`: Skip counts
    - Properties: `total_actions`, `executed_count`, `planned_count`, `blocked_count`
  - **New metrics:**
    - `grinder_reconcile_runs_with_mismatch_total`: Counter for runs that detected mismatches
    - `grinder_reconcile_runs_with_remediation_total{action}`: Counter for runs with executed actions
    - `grinder_reconcile_last_remediation_ts_ms`: Gauge for last remediation timestamp
  - **Audit logging:**
    - `RECONCILE_RUN`: Run completion with summary stats
    - `REMEDIATE_SKIP`: Skipped mismatch with reason
- **Test coverage:**
  - `tests/unit/test_reconcile_runner.py` (39 tests):
    - Routing policy constants tests
    - Routing behavior tests (4 mismatch types)
    - One action type per run tests
    - Terminal status skip tests
    - Metrics tests
    - ReconcileRunReport tests
    - Determinism tests (4 tests for priority-based ordering)
    - Edge cases
- **Consequences:**
  - Full wiring: ReconcileEngine → ReconcileRunner → RemediationExecutor
  - Routing policy is explicit SSOT (constants at module level)
  - One action type per run prevents mixed cancel/flatten in same cycle
  - Terminal orders are not attempted to cancel
  - Audit trail via structured logs + metrics
- **Runbook:** `docs/runbooks/13_OPERATOR_CEREMONY.md`
- **Out of Scope:**
  - Multi-action-type per run (cancel AND flatten)
  - Smart retry for ORDER_MISSING_ON_EXCHANGE
  - Integration with LiveEngineV0 event loop (separate task)

---

## ADR-045 — Configurable Order Identity (LC-12)
- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** The hardcoded `grinder_` prefix in multiple places made it impossible to:
  1. Run multiple strategy instances with isolated order ownership
  2. Identify which strategy placed an order
  3. Selectively remediate orders by strategy allowlist
  
  Gate 8 in ADR-043 checked `startswith("grinder_")` but couldn't distinguish between strategies.

- **Decision:**
  - **OrderIdentityConfig** (`src/grinder/reconcile/identity.py`): Central config for order identity:
    ```python
    @dataclass
    class OrderIdentityConfig:
        prefix: str = "grinder_"           # Order ID prefix
        strategy_id: str = "default"       # Strategy identifier
        allowed_strategies: set[str] = {}  # Allowlist for remediation
        require_strategy_allowlist: bool = True
        allow_legacy_format: bool = False  # Env: ALLOW_LEGACY_ORDER_ID=1
        identity_format_version: int = 1
    ```
  - **clientOrderId Format v1:** `{prefix}{strategy_id}_{symbol}_{level_id}_{ts}_{seq}`
    - Example: `grinder_momentum_BTCUSDT_1_1704067200000_1`
  - **Legacy Format:** `grinder_{symbol}_{level_id}_{ts}_{seq}` (no strategy_id)
    - Supported via `allow_legacy_format=True` or `ALLOW_LEGACY_ORDER_ID=1` env var
    - Parsed strategy_id: `__legacy__` (internal marker)
  - **ParsedOrderId:** Frozen dataclass with parsed components:
    - `prefix`, `strategy_id`, `symbol`, `level_id`, `ts`, `seq`, `is_legacy`
  - **Core functions:**
    - `parse_client_order_id(cid) -> ParsedOrderId | None`: Parse any format
    - `is_ours(cid, config) -> bool`: Check ownership via prefix + strategy allowlist
    - `generate_client_order_id(config, symbol, level_id, ts, seq) -> str`: Create v1 format
  - **Singleton pattern:**
    - `get_default_identity_config()`: Returns singleton (lazy init)
    - `set_default_identity_config(config)`: Set at startup
    - `reset_default_identity_config()`: For testing
  - **Integration points updated:**
    - `BinanceFuturesPort.place_order()`: Uses `generate_client_order_id()`
    - `BinanceFuturesPort.place_market_order()`: Uses `generate_client_order_id()`
    - `BinancePort.place_order()`: Uses `generate_client_order_id()`
    - `ReconcileEngine._check_unexpected_orders()`: Uses `is_ours()`
    - `RemediationExecutor.can_execute()` Gate 8: Uses `is_ours()`
  - **Strategy allowlist semantics:**
    - If `allowed_strategies` empty at init → defaults to `{strategy_id}`
    - Legacy orders (`__legacy__`) allowed only if `allow_legacy_format=True`
    - `require_strategy_allowlist=False` → accept any strategy
  - **Backward compatibility:**
    - v1 format includes strategy_id: `grinder_default_BTCUSDT_...` (differs from legacy `grinder_BTCUSDT_...`)
    - Legacy format parsing enabled via env var (`ALLOW_LEGACY_ORDER_ID=1`) or config flag
    - GRINDER_PREFIX constant preserved in remediation.py for backward compat

- **Test coverage:**
  - `tests/unit/test_identity.py` (44 tests):
    - Config validation tests
    - V1 format parsing tests
    - Legacy format parsing tests
    - `is_ours()` allowlist tests
    - Generation tests
    - Security edge cases
    - Singleton pattern tests

- **Consequences:**
  - Orders can be identified by prefix + strategy
  - Multiple strategies can coexist with isolated remediation scope
  - Gate 8 now checks allowlist, not just prefix
  - Legacy orders can be migrated gradually
  - Parser reads both v1 and legacy formats; generation always produces v1

- **Out of Scope (v0.1):**
  - Strategy-specific config loading from file
  - Dynamic allowlist updates at runtime
  - Multi-prefix support (only one prefix per config)
  - Strategy registry with capabilities/permissions

## ADR-046 — Audit JSONL for Reconcile/Remediation (LC-11b)

- **Date:** 2026-02-05
- **Status:** accepted
- **Context:** Reconciliation and remediation runs need a deterministic audit trail for:
  1. Post-mortem analysis of state mismatches
  2. Compliance/regulatory evidence
  3. Debugging and reproducibility

  Requirements:
  - Append-only JSONL format (one event per line)
  - No secrets in output (redaction by default)
  - Bounded file size with rotation
  - Opt-in (disabled by default)
  - Deterministic serialization (sorted keys)

- **Decision:**
  - **AuditConfig** (`src/grinder/reconcile/audit.py`): Configuration for audit logging:
    ```python
    @dataclass
    class AuditConfig:
        enabled: bool = False              # Opt-in
        path: str = "audit/reconcile.jsonl"
        max_bytes: int = 100_000_000       # 100 MB
        max_events_per_file: int = 100_000
        flush_every: int = 1               # Immediate flush
        fsync: bool = False                # No fsync for performance
        redact: bool = True                # Redact secrets
        fail_open: bool = True             # Continue on write error
    ```
  - **AuditEventType** enum:
    - `RECONCILE_RUN`: Summary of reconcile run
    - `REMEDIATE_ATTEMPT`: Individual remediation attempt
    - `REMEDIATE_RESULT`: Result of remediation (planned/executed/blocked/failed)
  - **AuditEvent** frozen dataclass:
    - `ts_ms`: Timestamp in milliseconds
    - `event_type`: Event type enum
    - `run_id`: Unique run identifier (format: `{ts_ms}_{seq}`)
    - `schema_version`: Schema version (default: 1)
    - `mode`: "dry_run" or "live"
    - `action`: Action type (none/cancel_all/flatten)
    - `status`: Event status (for REMEDIATE_* events)
    - `block_reason`: Why blocked (if applicable)
    - `symbols`: Bounded list of symbols (max 10)
    - `mismatch_counts`: Counts by mismatch type
    - `details`: Additional details dict
  - **AuditWriter** class:
    - Append-only writes to JSONL file
    - Creates directories if needed
    - Rotation when `max_bytes` or `max_events_per_file` exceeded
    - Redaction of sensitive fields (api_key, secret, token, etc.)
    - Context manager support
    - Injectable clock and run_id factory for testing
  - **Environment variables:**
    - `GRINDER_AUDIT_ENABLED=1`: Enable audit logging
    - `GRINDER_AUDIT_PATH=/path/to/file.jsonl`: Override audit path
  - **Integration:**
    - `ReconcileRunner` accepts optional `audit_writer` field
    - Writes `RECONCILE_RUN` event at end of each run
    - Collects mismatch counts and symbols during run
  - **Redaction:** Fields containing these patterns are replaced with `[REDACTED]`:
    - `api_key`, `api_secret`, `secret`, `password`, `token`, `signature`, `x-mbx-apikey`, `authorization`
  - **Failure policy:** `fail_open=True` (default) continues on write error, logs warning

- **Test coverage:**
  - `tests/unit/test_audit.py` (33 tests):
    - AuditConfig: defaults, env var overrides
    - AuditEvent: serialization, schema, immutability
    - AuditWriter: append-only, rotation, redaction, context manager
    - Factory functions: event creation
    - Determinism: same inputs → same outputs
    - Runner integration: audit event written, no-audit works

- **Consequences:**
  - Audit trail available for post-mortems when enabled
  - No secrets in audit files (redaction default)
  - Bounded file size prevents disk exhaustion
  - Opt-in: no impact when disabled
  - Deterministic output enables diff-based testing

- **Out of Scope (v0.1):**
  - REMEDIATE_ATTEMPT/RESULT events in runner (only RECONCILE_RUN)
  - Centralized storage (S3, Loki)
  - Encryption/signing of audit files
  - Audit metrics (planned for P1)

## ADR-047 — E2E Reconcile→Remediate Smoke Harness (LC-13)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** Active remediation (LC-10/LC-11) has 9 safety gates but no end-to-end test that exercises the full `detect → route → remediate` flow in a controlled environment. Unit tests validate individual components; we need a higher-level smoke test that:
  1. Validates the complete flow without real HTTP/WS calls
  2. Confirms dry-run mode produces zero port calls
  3. Ensures live mode is protected by 5 explicit gates
  4. Verifies audit integration when enabled

- **Decision:**
  - **New script:** `scripts/smoke_reconcile_e2e.py` — E2E smoke harness for reconciliation + remediation

  - **FakePort pattern:** Duck-typed port that records calls without executing:
    ```python
    @dataclass
    class FakePort:
        calls: list[dict[str, Any]] = field(default_factory=list)

        def cancel_order(self, symbol: str, client_order_id: str) -> dict:
            self.calls.append({"action": "cancel_order", ...})
            return {"status": "CANCELED", ...}

        def market_order(self, symbol: str, side: str, quantity: Decimal) -> dict:
            self.calls.append({"action": "market_order", ...})
            return {"status": "FILLED", ...}
    ```

  - **3 P0 scenarios:**
    - `order`: Unexpected order → CANCEL → validates cancel routing
    - `position`: Unexpected position → FLATTEN → validates flatten routing
    - `mixed`: Both mismatches → priority routing (order wins with CANCEL_ALL action)

  - **Default mode: DRY-RUN**
    - Zero port calls in dry-run mode
    - Plans recorded via `planned_count`
    - Safe to run without any env vars

  - **Live mode gating (5 gates):**
    - `--confirm LIVE_REMEDIATE` — explicit CLI flag
    - `RECONCILE_DRY_RUN=0` — config override
    - `RECONCILE_ALLOW_ACTIVE=1` — allow active remediation
    - `ARMED=1` — executor armed
    - `ALLOW_MAINNET_TRADE=1` — mainnet trade env var

    All 5 must pass; any failure blocks live mode with explicit error message.

  - **Audit integration:**
    - When `GRINDER_AUDIT_ENABLED=1`, writes RECONCILE_RUN events to audit file
    - Default path: `audit/reconcile.jsonl`
    - Events include mismatch counts, action, mode, symbols

  - **Output format:** Clear tabular output for each scenario:
    ```
    --- Scenario: order [PASS] ---
      Mismatches detected:  1
      Expected action:      cancel
      Actual action:        cancel
      Port calls:           0
      Planned count:        1
      Executed count:       0
    ```

- **Usage:**
  ```bash
  # Dry-run (safe, default)
  PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e

  # With audit enabled
  GRINDER_AUDIT_ENABLED=1 PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e

  # Live mode (requires all 5 gates)
  RECONCILE_DRY_RUN=0 RECONCILE_ALLOW_ACTIVE=1 ARMED=1 ALLOW_MAINNET_TRADE=1 \
    PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e --confirm LIVE_REMEDIATE
  ```

- **Consequences:**
  - E2E validation of reconcile→remediate flow without real exchange calls
  - Dry-run safety verified (zero port calls assertion)
  - Live mode protected by explicit 5-gate ceremony
  - Audit integration validated in harness

- **Out of Scope (v0.1):**
  - Real mainnet execution in smoke test
  - COIN-M futures support
  - Concurrent scenario execution
  - Performance benchmarking

## ADR-048 — ReconcileLoop Wiring for LiveEngine (LC-14a)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** ReconcileRunner exists but is not integrated into the live trading loop. We need a periodic background loop that:
  1. Runs ReconcileRunner on a configurable interval
  2. Respects HA role (only runs when ACTIVE)
  3. Operates in detect-only mode by default
  4. Provides thread-safe statistics for monitoring
  5. Follows existing threading patterns (LeaderElector)

- **Decision:**
  - **New module:** `src/grinder/live/reconcile_loop.py` — ReconcileLoop component

  - **Threading pattern (from LeaderElector):**
    - Daemon thread for background execution
    - `threading.Event` for graceful shutdown
    - Interruptible wait via `Event.wait(timeout=interval)`

  - **Configuration (ReconcileLoopConfig):**
    ```python
    @dataclass
    class ReconcileLoopConfig:
        enabled: bool  # env: RECONCILE_ENABLED (default: False)
        interval_ms: int  # env: RECONCILE_INTERVAL_MS (default: 30000)
        require_active_role: bool = True  # Check HA role before running
    ```

  - **Statistics (ReconcileLoopStats):**
    ```python
    @dataclass
    class ReconcileLoopStats:
        runs_total: int
        runs_skipped_role: int
        runs_with_mismatch: int
        runs_with_error: int
        last_run_ts_ms: int
        last_report: ReconcileRunReport | None
    ```

  - **HA integration:**
    - When `require_active_role=True` (default), checks HA role before each run
    - Skips run if role != ACTIVE, increments `runs_skipped_role`
    - Optional dependency: works without HA module installed

  - **Error handling:**
    - Exceptions in runner logged, loop continues
    - `runs_with_error` counter incremented
    - Never crashes the loop thread

  - **Safety guarantees:**
    - Default `enabled=False` — must opt-in via env var
    - Default interval 30s — prevents rapid-fire reconciliation
    - Minimum interval 1000ms — enforced by validation
    - Detect-only by default (via ReconcileConfig)

- **Usage:**
  ```python
  # In LiveEngine initialization
  loop = ReconcileLoop(
      runner=reconcile_runner,
      config=ReconcileLoopConfig(enabled=True, interval_ms=30000),
  )
  loop.start()  # Starts background thread
  # ... later
  loop.stop()   # Graceful shutdown
  ```

  ```bash
  # Enable via environment
  RECONCILE_ENABLED=1 RECONCILE_INTERVAL_MS=30000 python3 -m grinder.live

  # Smoke test
  PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop --duration 15
  ```

- **Smoke test script:** `scripts/smoke_live_reconcile_loop.py`
  - Uses FakePort (no real HTTP calls)
  - Demonstrates loop start/stop lifecycle
  - Verifies detect-only mode (zero port calls)
  - Supports `--inject-mismatch` for mismatch detection

- **Unit tests:** `tests/unit/test_reconcile_loop.py` (18 tests)
  - Config: defaults, env vars, validation
  - Lifecycle: start/stop, idempotent
  - Stats: initial, after runs, thread-safe
  - HA: skip when not ACTIVE, run when ACTIVE
  - Error handling: exceptions don't crash loop

- **Consequences:**
  - ReconcileRunner can now run as periodic background task
  - HA-aware reconciliation prevents split-brain execution
  - Statistics enable monitoring and alerting
  - Thread-safe design prevents race conditions
  - Follows established patterns from LeaderElector

- **Out of Scope (v0.1):**
  - Dynamic interval adjustment
  - Backoff on repeated errors
  - Metrics export (Prometheus)
  - Integration with LiveEngine start/stop

## ADR-049 — Real Sources Wiring for ReconcileLoop (LC-14b)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** ReconcileLoop (LC-14a) uses mock data sources. Production deployment requires:
  1. Real price data from Binance REST API (for notional calculation)
  2. Hard enforcement of detect-only mode (refuse to start if runner can execute)
  3. Proof of zero execution side-effects with real sources

- **Decision:**

  - **PriceGetter module:** `src/grinder/reconcile/price_getter.py`
    - Fetches current market price from Binance Futures REST API (`/fapi/v1/ticker/price`)
    - Uses HttpClient protocol (same as SnapshotClient)
    - 1-second cache TTL to reduce API calls
    - Returns `Decimal | None` for safe handling of unavailable prices
    ```python
    @dataclass
    class PriceGetter:
        http_client: HttpClient
        config: PriceGetterConfig

        def get_price(self, symbol: str) -> Decimal | None:
            # Fetches from REST with caching
    ```

  - **detect_only enforcer in ReconcileLoopConfig:**
    ```python
    @dataclass
    class ReconcileLoopConfig:
        # Existing fields...
        detect_only: bool = True  # LC-14b: Hard enforcer
    ```

    - Hard check on `start()`: refuses to run if runner can execute actions
    - Condition for detect-only: `action=NONE OR (dry_run=True AND allow_active_remediation=False)`
    - Raises `RuntimeError` if detect-only violated

  - **Smoke test with real sources:** `scripts/smoke_live_reconcile_loop_real_sources.py`
    - Uses RequestsHttpClient for real Binance REST calls
    - Tests PriceGetter with live market data
    - FakePort records all execution calls
    - Verifies zero port calls at end (detect-only proof)
    ```python
    # Execution verification
    if len(fake_port.calls) > 0:
        raise AssertionError(f"DETECT-ONLY VIOLATED: {fake_port.calls}")
    print("✓ DETECT-ONLY MODE VERIFIED: Zero port calls")
    ```

- **Consequences:**
  - ReconcileLoop enforces detect-only mode at startup
  - PriceGetter enables notional calculation for flatten safety gate
  - Smoke test provides reproducible proof of no execution side-effects
  - Ready for production deployment with real market data

- **Safety Guarantees:**
  - detect_only=True default refuses to start if runner can execute
  - FakePort in smoke test catches any execution attempts
  - No API credentials required for price fetch (public endpoint)

- **Out of Scope (v0.1):**
  - WS user-data stream integration (FuturesUserDataWsConnector wiring)
  - REST snapshot fallback (SnapshotClient wiring to ObservedStateStore)
  - WebSocket price streaming (using REST for simplicity)

## ADR-050 — Operator Ceremony: Staged Enablement + Rollback (LC-15a)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** ReconcileLoop with remediation is production-ready but enabling it requires careful staged rollout. Operators need:
  1. Clear step-by-step procedure with verification at each stage
  2. Explicit rollback steps that work immediately
  3. Documented failure modes and expected behavior
  4. Proof that each stage doesn't execute until explicitly enabled

- **Decision:**

  - **5-stage enablement ceremony:**
    | Stage | Name | Execution | Verification |
    |-------|------|-----------|--------------|
    | 0 | Baseline | Disabled | Loop not running |
    | 1 | Detect-only | None | Port calls = 0, runs increasing |
    | 2 | Plan-only | Planned | action_planned > 0, executed = 0 |
    | 3 | Blocked | Blocked | action_blocked > 0 with reason |
    | 4 | Live | Executed | action_executed > 0 when mismatch |

  - **Each stage has explicit pass criteria** before proceeding:
    - Minimum runtime (10 minutes for stages 1-2)
    - Metrics verification
    - Log/audit verification
    - Zero unexpected execution calls

  - **Rollback is single-command:**
    ```bash
    # Any of these stops execution immediately:
    export RECONCILE_ENABLED=0    # Disable loop entirely
    export RECONCILE_ACTION=none  # Disable remediation
    export ARMED=0                # Block at armed gate
    ```

  - **Kill-switch semantics:** Remediation ALLOWED under kill-switch (ADR-043):
    - Cancel/flatten reduce risk
    - New trades blocked, cleanup allowed
    - Documented in ceremony runbook

  - **Smoke script for ceremony:** `scripts/smoke_enablement_ceremony.py`
    - Runs mini-stages A/B/C/D locally
    - Default: 0 execution calls
    - Verifies stage transitions work correctly
    - Optional `--inject-mismatch` for testing

- **Consequences:**
  - Operators have reproducible, auditable enablement procedure
  - Each stage can be verified before proceeding
  - Rollback is instant and reliable
  - Smoke script proves ceremony works without live execution

- **Related:**
  - Runbook: `docs/runbooks/15_ENABLEMENT_CEREMONY.md`
  - ADR-048: ReconcileLoop Wiring
  - ADR-049: Real Sources Wiring
  - ADR-043: Active Remediation (kill-switch semantics)

## ADR-051 — Reconcile Alerts, Metrics Contract, and SLOs (LC-15b)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** ReconcileLoop and remediation are production-ready but lack observability primitives:
  1. Metrics are exported but not integrated into `/metrics` endpoint contract
  2. No Prometheus alert rules for reconcile-specific failure modes
  3. No defined SLOs for loop availability, snapshot freshness, execution budget

- **Decision:**

  - **Metrics contract integration:**
    - Added reconcile metrics to `REQUIRED_METRICS_PATTERNS` in `live_contract.py`
    - MetricsBuilder now includes reconcile metrics via `_build_reconcile_metrics()`
    - Contract tests verify all reconcile metrics are present with correct labels
    ```python
    # Series-level patterns (enforce label schema)
    'grinder_reconcile_mismatch_total{type=',
    'grinder_reconcile_action_planned_total{action=',
    'grinder_reconcile_action_executed_total{action=',
    'grinder_reconcile_runs_with_remediation_total{action=',
    ```

  - **Prometheus alert rules (`monitoring/alert_rules.yml`):**
    | Alert | Severity | Condition |
    |-------|----------|-----------|
    | ReconcileLoopDown | warning | No runs for 5 min while grinder_up=1 |
    | ReconcileSnapshotStale | warning | last_snapshot_age > 120s for 2 min |
    | ReconcileMismatchSpike | warning | Mismatch rate > 0.1/sec for 3 min |
    | ReconcileRemediationExecuted | critical | Any real execution (increase > 0) |
    | ReconcileRemediationPlanned | info | Dry-run action planned |
    | ReconcileRemediationBlocked | info | Action blocked by gates |
    | ReconcileMismatchNoBlocks | warning | Mismatches but no remediation |

  - **Service Level Objectives:**
    | SLO | Target | Metric |
    |-----|--------|--------|
    | Loop Availability | 99.9% | runs_total > 0 per 5-min window |
    | Snapshot Freshness | 99% | age < 120s |
    | Execution Budget | < 10/day | action_executed_total daily increase |

  - **Runbook:** `docs/runbooks/16_RECONCILE_ALERTS_SLOS.md`
    - Triage procedures for each alert
    - Grafana dashboard queries
    - Emergency rollback steps
    - SLO burn rate monitoring

- **Consequences:**
  - Reconcile metrics are now part of the enforced `/metrics` contract
  - Prometheus alerts catch reconcile-specific failure modes
  - SLOs enable data-driven operational decisions
  - Operators have clear triage procedures for each alert

- **Related:**
  - ADR-048: ReconcileLoop Wiring
  - ADR-049: Real Sources Wiring
  - ADR-050: Operator Ceremony
  - Runbook: `docs/runbooks/16_RECONCILE_ALERTS_SLOS.md`

## ADR-052 — Remediation Safety Extensions (LC-18)

- **Date:** 2026-02-06
- **Status:** accepted
- **Context:** Active remediation (LC-10) is functional but requires additional safety layers for production:
  1. Strategy allowlist: Only remediate orders from specific strategies (uses LC-12 identity)
  2. Per-run and per-day budget limits: Cap calls and notional exposure
  3. Staged rollout modes: DETECT_ONLY → PLAN_ONLY → BLOCKED → EXECUTE_CANCEL_ALL → EXECUTE_FLATTEN
  4. Budget persistence: Track daily usage across restarts

- **Decision:**

  - **RemediationMode enum** (`config.py`):
    ```python
    class RemediationMode(Enum):
        DETECT_ONLY = "detect_only"      # Detect mismatches, no planning (0 calls)
        PLAN_ONLY = "plan_only"          # Plan remediation, increment planned metrics (0 calls)
        BLOCKED = "blocked"              # Plan + block by gates, increment blocked metrics (0 calls)
        EXECUTE_CANCEL_ALL = "execute_cancel_all"  # Execute only cancel_all actions
        EXECUTE_FLATTEN = "execute_flatten"        # Execute only flatten actions
    ```

  - **Budget config fields** (`ReconcileConfig`):
    ```python
    remediation_mode: RemediationMode = RemediationMode.DETECT_ONLY
    remediation_strategy_allowlist: set[str] = field(default_factory=set)
    remediation_symbol_allowlist: set[str] = field(default_factory=set)
    max_calls_per_day: int = 100
    max_notional_per_day: Decimal = Decimal("5000")
    max_calls_per_run: int = 10
    max_notional_per_run: Decimal = Decimal("1000")
    flatten_max_notional_per_call: Decimal = Decimal("500")
    budget_state_path: str | None = None  # JSON persistence
    ```

  - **BudgetTracker** (`budget.py`):
    - Tracks calls_today, notional_today with daily reset at midnight UTC
    - Tracks calls_this_run, notional_this_run with per-run reset
    - Persists daily state to JSON file (optional)
    - `can_execute(notional) → (allowed, block_reason)` checks both limits
    - `record_execution(notional)` updates counters

  - **Extended block reasons** (`RemediationBlockReason`):
    ```python
    # Mode-based reasons
    MODE_DETECT_ONLY = "mode_detect_only"
    MODE_PLAN_ONLY = "mode_plan_only"
    MODE_BLOCKED = "mode_blocked"
    MODE_CANCEL_ONLY = "mode_cancel_only"
    MODE_FLATTEN_ONLY = "mode_flatten_only"

    # Budget reasons
    MAX_CALLS_PER_RUN = "max_calls_per_run"
    MAX_NOTIONAL_PER_RUN = "max_notional_per_run"
    MAX_CALLS_PER_DAY = "max_calls_per_day"
    MAX_NOTIONAL_PER_DAY = "max_notional_per_day"

    # Allowlist reasons
    STRATEGY_NOT_ALLOWED = "strategy_not_allowed"
    SYMBOL_NOT_IN_REMEDIATION_ALLOWLIST = "symbol_not_in_remediation_allowlist"
    ```

  - **Budget metrics** (`ReconcileMetrics`):
    ```python
    budget_calls_used_day: int
    budget_notional_used_day: Decimal
    budget_calls_remaining_day: int
    budget_notional_remaining_day: Decimal
    ```

  - **Gate ordering in `can_execute()`:**
    1. Mode check (DETECT_ONLY/PLAN_ONLY/BLOCKED early exit)
    2. Strategy allowlist (uses `parse_client_order_id()` from LC-12)
    3. Symbol remediation allowlist (optional)
    4. Budget per-run limits
    5. Budget per-day limits
    6. Original LC-10 gates (action, dry_run, allow_active, armed, env_var, cooldown, whitelist)

  - **Mode semantics:**
    - DETECT_ONLY: Returns PLANNED with MODE_DETECT_ONLY (0 port calls)
    - PLAN_ONLY: Returns PLANNED with MODE_PLAN_ONLY (0 port calls)
    - BLOCKED: Returns PLANNED with MODE_BLOCKED (0 port calls, but logs as blocked)
    - EXECUTE_CANCEL_ALL: Allows only cancel_all actions (blocks flatten with MODE_CANCEL_ONLY)
    - EXECUTE_FLATTEN: Allows only flatten actions (blocks cancel with MODE_FLATTEN_ONLY)

- **Test coverage:**
  - `tests/unit/test_remediation.py` (41 tests total, 13 new for LC-18):
    - Mode semantics: detect_only, plan_only, blocked, execute_cancel_all, execute_flatten
    - Strategy allowlist: allowed, blocked, empty=allow all
    - Budget gates: max_calls_per_run, max_notional_per_run, reset_between_runs

- **Consequences:**
  - Staged rollout: Operators can safely progress through modes
  - Budget enforcement: Daily exposure limited even with bugs
  - Strategy isolation: Only specified strategies can trigger remediation
  - No breaking changes: Default mode is DETECT_ONLY (safe)

- **Related:**
  - ADR-043: Active Remediation v0.1
  - ADR-045: Configurable Order Identity (LC-12)
  - Runbook: `docs/runbooks/12_ACTIVE_REMEDIATION.md` (update pending)
