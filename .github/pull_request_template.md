# PR Checklist (MUST)

> **No Proof Bundle = no review.**  
> Paste raw command outputs (not screenshots). If something is not applicable, explain why.

---

## What
<!-- 1–5 bullets: what changed -->

## Why
<!-- The reason / problem being solved -->

## Changes
<!-- Concrete list of code/docs/infra changes -->
- 
- 
- 

## Risks
**Risk level:** LOW / MEDIUM / HIGH  
<!-- Explain impact radius + failure modes -->

## Rollback plan
<!-- Exact steps to revert/disable -->

---

## Milestone
- [ ] None
- [ ] M1 (Vertical Slice v0.1)
- [ ] M2 (Beta v0.5)
- [ ] M3 (Production v1.0)

---

## Proof (required)
Paste command output under each item.

- [ ] `PYTHONPATH=src python -m pytest -q`
<details><summary>pytest output</summary>

```text

```

</details>

- [ ] `ruff check .` and `ruff format --check .`
<details><summary>ruff output</summary>

```text

```

</details>

- [ ] `python -m scripts.secret_guard --verbose` (required for docs/infra/config changes)
<details><summary>secret_guard output</summary>

```text

```

</details>

- [ ] Replay determinism (required if touching policy/risk/execution/fixtures):
  - `python -m scripts.verify_replay_determinism`
<details><summary>determinism output</summary>

```text

```

</details>

- [ ] Docker/Compose (required if touching Docker/compose/monitoring):
  - `docker build -t grinder:test .`
  - `docker run --rm -p 9090:9090 grinder:test --duration-s 10 --metrics-port 9090`
  - `curl -fsS http://localhost:9090/healthz`
  - `curl -fsS http://localhost:9090/metrics | head`
  - `docker compose up -d` (if compose changed)
<details><summary>docker output</summary>

```text

```

</details>

---

## SSOT updates (required when behavior/contracts change)
Check what you updated.

- [ ] `docs/STATE.md`
- [ ] `docs/DECISIONS.md`
- [ ] Other docs (list):
  - 

---

## Notes for reviewer
<!-- Anything that helps review quickly: tradeoffs, known gaps, follow-ups -->

---

<!-- ACCEPTANCE_PACKET_START -->
== ACCEPTANCE PACKET: PENDING ==
<!-- CI will auto-populate this section -->
<!-- ACCEPTANCE_PACKET_END -->
