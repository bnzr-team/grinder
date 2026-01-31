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
  - Canonical digests locked: `sample_day` = `66b29a4e92192f8f`, `sample_day_allowed` = `ec223bce78d7926f`
- **Consequences:**
  - Adding new fields is safe (append-only)
  - Removing/renaming existing fields is breaking change requiring version bump
  - Downstream tools can rely on field presence
  - Digests change when output structure changes (intentional)
- **Alternatives:**
  - No versioning — rejected because silent breaks are worse
  - Semantic versioning — deferred, simple "v1" sufficient for now
