# Safety Envelope (TRD-1)

Normative specification of the mainnet safety guarantees.
Contract tests: `tests/unit/test_safety_envelope.py`.
ADR: ADR-076 in `docs/DECISIONS.md`.

## Dry-run contract

**Writes impossible unless** `armed=True AND mode=LIVE_TRADE AND exchange_port=futures`.

| Parameter | Default | Set via | Effect |
|-----------|---------|---------|--------|
| `armed` | `False` | `--armed` CLI flag / `LiveEngineConfig.armed` | Gate 1: blocks ALL actions |
| `mode` | `READ_ONLY` | `GRINDER_TRADING_MODE` env / `LiveEngineConfig.mode` | Gate 2: blocks ALL actions when != LIVE_TRADE |
| `exchange_port` | `noop` | `--exchange-port` CLI flag | NoOpExchangePort: in-memory only, zero HTTP |

All three defaults must be explicitly overridden for any write operation to reach
the exchange.  A single default blocks all writes.

## Engine gate chain

`LiveEngineV0._process_action()` applies gates in strict sequential order.
Each gate returns early on block — later gates are never evaluated.

| # | Gate | Config field | BlockReason | Blocks | Allows |
|---|------|-------------|-------------|--------|--------|
| 1 | Arming | `armed=False` | `NOT_ARMED` | ALL | - |
| 2 | Mode | `mode != LIVE_TRADE` | `MODE_NOT_LIVE_TRADE` | ALL | - |
| 3 | Kill-switch | `kill_switch_active=True` | `KILL_SWITCH_ACTIVE` | PLACE, REPLACE | CANCEL |
| 4 | Symbol whitelist | `symbol_whitelist` (non-empty) | `SYMBOL_NOT_WHITELISTED` | unlisted symbols | listed symbols |
| 5 | Max position | `max_position_usd` (non-None) | `MAX_POSITION_EXCEEDED` | INCREASE_RISK when notional >= cap | CANCEL, REDUCE_RISK |
| 6 | Drawdown guard | `DrawdownGuardV1` state | `DRAWDOWN_BLOCKED` | INCREASE_RISK in DRAWDOWN | CANCEL, REDUCE_RISK |
| 7 | FSM permission | `FsmDriver.check_intent()` | `FSM_STATE_BLOCKED` | actions forbidden by FSM state | allowed intents |
| 8 | Fill probability | `FillModelV0` threshold | `FILL_PROB_LOW` | low-prob PLACE/REPLACE | high-prob or CANCEL |

After all 8 gates pass, SOR routing (not a safety gate) may adjust the order
before execution via the exchange port.

### Ordering rationale

1. **Fail-fast**: cheapest checks first (bool flags before model inference)
2. **Determinism**: same input always hits the same first blocking gate
3. **Operator expectations**: `armed=False` is the master kill — it always wins

Any change to gate ordering requires updating this document, the contract tests,
and ADR-076.

### Position-aware intent classification (PR-INV-1)

`classify_intent(action, pos_sign)` determines INCREASE_RISK vs REDUCE_RISK
based on current position direction:

| pos_sign | Action side | Intent |
|----------|------------|--------|
| +1 (LONG) | SELL | REDUCE_RISK |
| +1 (LONG) | BUY | INCREASE_RISK |
| -1 (SHORT) | BUY | REDUCE_RISK |
| -1 (SHORT) | SELL | INCREASE_RISK |
| None (unknown/BOTH/flat) | any | INCREASE_RISK (fail-closed) |

`pos_sign` is derived from `AccountSnapshot.positions` (hedge-mode only).
In one-way mode (`side="BOTH"`), sign is unknown → fail-closed.

### Gate 5: Max position (PR-INV-1)

Enabled by `GRINDER_MAX_POSITION_USD` environment variable.
Uses `_position_notional_usd` from AccountSyncer (total across all symbols).
When `notional >= cap`, blocks INCREASE_RISK; allows CANCEL and REDUCE_RISK.
Disabled when env var is unset or AccountSync has not run yet.

## ConsecutiveLossGuard (indirect gate)

ConsecutiveLossGuard is **not** in the engine gate chain.  It is wired into
the live reconciliation pipeline (`risk/consecutive_loss_wiring.py`):

1. `ConsecutiveLossService.process_trades()` tracks per-symbol loss streaks
2. On trip (N consecutive losses): sets `GRINDER_OPERATOR_OVERRIDE=PAUSE`
3. FSM reads this env var and transitions to PAUSE state
4. Gate 7 (FSM permission) blocks risk-increasing actions

This is an **indirect** safety mechanism: CLG -> env var -> FSM -> Gate 7.

## BinanceFuturesPort guards (port level)

Even if all engine gates pass, `BinanceFuturesPort` has its own validation:

| Guard | Check | Failure mode |
|-------|-------|-------------|
| Mode validation | `mode != LIVE_TRADE` | `ConnectorNonRetryableError` |
| Symbol whitelist | symbol not in config whitelist | `ConnectorNonRetryableError` |
| Notional limit | `price * qty > max_notional_per_order` | `ConnectorNonRetryableError` |
| Order count | `orders_this_run >= max_orders_per_run` | `ConnectorNonRetryableError` |
| Mainnet gates | `allow_mainnet=True` + `ALLOW_MAINNET_TRADE=1` + non-empty whitelist + max_notional set | Config validation at init |
| dry_run | `dry_run=True` | Returns synthetic result, 0 HTTP calls |

## Smoke verification

`scripts/smoke_futures_no_orders.sh` runs with **all safety gates open**
(`--armed`, `GRINDER_TRADING_MODE=live_trade`, `--exchange-port futures`)
on fixture data with fake API keys.

### What it proves

- Zero order-like network strings in process output (grep for `/fapi/v1/order`, `newOrder`, etc.)
- `grinder_port_order_attempts_total{port="futures"}` = 0 for all ops
- `grinder_port_http_requests_total{port="futures"}` sum = 0
- Process exits cleanly (exit code 0)
- Boot line confirms `port=futures armed=True` (gates are open)

### What it does NOT prove

- That real API credentials would not produce writes (it uses fake keys)
- That kill-switch / drawdown correctly block at runtime (covered by unit tests)
- That network airgap works on non-fixture data (covered by `fixture_guard.py` tests)

The smoke test complements — but does not replace — the contract unit tests.
Together they provide defense-in-depth: unit tests verify gate logic, smoke
verifies zero network I/O end-to-end.
