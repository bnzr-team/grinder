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
