# GRINDER - State Machine

> System states, transitions, and operational modes

> **Status:** PARTIAL (regime classifier only, no centralized FSM)
>
> **Reality (implemented now):**
> - `classify_regime()` in `src/grinder/controller/regime.py` — precedence-based regime classification
> - Regime enum: RANGE, TREND_UP, TREND_DOWN, VOL_SHOCK, THIN_BOOK, TOXIC, PAUSED, EMERGENCY
> - RegimeConfig with configurable thresholds (no magic numbers)
> - Regime drives AdaptiveGridPolicy behavior (spacing, width, levels, mode)
> - KillSwitch (`src/grinder/risk/kill_switch.py`) triggers EMERGENCY regime
>
> **Not implemented yet (Launch-13 target):**
> - Centralized `OrchestratorFSM` class (Sec 8.9-8.14)
> - Explicit INIT / READY / DEGRADED state handlers
> - State persistence (save/load across restarts)
> - State guards (`can_enter`, `can_exit` validation)
> - Multi-symbol state coordination
> - On-enter / on-exit actions as formal hooks
>
> **Tracking:** Regime classification covers core needs. Formal FSM orchestrator is Launch-13 (P1 Hardening Pack).
> Sections 8.1-8.8 describe existing target-state sketches. Sections 8.9-8.14 are the Launch-13 orchestrator spec (SSOT).

---

## 8.1 System States

```
┌─────────────────────────────────────────────────────────────────┐
│                    GRINDER STATE MACHINE                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│    ┌──────────┐                                                 │
│    │  INIT    │                                                 │
│    └────┬─────┘                                                 │
│         │ Health checks pass                                    │
│         ▼                                                       │
│    ┌──────────┐         Feed issues          ┌──────────┐      │
│    │  READY   │◄─────────────────────────────│ DEGRADED │      │
│    └────┬─────┘                              └────┬─────┘      │
│         │ Top-K selected                          │             │
│         ▼                                         │             │
│    ┌──────────┐         Tox HIGH             ┌───┴──────┐      │
│    │  ACTIVE  │─────────────────────────────►│  PAUSED  │      │
│    │  (Grid)  │◄─────────────────────────────│          │      │
│    └────┬─────┘         Cooldown OK          └────┬─────┘      │
│         │                                         │             │
│         │ Tox MID                                │             │
│         ▼                                         │             │
│    ┌──────────┐                                  │             │
│    │THROTTLED │──────────────────────────────────┘             │
│    │          │         Tox HIGH                               │
│    └──────────┘                                                 │
│         │                                                       │
│         │ DD breach / Error                                    │
│         ▼                                                       │
│    ┌──────────┐                                                 │
│    │EMERGENCY │                                                 │
│    │  EXIT    │                                                 │
│    └──────────┘                                                 │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 8.2 State Definitions

| State | Description | Grid Activity | Orders |
|-------|-------------|---------------|--------|
| **INIT** | System starting up | None | None |
| **READY** | Connected, waiting for Top-K | None | None |
| **ACTIVE** | Normal operation | Full grid | All policies |
| **THROTTLED** | Reduced activity | Partial grid | Wider spacing, smaller size |
| **PAUSED** | No new orders | Frozen | Maintain existing only |
| **DEGRADED** | Data issues | Frozen | Maintain existing only |
| **EMERGENCY** | Risk breach | Exit only | Aggressive reduction |

---

## 8.3 State Transitions

| From | To | Trigger | Action |
|------|-----|---------|--------|
| INIT | READY | Health OK | Start data feeds |
| READY | ACTIVE | Top-K ready | Enable L2, start policies |
| ACTIVE | THROTTLED | `tox ∈ [LOW, HIGH)` | Widen spacing, reduce size |
| ACTIVE | PAUSED | `tox ≥ HIGH` | Cancel new orders |
| THROTTLED | ACTIVE | `tox < LOW` for `T_COOLDOWN` | Resume normal |
| THROTTLED | PAUSED | `tox ≥ HIGH` | Cancel new orders |
| PAUSED | ACTIVE | `tox < LOW` for `T_COOLDOWN` | Resume |
| ANY | DEGRADED | Feed stale | Freeze state, wait |
| ANY | EMERGENCY | DD breach | Cancel all, reduce inventory |
| EMERGENCY | PAUSED | Position reduced | Wait for manual review |

---

## 8.4 State Implementation

```python
from enum import Enum
from dataclasses import dataclass
from typing import Callable

class SystemState(Enum):
    INIT = "INIT"
    READY = "READY"
    ACTIVE = "ACTIVE"
    THROTTLED = "THROTTLED"
    PAUSED = "PAUSED"
    DEGRADED = "DEGRADED"
    EMERGENCY = "EMERGENCY"

@dataclass
class StateTransition:
    from_state: SystemState
    to_state: SystemState
    trigger: str
    action: Callable | None = None

class StateMachine:
    """Grinder state machine."""

    def __init__(self):
        self.state = SystemState.INIT
        self.transitions = self._build_transitions()
        self.state_enter_ts: int = 0
        self.history: list[tuple[int, SystemState, str]] = []

    def _build_transitions(self) -> dict[tuple[SystemState, str], StateTransition]:
        """Build transition table."""
        transitions = [
            StateTransition(SystemState.INIT, SystemState.READY, "HEALTH_OK"),
            StateTransition(SystemState.READY, SystemState.ACTIVE, "TOPK_READY"),
            StateTransition(SystemState.ACTIVE, SystemState.THROTTLED, "TOX_MID"),
            StateTransition(SystemState.ACTIVE, SystemState.PAUSED, "TOX_HIGH"),
            StateTransition(SystemState.THROTTLED, SystemState.ACTIVE, "TOX_LOW_COOLDOWN"),
            StateTransition(SystemState.THROTTLED, SystemState.PAUSED, "TOX_HIGH"),
            StateTransition(SystemState.PAUSED, SystemState.ACTIVE, "TOX_LOW_COOLDOWN"),
            StateTransition(SystemState.PAUSED, SystemState.THROTTLED, "TOX_MID_COOLDOWN"),
            # Degraded transitions
            StateTransition(SystemState.ACTIVE, SystemState.DEGRADED, "FEED_STALE"),
            StateTransition(SystemState.THROTTLED, SystemState.DEGRADED, "FEED_STALE"),
            StateTransition(SystemState.DEGRADED, SystemState.ACTIVE, "FEED_RECOVERED"),
            # Emergency transitions
            StateTransition(SystemState.ACTIVE, SystemState.EMERGENCY, "DD_BREACH"),
            StateTransition(SystemState.THROTTLED, SystemState.EMERGENCY, "DD_BREACH"),
            StateTransition(SystemState.PAUSED, SystemState.EMERGENCY, "DD_BREACH"),
            StateTransition(SystemState.EMERGENCY, SystemState.PAUSED, "POSITION_REDUCED"),
        ]

        return {
            (t.from_state, t.trigger): t
            for t in transitions
        }

    def can_transition(self, trigger: str) -> bool:
        """Check if transition is valid."""
        return (self.state, trigger) in self.transitions

    def transition(self, trigger: str, ts: int) -> bool:
        """Execute transition if valid."""
        key = (self.state, trigger)
        if key not in self.transitions:
            return False

        transition = self.transitions[key]
        old_state = self.state

        # Execute action if defined
        if transition.action:
            transition.action()

        # Update state
        self.state = transition.to_state
        self.state_enter_ts = ts
        self.history.append((ts, old_state, trigger))

        logger.info(f"State transition: {old_state} -> {self.state} ({trigger})")
        return True

    def time_in_state(self, ts: int) -> int:
        """Time in current state in ms."""
        return ts - self.state_enter_ts
```

---

## 8.5 State Actions

### On Enter ACTIVE

```python
async def on_enter_active(self):
    """Actions when entering ACTIVE state."""
    # Start policy engine
    await self.policy_engine.start()

    # Enable L2 subscriptions for Top-K
    for symbol in self.topk_symbols:
        await self.connector.subscribe_depth(symbol)

    # Log state entry
    logger.info("Entering ACTIVE state", extra={
        "topk": self.topk_symbols,
        "toxicity": {s: self.get_toxicity(s) for s in self.topk_symbols}
    })
```

### On Enter THROTTLED

```python
async def on_enter_throttled(self, reason: str):
    """Actions when entering THROTTLED state."""
    # Modify grid parameters
    self.config.spacing_multiplier = 1.5
    self.config.size_multiplier = 0.6

    # Log with reason
    logger.warning("Entering THROTTLED state", extra={
        "reason": reason,
        "toxicity": self.current_toxicity
    })
```

### On Enter PAUSED

```python
async def on_enter_paused(self, reason: str):
    """Actions when entering PAUSED state."""
    # Stop new order placement
    self.policy_engine.pause()

    # Optionally cancel far orders
    if self.config.cancel_far_orders_on_pause:
        await self.execution_engine.cancel_far_orders()

    logger.warning("Entering PAUSED state", extra={
        "reason": reason,
        "active_orders": len(self.active_orders)
    })
```

### On Enter EMERGENCY

```python
async def on_enter_emergency(self, reason: str):
    """Actions when entering EMERGENCY state."""
    # Cancel all orders
    await self.execution_engine.cancel_all_orders()

    # Start position reduction
    await self.execution_engine.start_emergency_exit()

    # Alert operators
    await self.alert_manager.send_alert(
        level="CRITICAL",
        message=f"EMERGENCY: {reason}",
        context={
            "positions": self.positions,
            "pnl": self.session_pnl,
            "dd": self.current_dd
        }
    )

    logger.critical("Entering EMERGENCY state", extra={
        "reason": reason
    })
```

---

## 8.6 State Guards

```python
class StateGuards:
    """Guards for state transitions."""

    def __init__(self, config: StateConfig):
        self.config = config
        self.cooldown_tracker = CooldownTracker()

    def can_enter_active(self, context: StateContext) -> tuple[bool, str]:
        """Check if can enter ACTIVE."""
        # Check feeds are fresh
        if context.max_feed_staleness_ms > self.config.max_staleness_ms:
            return False, "FEEDS_STALE"

        # Check Top-K is ready
        if len(context.topk_symbols) < self.config.min_topk:
            return False, "TOPK_NOT_READY"

        # Check cooldown after pause
        if self.cooldown_tracker.in_cooldown(context.ts):
            return False, "COOLDOWN_ACTIVE"

        return True, "OK"

    def can_exit_emergency(self, context: StateContext) -> tuple[bool, str]:
        """Check if can exit EMERGENCY."""
        # Check position is reduced
        total_notional = sum(
            abs(p.notional) for p in context.positions.values()
        )
        if total_notional > self.config.emergency_exit_threshold:
            return False, "POSITION_NOT_REDUCED"

        return True, "OK"
```

---

## 8.7 State Persistence

```python
@dataclass
class PersistedState:
    """State that survives restarts."""
    state: SystemState
    state_enter_ts: int
    session_start_ts: int
    session_pnl: Decimal
    session_dd: Decimal
    daily_pnl: Decimal
    daily_dd: Decimal
    positions: dict[str, Position]
    active_orders: list[Order]

def save_state(state: PersistedState, path: Path) -> None:
    """Save state to disk."""
    data = {
        "state": state.state.value,
        "state_enter_ts": state.state_enter_ts,
        "session_start_ts": state.session_start_ts,
        "session_pnl": str(state.session_pnl),
        "session_dd": str(state.session_dd),
        "daily_pnl": str(state.daily_pnl),
        "daily_dd": str(state.daily_dd),
        "positions": [p.to_dict() for p in state.positions.values()],
        "active_orders": [o.to_dict() for o in state.active_orders],
        "saved_at": int(time.time() * 1000)
    }

    # Atomic write
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w") as f:
        json.dump(data, f)
    temp_path.rename(path)

def load_state(path: Path) -> PersistedState | None:
    """Load state from disk."""
    if not path.exists():
        return None

    with open(path) as f:
        data = json.load(f)

    return PersistedState(
        state=SystemState(data["state"]),
        state_enter_ts=data["state_enter_ts"],
        session_start_ts=data["session_start_ts"],
        session_pnl=Decimal(data["session_pnl"]),
        session_dd=Decimal(data["session_dd"]),
        daily_pnl=Decimal(data["daily_pnl"]),
        daily_dd=Decimal(data["daily_dd"]),
        positions={p["symbol"]: Position.from_dict(p) for p in data["positions"]},
        active_orders=[Order.from_dict(o) for o in data["active_orders"]],
    )
```

---

## 8.8 State Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `grinder_state` | Gauge | Current state (encoded) |
| `grinder_state_duration_s` | Gauge | Time in current state |
| `grinder_state_transitions_total` | Counter | Total transitions |
| `grinder_state_transitions` | Counter | Transitions by from/to/trigger |

---

## 8.9 Orchestrator Contracts (Launch-13)

> **SSOT for Launch-13 FSM orchestrator.**
> Implementation PRs reference this section. Any conflict = update this spec first.

### 8.9.1 TransitionReason

Reason codes attached to every state transition. Greppable in logs and evidence.

```python
class TransitionReason(Enum):
    """Why the FSM changed state. Every transition carries exactly one reason."""
    # INIT -> READY
    HEALTH_OK = "HEALTH_OK"

    # READY -> ACTIVE
    FEEDS_READY = "FEEDS_READY"

    # -> THROTTLED
    TOX_MID = "TOX_MID"

    # -> PAUSED
    TOX_HIGH = "TOX_HIGH"
    OPERATOR_PAUSE = "OPERATOR_PAUSE"

    # -> ACTIVE (recovery)
    TOX_LOW_COOLDOWN = "TOX_LOW_COOLDOWN"
    TOX_MID_COOLDOWN = "TOX_MID_COOLDOWN"
    FEED_RECOVERED = "FEED_RECOVERED"

    # -> DEGRADED
    FEED_STALE = "FEED_STALE"

    # -> EMERGENCY
    DD_BREACH = "DD_BREACH"
    KILL_SWITCH = "KILL_SWITCH"
    OPERATOR_EMERGENCY = "OPERATOR_EMERGENCY"

    # EMERGENCY -> PAUSED
    POSITION_REDUCED = "POSITION_REDUCED"
```

### 8.9.2 TransitionEvent

Immutable record emitted on every transition.

```python
@dataclass(frozen=True)
class TransitionEvent:
    ts_ms: int                     # monotonic event timestamp
    from_state: SystemState
    to_state: SystemState
    reason: TransitionReason
    evidence_ref: str | None       # optional EVIDENCE_REF pointer
```

### 8.9.3 OrchestratorInputs

Snapshot of inputs the FSM evaluates on each tick.

```python
@dataclass(frozen=True)
class OrchestratorInputs:
    ts_ms: int
    regime: Regime                 # from classify_regime()
    toxicity: float                # current tox score
    max_feed_staleness_ms: int     # worst-case feed age
    drawdown_pct: float            # current session drawdown
    kill_switch_active: bool       # from KillSwitch
    position_notional: float       # abs total notional
    operator_override: str | None  # "PAUSE" | "EMERGENCY" | None
```

### 8.9.4 OrchestratorFSM

Pure-logic FSM. No I/O, no side effects. Testable with table-driven fixtures.

```python
class OrchestratorFSM:
    """Centralized lifecycle state machine.

    MUST:
    - Emit TransitionEvent on every state change.
    - Be deterministic: same (state, inputs) -> same (next_state, reason).
    - Never skip states (e.g., INIT -> ACTIVE is illegal).

    MUST NOT:
    - Perform I/O (logging, metrics, network).
    - Hold mutable references to external state.
    """

    state: SystemState
    last_transition: TransitionEvent | None
    state_enter_ts: int

    def tick(self, inputs: OrchestratorInputs) -> TransitionEvent | None:
        """Evaluate inputs and return a TransitionEvent if state changed, else None."""
        ...

    def force(self, to_state: SystemState, reason: TransitionReason, ts_ms: int) -> TransitionEvent:
        """Operator-forced transition. Always succeeds. Returns TransitionEvent."""
        ...
```

---

## 8.10 Invariants (MUST / MUST NOT)

### MUST

1. **Every transition emits a TransitionEvent** — no silent state changes.
2. **TransitionEvent is immutable** — once emitted, never mutated.
3. **Deterministic**: given `(current_state, OrchestratorInputs)`, the output is always the same `(next_state, reason)` or `None`.
4. **EMERGENCY blocks INCREASE_RISK** — consistent with `DrawdownGuardV1` (see Sec 8.11).
5. **DEGRADED blocks INCREASE_RISK** — no new exposure when feeds are stale.
6. **Transition log + metric on every change** — emitted by the caller (not the FSM itself, which is pure).
7. **Ops output includes current state and last transition reason**.

### MUST NOT

1. **FSM MUST NOT perform I/O** — no logging, no metrics emission, no network calls inside `OrchestratorFSM`.
2. **FSM MUST NOT skip states** — e.g., INIT -> ACTIVE is forbidden; must go INIT -> READY -> ACTIVE.
3. **FSM MUST NOT transition without a reason** — every `TransitionEvent` carries a `TransitionReason`.
4. **FSM MUST NOT allow EMERGENCY -> ACTIVE directly** — must go EMERGENCY -> PAUSED first.
5. **FSM MUST NOT hold references to mutable external state** — inputs are passed as frozen snapshots.

---

## 8.11 Action Permissions Matrix

Action permissions are defined in terms of **risk intents** from `src/grinder/risk/drawdown_guard_v1.py::OrderIntent`:

| Intent | Description |
|--------|-------------|
| `INCREASE_RISK` | Orders that increase exposure (new positions, grid entries) |
| `REDUCE_RISK` | Orders that decrease exposure (closes, reduce-only) |
| `CANCEL` | Cancellation of existing orders (always allowed) |

### Permissions by State

| State | INCREASE_RISK | REDUCE_RISK | CANCEL |
|-------|:---:|:---:|:---:|
| **INIT** | BLOCKED | BLOCKED | BLOCKED |
| **READY** | BLOCKED | BLOCKED | ALLOWED |
| **ACTIVE** | ALLOWED | ALLOWED | ALLOWED |
| **THROTTLED** | BLOCKED | ALLOWED | ALLOWED |
| **PAUSED** | BLOCKED | ALLOWED | ALLOWED |
| **DEGRADED** | BLOCKED | ALLOWED | ALLOWED |
| **EMERGENCY** | BLOCKED | ALLOWED | ALLOWED |

**Rules:**
- `CANCEL` is always ALLOWED in every state except INIT (no orders exist).
- `INCREASE_RISK` is ALLOWED only in ACTIVE.
- `REDUCE_RISK` is ALLOWED in every state except INIT and READY (no position to reduce).
- EMERGENCY forces safe behavior: `INCREASE_RISK` is BLOCKED, consistent with `DrawdownGuardV1.evaluate()`.
- The FSM provides `is_action_allowed(state, intent) -> bool` as a pure query.
- **PR-338:** PaperEngine evaluation is deferred during INIT and READY. This prevents ghost
  orders in paper state (via NoOp port) that would freeze reconciliation after ACTIVE
  transition. Actions start flowing only after FSM reaches ACTIVE.

---

## 8.12 Transition Priority and Anti-Flap

### Priority (highest wins)

When multiple triggers fire simultaneously, the FSM applies this priority:

1. **KILL_SWITCH / OPERATOR_EMERGENCY** -> EMERGENCY (highest)
2. **DD_BREACH** -> EMERGENCY
3. **FEED_STALE** -> DEGRADED
4. **OPERATOR_PAUSE** -> PAUSED
5. **TOX_HIGH** -> PAUSED
6. **TOX_MID** -> THROTTLED
7. **Recovery triggers** (TOX_LOW_COOLDOWN, FEED_RECOVERED, etc.) (lowest)

### Anti-Flap / Hysteresis

To prevent rapid oscillation between states:

- **Cooldown timer**: After entering PAUSED or THROTTLED, the FSM MUST NOT transition to a less-restrictive state until `T_COOLDOWN` (configurable, default 30s) has elapsed in the current state.
- **Hysteresis on toxicity**: Recovery from PAUSED requires `tox < TOX_LOW_THRESHOLD` (not just `< TOX_HIGH_THRESHOLD`). Recovery from THROTTLED requires `tox < TOX_LOW_THRESHOLD`.
- **EMERGENCY has no auto-recovery**: Transition out of EMERGENCY requires `POSITION_REDUCED` (position below threshold) — no timeout-based recovery.
- **`time_in_state(ts)`**: The FSM tracks `state_enter_ts` and exposes `time_in_state()` for cooldown evaluation.

---

## 8.13 Observability and Evidence Contract

### Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grinder_fsm_current_state` | Gauge | `state` | Current FSM state (one-hot encoding: 1 for current, 0 for others) |
| `grinder_fsm_state_duration_seconds` | Gauge | `state` | Time in current state |
| `grinder_fsm_transitions_total` | Counter | `from`, `to`, `reason` | Total transitions by from/to/reason |
| `grinder_fsm_action_blocked_total` | Counter | `state`, `intent` | Actions blocked by permission matrix |

### Structured Logs

Every transition emits a structured log entry:

```json
{
  "event": "fsm_transition",
  "ts_ms": 1708387200000,
  "from_state": "ACTIVE",
  "to_state": "EMERGENCY",
  "reason": "DD_BREACH",
  "time_in_prev_state_s": 142.5,
  "evidence_ref": "EVIDENCE_REF mode=fsm evidence_dir=/tmp/grinder_evidence/fsm_20260220_120000"
}
```

### Evidence Artifacts

When transitions involve risk-relevant state changes (-> EMERGENCY, -> DEGRADED, -> PAUSED), the runtime (not the FSM) produces:

- `summary.txt`: transition details, inputs snapshot, action permissions at time of transition
- `sha256sums.txt`: integrity hashes for all evidence files
- `EVIDENCE_REF` line in ops output: `EVIDENCE_REF mode=fsm evidence_dir=<path> summary=<path>/summary.txt sha256sums=<path>/sha256sums.txt`

### Ops Triage Integration

- `ops_fill_triage.sh` (or successor) surfaces FSM state in its output.
- Runbook entry covers: "FSM stuck in EMERGENCY", "FSM flapping", "FSM transition storm".

---

## 8.14 Launch-13 PR Slicing

| PR | Scope | Key Deliverables |
|----|-------|-----------------|
| **PR0** | Spec (this document) | Sections 8.9-8.13, SSOT wiring |
| **PR1** | Contracts + logic + tests | `FsmState`, `TransitionReason`, `TransitionEvent`, `OrchestratorInputs`, `OrchestratorFSM`; table-driven tests; permission matrix tests |
| **PR2** | Runtime wiring + metrics/logs | Wire FSM into live loop; emit metrics + structured logs; integrate with existing `DrawdownGuardV1` |
| **PR3** | Ops docs + runbook + fire drill | Runbook additions; fire drill script; ops triage integration |
