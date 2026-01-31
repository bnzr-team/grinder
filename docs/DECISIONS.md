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
- **Date:** 2026-01-31
- **Status:** accepted
- **Context:** GitHub flags "hidden or bidirectional Unicode" in PRs; need clear policy on allowed vs dangerous chars.
- **Decision:**
  - **Forbidden:** bidi controls (U+202A-E, U+2066-9), zero-width (U+200B-D, U+FEFF), soft hyphen (U+00AD)
  - **Allowed:** box-drawing (U+2500-257F) for diagrams, Cyrillic for Russian text
  - Use `scripts/check_unicode.py` to verify before merge
- **Consequences:** PRs must pass Unicode scan; box-drawing triggers GitHub warning but is allowed.
- **Alternatives:** replace all box-drawing with ASCII art — rejected for readability.

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
