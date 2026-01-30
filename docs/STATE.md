# STATE — Current Implementation Status

Цель: фиксировать **что реально работает сейчас** (а не что хотелось бы). Обновлять в каждом PR, если изменилось.

## Works now
- `python -m scripts.run_live` поднимает `/healthz` и `/metrics`.
- Replay utilities: `python -m scripts.run_replay` и `python -m scripts.verify_replay_determinism`.
- `python -m scripts.secret_guard` проверяет repo на утечки секретов.

## Partially implemented
- Структура пакета `src/grinder/*` (core, protocols/interfaces) — каркас.
- Документация в `docs/*` — SSOT по архитектуре/спекам (но должна совпадать с реализацией).

## Known gaps / mismatches
- Workflows/упаковка/compose содержат ссылки на несуществующие компоненты (должно быть исправлено в ближайших PR):
  - `nightly_soak.yml` вызывает `scripts.run_soak` (скрипта нет).
  - `pyproject.toml` объявляет CLI entrypoints (`grinder`, `grinder-paper`, `grinder-backtest`), но модулей ещё нет.
  - Docker/compose healthcheck используют `curl`, которого нет в `python:3.11-slim`.
  - `docker-compose.yml` ожидает Grafana provisioning (`monitoring/grafana/provisioning`), которого нет.
  - README содержит неверный clone URL и команды, которые пока не исполняются.

## Planned next
- Привести README/pyproject/CI/Docker к честному состоянию.
- Сделать минимально рабочие CLI (хотя бы `--help` + запуск существующих scripts).
- Добавить базовый Grafana provisioning (datasource + 1 dashboard).
