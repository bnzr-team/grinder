# STATE — Current Implementation Status

Цель: фиксировать **что реально работает сейчас** (а не что хотелось бы). Обновлять в каждом PR, если изменилось.

Next steps and progress tracker: `docs/ROADMAP.md`.

## Works now
- `grinder --help` / `grinder-paper --help` / `grinder-backtest --help` — CLI entrypoints работают.
- `python -m scripts.run_live` поднимает `/healthz` и `/metrics`.
- `python -m scripts.run_soak` генерирует synthetic soak metrics JSON.
- **End-to-end replay**:
  - CLI: `grinder replay --fixture <path> [-v] [--out <path>]`
  - Script: `python -m scripts.run_replay --fixture <path> [-v] [--out <path>]`
  - Determinism check: `python -m scripts.verify_replay_determinism --fixture <path>`
  - Output format: `Replay completed. Events processed: N\nOutput digest: <16-char-hex>`
  - Expected digest for `tests/fixtures/sample_day`: `453ebd0f655e4920`
  - Fixture format: SNAPSHOT events (see ADR-006 for migration from BOOK_TICKER)
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
