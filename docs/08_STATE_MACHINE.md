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
> **Not implemented yet:**
> - Centralized `StateMachine` orchestrator class
> - Explicit INIT / READY / DEGRADED state handlers
> - State persistence (save/load across restarts)
> - State guards (`can_enter`, `can_exit` validation)
> - Multi-symbol state coordination
> - On-enter / on-exit actions as formal hooks
>
> **Tracking:** Regime classification covers core needs. Formal FSM orchestrator is post-launch.
> This spec describes target state beyond current implementation.

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
