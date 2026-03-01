# 27 — FSM Operator Override

This runbook describes the operator-facing override that forces the LiveEngine FSM into a safe state.

## What it is

`GRINDER_OPERATOR_OVERRIDE` is an environment variable read by `LiveEngineV0` on every `process_snapshot()` tick.
It is applied **before** action processing, so the FSM state immediately affects Gate 6 (FSM permission gate).

The override is interpreted by the FSM driver and can force transitions such as:
- `ACTIVE -> PAUSED`
- `ACTIVE -> EMERGENCY`

## Allowed values

| Value | Effect |
|-------|--------|
| `PAUSE` | Forces FSM into `PAUSED`. Write path blocks increase-risk intents (e.g. PLACE). Reduce-risk (CANCEL) still allowed. |
| `EMERGENCY` | Forces FSM into `EMERGENCY`. Write path blocks increase-risk intents. |

## Normalization rules

The value is normalized on every tick:

- Leading/trailing whitespace is stripped
- Value is uppercased (case-insensitive)
- Empty string after normalization is treated as unset (`None`)
- Invalid values are ignored (treated as unset) and a warning is logged

Examples:

| Raw value | Normalized | Effect |
|-----------|------------|--------|
| `PAUSE` | `PAUSE` | override active |
| ` pause ` | `PAUSE` | override active |
| `Emergency` | `EMERGENCY` | override active |
| `   ` | (empty) | no override |
| `""` | (empty) | no override |
| `INVALID` | `INVALID` | warning + no override |

## How to use

### Enable PAUSE

```bash
export GRINDER_OPERATOR_OVERRIDE=PAUSE
```

### Enable EMERGENCY

```bash
export GRINDER_OPERATOR_OVERRIDE=EMERGENCY
```

### Disable

```bash
unset GRINDER_OPERATOR_OVERRIDE
# or:
export GRINDER_OPERATOR_OVERRIDE=""
```

## What you should observe

### Logs

On valid override causing a transition:
- `FSM_TRANSITION` log with `from_state`, `to_state`, `reason` fields
- Reason will be `OPERATOR_PAUSE` or `OPERATOR_EMERGENCY`

On invalid values:
- Warning: `Invalid GRINDER_OPERATOR_OVERRIDE='...' (normalized='...'), treating as None`

### Metrics

FSM metrics reflect the forced state:

- `grinder_fsm_current_state{state="PAUSED"} 1` when overridden to PAUSE
- `grinder_fsm_current_state{state="EMERGENCY"} 1` when overridden to EMERGENCY
- `grinder_fsm_transitions_total{from_state="ACTIVE",to_state="PAUSED",reason="OPERATOR_PAUSE"} 1`

If Gate 6 blocks an intent due to FSM state:

- `grinder_fsm_action_blocked_total{state="PAUSED",intent="INCREASE_RISK"} ...` increments
- Warning log `FSM_ACTION_BLOCKED` with `state` and `intent` fields

## Safety notes

- Override is read from env var every tick; it is **not persisted** anywhere else.
- Invalid override values are **ignored** (safe default: state remains as driven by real signals).
- The tick still runs even with invalid values (duration gauge, state gauge keep updating).
- This override does not modify FSM transition logic; it only provides a runtime input path.

## Troubleshooting

### Override set but no effect

1. Confirm the process environment includes the variable: `env | grep GRINDER_OPERATOR`
2. Confirm `process_snapshot()` is being called (FSM ticks from there).
3. Check metrics: `curl -s localhost:9090/metrics | grep grinder_fsm_current_state`

### You see invalid override warnings

- The value is not `PAUSE` or `EMERGENCY` after normalization.
- Fix by setting exactly one of the allowed values.

### State doesn't revert after unsetting override

- The FSM follows its normal transition rules after the override is removed.
- Recovery from PAUSED requires cooldown elapsed + no active pause triggers.
- Recovery from EMERGENCY requires `position_notional_usd < threshold` (default $10 USDT) + no active emergency triggers.
- See `docs/08_STATE_MACHINE.md` Sec 8.10 for the full state diagram.

## Evidence artifacts

When enabled, every FSM transition writes a deterministic evidence bundle (`.txt` + `.sha256`).

### Enable

```bash
export GRINDER_FSM_EVIDENCE=1
# Optional: set output directory (default: ./artifacts)
export GRINDER_ARTIFACT_DIR=/path/to/artifacts
```

Evidence files appear under `${GRINDER_ARTIFACT_DIR:-artifacts}/fsm/`:

```
fsm/
  fsm_transition_1706000000_ACTIVE_EMERGENCY.txt
  fsm_transition_1706000000_ACTIVE_EMERGENCY.sha256
```

### Verify integrity

```bash
cd ${GRINDER_ARTIFACT_DIR:-artifacts}/fsm
# Verify all artifacts:
sha256sum -c fsm_transition_*.sha256
```

### Disable

```bash
unset GRINDER_FSM_EVIDENCE
# or:
export GRINDER_FSM_EVIDENCE=0
```

Truthy values: `1`, `true`, `yes`, `on` (case-insensitive, whitespace-trimmed).
Falsey values: `0`, `false`, `no`, `off`, empty, unset.
Unknown values default to disabled (safe).

### Format

Each `.txt` file contains a canonical text block:

```
artifact_version=fsm_evidence_v2
ts_ms=1706000000
from_state=ACTIVE
to_state=EMERGENCY
reason=KILL_SWITCH
signals:
  drawdown_pct=0.0
  feed_gap_ms=0
  kill_switch_active=True
  operator_override=None
  position_notional_usd=0.0
  spread_bps=0.0
  toxicity_score_bps=0.0
```

Signals are sorted by key name. Format is deterministic: same event always produces identical output.
