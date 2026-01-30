# CLAUDE.md — Конституция Grinder

Ты (Claude) — исполнитель/кодер. Этот файл — закон разработки. Нарушение **MUST** = PR не принимается.

## 1) Источники правды (SSOT)
Если меняешь поведение/интерфейсы/архитектуру/пороги — обновляй соответствующие документы в `docs/`:

- `docs/00_PRODUCT.md` — продукт/цели/границы
- `docs/03_ARCHITECTURE.md` — архитектура и потоки
- `docs/04_PREFILTER_SPEC.md` — prefilter/gating входа
- `docs/06_TOXICITY_SPEC.md` — toxicity/gating
- `docs/07_GRID_POLICY_LIBRARY.md` — политики (контракты)
- `docs/09_EXECUTION_SPEC.md` — исполнение/ордера
- `docs/10_RISK_SPEC.md` — риск-ограничения
- `docs/11_BACKTEST_PROTOCOL.md` — бэктест/реплей/детерминизм
- `docs/13_OBSERVABILITY.md` — метрики/алерты/логирование
- `docs/14_GITHUB_WORKFLOW.md` — CI/процессы
- `docs/15_CONSTANTS.md` — константы/пороги

Дополнительно (репо-управление):
- `docs/DECISIONS.md` — почему мы так решили (ADR)
- `docs/STATE.md` — что реально работает сейчас (без фантазий)

README и `pyproject.toml` обязаны соответствовать реальности. Никаких “написано, но нет”.

## 2) Proof Bundle обязателен для каждого PR
В описание PR добавляй `## Proof` и вставляй вывод команд (не скриншоты):

- `PYTHONPATH=src python -m pytest -q`
- `python -m scripts.verify_replay_determinism` (если трогал replay/fixtures/policy/risk/execution)
- `python -m scripts.secret_guard --verbose` (для PR’ов затрагивающих конфиги/infra/доки/скрипты)
- если менял Docker/compose: команды сборки/запуска и проверка `/healthz` и `/metrics`

Если пишешь “исправил/работает/ускорил” — показывай **до/после** и чем мерил.

## 3) Нулевой допуск к мусору
Запрещено коммитить или включать в архивы/артефакты:
- `.git/`
- `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`
- `__pycache__/`, `*.pyc`

Если мусор попал — отдельный PR на очистку обязателен.

## 4) Контракты нельзя ломать тихо
Любые изменения:
- CLI (`[project.scripts]` / команды)
- форматов конфигов/JSON/fixtures
- имён Prometheus-метрик
- структуры выводов replay

…должны сопровождаться:
- обновлением соответствующего `docs/*`
- тестом, фиксирующим новый контракт

## 5) Детерминизм — закон
Replay/бэктест должны быть детерминированны:
- одинаковый вход → одинаковый digest/выход
- рандом только с фиксированным seed и документированием

## 6) Packaging truth
- Если entrypoint объявлен в `pyproject.toml` — модуль обязан существовать.
- Версия должна быть единым источником правды.

## 7) CI truth
Workflows не имеют права ссылаться на несуществующие файлы/скрипты.
Если workflow добавлен — он либо проходит, либо выключен до реализации.

## 8) Шаблон PR (обязательно)

### What
- …

### Why
- …

### Changes
- …

### Risks
- …

### Proof
- pytest: …
- replay: …
- secret_guard: …
- docker/compose: … (если применимо)

### Docs updated
- перечисли, какие `docs/*.md` обновил и почему
