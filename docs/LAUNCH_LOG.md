# Launch Log — Grinder v1

> Evidence store for launch ceremonies C3 and C4.
>
> Each entry is dated with operator name, ceremony step, and verbatim evidence.
> Referenced from `docs/LAUNCH_PLAN.md` Section 7 (Ceremony Tracker).

---

## How to use this file

1. Before starting a ceremony step, copy the template below.
2. Fill in all evidence fields with **verbatim command outputs** (not summaries).
3. Mark the ceremony step DONE in `docs/LAUNCH_PLAN.md` only after evidence is recorded here.

---

## C3 — Canary

**Status:** NOT STARTED

### Precondition checks

```
Date:
Operator:
Main commit:
Release gates (Section 2): [ ] ALL PASS
```

### Preflight

```
Command: python3 -m scripts.preflight_fill_prob --model $GRINDER_FILL_MODEL_DIR --eval $GRINDER_FILL_PROB_EVAL_DIR --auto-threshold
Output:
  (paste verbatim)
```

### Startup

```
Symbol allowlist:
Exchange port: futures
Startup log (FILL_PROB_THRESHOLD_RESOLUTION_OK line):
  (paste verbatim)
Post-restart metrics:
  enforce_enabled=
  allowlist_enabled=
  cb_trips=
```

### Observation window

```
Start time:
End time:
Duration:
blocks_total=
cb_trips=
Budget/drawdown status:
Unexpected writes (Y/N):
Alerts fired (list or "none"):
```

### Sign-off

```
Result: PASS / FAIL
Notes:
Operator:
Date:
```

---

## C4 — Full Rollout

**Status:** NOT STARTED

### Precondition checks

```
Date:
Operator:
Main commit:
C3 evidence: [ ] recorded above with PASS
Kill-switch tested: [ ] trip + recovery verified
```

### Startup

```
GRINDER_FILL_PROB_ENFORCE_SYMBOLS= (empty = all)
Exchange port: futures
Startup log (FILL_PROB_THRESHOLD_RESOLUTION_OK line):
  (paste verbatim)
Post-restart metrics:
  enforce_enabled=
  allowlist_enabled=
  cb_trips=
```

### Observation window (24h minimum)

```
Start time:
End time:
Duration:
blocks_total=
cb_trips=
Block rate (approx %):
Budget/drawdown status:
Alerts fired (list or "none"):
```

### Phase 5 — Auto-threshold (optional)

```
Enabled: Y/N
mode= (from startup log)
effective_bps=
recommended_bps=
```

### Sign-off

```
Result: PASS / FAIL
Notes:
Operator:
Date:
```

**If C4 = PASS: Launch v1 achieved.**
