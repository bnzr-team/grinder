# Runbook 17: LIVE_TRADE Connector Enablement (LC-22)

## Scope

**What this runbook enables:**
- `LiveConnectorV0` write operations in `LIVE_TRADE` mode
- Real order placement via `BinanceFuturesPort` (place/cancel/replace)
- Production trading on Binance Futures USDT-M

**What this runbook does NOT cover:**
- Policy changes (grid sizing, risk limits) — see [12_ACTIVE_REMEDIATION](12_ACTIVE_REMEDIATION.md)
- L2 order book integration — not yet implemented
- Reconciliation/remediation — see [11_RECONCILIATION_TRIAGE](11_RECONCILIATION_TRIAGE.md), [12_ACTIVE_REMEDIATION](12_ACTIVE_REMEDIATION.md)
- HA leader election — see [07_HA_OPERATIONS](07_HA_OPERATIONS.md)

---

## Safety Model (SSOT)

### 3 Safety Gates + Required Dependency

ALL must pass for real trades:

| # | Gate | Config/Env | Default | Blocks if |
|---|------|------------|---------|-----------|
| 1 | `armed=True` | `LiveConnectorConfig.armed` | `False` | armed=False |
| 2 | `mode=LIVE_TRADE` | `LiveConnectorConfig.mode` | `READ_ONLY` | mode != LIVE_TRADE |
| 3 | `ALLOW_MAINNET_TRADE=1` | Environment variable | not set | env var missing/falsy |
| - | `futures_port` | `LiveConnectorConfig.futures_port` | `None` | not configured |

**Default mode:** `READ_ONLY` (no writes possible)

**Gate failure:** `ConnectorNonRetryableError` with actionable message

**Reference:** ADR-056 in `docs/DECISIONS.md`

---

## Preflight Checklist

Before enabling LIVE_TRADE, verify each item:

### 1. API Keys
- [ ] Binance API key has **Futures trading permission** enabled
- [ ] API key stored securely (env var or secret manager, never in code)
- [ ] Key rotation procedure documented
- [ ] IP allowlist configured (if using)

### 2. Notional Limits
- [ ] `max_notional_per_order` configured (default: $125)
- [ ] Binance minimum notional met ($100 for USDT-M Futures)
- [ ] See [10_FUTURES_MAINNET_TRADE_SMOKE](10_FUTURES_MAINNET_TRADE_SMOKE.md) for notional calculation

### 3. WebSocket Health
- [ ] `grinder_ws_connected` = 1
- [ ] `grinder_last_tick_ts` is fresh (< 10s old)
- [ ] No reconnect loops (`grinder_ws_reconnect_total` stable)

### 4. HA Mode (if enabled)
- [ ] Current instance is **leader** (check `/readyz` or `grinder_ha_is_leader`)
- [ ] Only leader should have `armed=True`
- [ ] See [07_HA_OPERATIONS](07_HA_OPERATIONS.md) for HA operations

### 5. Stream-Only Sanity
Run in `READ_ONLY` mode first to verify:
- [ ] L1 ticks flowing (`grinder_ticks_received_total` increasing)
- [ ] Feature engine producing updates (if enabled)
- [ ] No error logs in connector

---

## Step-by-Step Enablement

### Step 1: Verify Environment

```bash
# Check required env vars are ready (don't echo secrets!)
echo "BINANCE_API_KEY: ${BINANCE_API_KEY:+set}"
echo "BINANCE_API_SECRET: ${BINANCE_API_SECRET:+set}"
echo "ALLOW_MAINNET_TRADE: ${ALLOW_MAINNET_TRADE:-NOT SET}"
```

### Step 2: Run Dry-Run Smoke Test

Verify gate checks work without real orders:

```bash
PYTHONPATH=src python -m scripts.smoke_lc22_live_trade
```

Expected output:
```
============================================================
LC-22 LIVE_TRADE SMOKE TEST
============================================================

## Dry-run mode: Testing gate checks only

### Gate 1: armed=False should block
PASS: Blocked with: Cannot place_order: armed=False...

### Gate 3: ALLOW_MAINNET_TRADE not set should block
PASS: Blocked with: Cannot place_order: ALLOW_MAINNET_TRADE=1 not set...

============================================================
DRY-RUN COMPLETE: All gate checks passed
============================================================
```

### Step 3: Real Smoke Test (place + cancel)

**WARNING:** This places a real order on mainnet. Use far-from-market price.

```bash
BINANCE_API_KEY=xxx \
BINANCE_API_SECRET=yyy \
ALLOW_MAINNET_TRADE=1 \
PYTHONPATH=src python -m scripts.smoke_lc22_live_trade --confirm LC22_LIVE_TRADE
```

Expected output:
```
============================================================
LC-22 LIVE_TRADE SMOKE TEST
============================================================
...
## Placing order via LiveConnectorV0.place_order()
  symbol: BTCUSDT
  side: BUY
  price: 48500.00 (50% below market)
  quantity: 0.003

### Order placed!
  order_id: grinder_s_BTCUSDT_1_1234567890_1

## Cancelling order via LiveConnectorV0.cancel_order()
  order_id: grinder_s_BTCUSDT_1_1234567890_1

### Order cancelled!
  result: True

============================================================
SMOKE TEST PASSED
============================================================
```

### Step 4: Enable in Production Config

```python
# In your LiveEngine/connector config:
config = LiveConnectorConfig(
    mode=SafeMode.LIVE_TRADE,  # Step 1: explicit mode
    armed=True,                 # Step 2: arm the connector
    futures_port=futures_port,  # Step 3: provide configured port
)

# Environment: ALLOW_MAINNET_TRADE=1
```

### Step 5: Start and Monitor

```bash
# Start with all gates enabled
ALLOW_MAINNET_TRADE=1 python -m grinder.live.run
```

Verify in logs:
```
INFO LiveConnectorV0 connected mode=LIVE_TRADE armed=True
INFO BinanceFuturesPort initialized url=https://fapi.binance.com
```

---

## Expected Logs (Normal Operation)

```
2026-02-08 12:00:00 INFO LiveConnectorV0 connected mode=LIVE_TRADE armed=True
2026-02-08 12:00:01 INFO WebSocket connected to wss://fstream.binance.com/ws
2026-02-08 12:00:02 INFO Received tick BTCUSDT price=97000.50
2026-02-08 12:05:00 INFO place_order symbol=BTCUSDT side=BUY price=96500.00 qty=0.001
2026-02-08 12:05:00 INFO Order placed order_id=grinder_BTCUSDT_1_1234567890
2026-02-08 12:10:00 INFO cancel_order order_id=grinder_BTCUSDT_1_1234567890
2026-02-08 12:10:00 INFO Order cancelled order_id=grinder_BTCUSDT_1_1234567890
```

---

## Rollback

### Immediate Stop (fastest)

```bash
# Option 1: Unset env var (requires restart)
unset ALLOW_MAINNET_TRADE

# Option 2: Change mode in config
config.mode = SafeMode.READ_ONLY  # or PAPER

# Option 3: Disarm
config.armed = False
```

### After Rollback, Verify

- [ ] No new orders placed (check Binance console)
- [ ] Existing orders cancelled (if needed)
- [ ] Logs show `armed=False` or `mode=READ_ONLY`
- [ ] Metrics: `grinder_orders_placed_total` stable

---

## Post-Check Metrics

| Metric | Expected | Problem if |
|--------|----------|------------|
| `grinder_ws_connected` | 1 | 0 for > 30s |
| `grinder_ws_reconnect_total` | stable | increasing rapidly |
| `grinder_ticks_received_total` | increasing | flat |
| `grinder_last_tick_ts` | < 10s old | > 60s old |
| `grinder_orders_placed_total` | increasing (if trading) | — |
| `grinder_orders_cancelled_total` | as expected | — |
| `grinder_connector_errors_total` | 0 or low | high/increasing |

---

## Troubleshooting

### 1. Gate Failure: "armed=False"

**Symptom:** `ConnectorNonRetryableError: Cannot place_order: armed=False`

**Fix:** Set `armed=True` in `LiveConnectorConfig`:
```python
config = LiveConnectorConfig(armed=True, ...)
```

### 2. Gate Failure: "ALLOW_MAINNET_TRADE=1 not set"

**Symptom:** `ConnectorNonRetryableError: Cannot place_order: ALLOW_MAINNET_TRADE=1 not set`

**Fix:** Export env var before starting:
```bash
export ALLOW_MAINNET_TRADE=1
```

### 3. Gate Failure: "futures_port not configured"

**Symptom:** `ConnectorNonRetryableError: Cannot place_order: futures_port not configured`

**Fix:** Provide `BinanceFuturesPort` instance:
```python
futures_port = BinanceFuturesPort(config=port_config, http_client=http_client)
config = LiveConnectorConfig(futures_port=futures_port, ...)
```

### 4. ListenKey Expired / 401 Unauthorized

**Symptom:** `BinanceAPIError: -2015 Invalid listen key`

**Fix:**
- Check API key has correct permissions
- Verify key is not expired/revoked
- Check IP allowlist includes your server

### 5. Minimum Notional Reject

**Symptom:** `BinanceAPIError: -4164 Order's notional must be no smaller than 100`

**Fix:**
- Increase quantity: `notional = price * quantity >= 100`
- See [10_FUTURES_MAINNET_TRADE_SMOKE](10_FUTURES_MAINNET_TRADE_SMOKE.md) for notional calculation

### 6. Not Leader (HA Mode)

**Symptom:** Orders not being placed, no errors

**Check:** `curl http://localhost:9090/readyz` should return `{"role": "leader"}`

**Fix:** Check HA configuration, see [07_HA_OPERATIONS](07_HA_OPERATIONS.md)

### 7. Reconnect Loop

**Symptom:** `grinder_ws_reconnect_total` increasing rapidly

**Check:**
- Network connectivity
- Binance status page
- Rate limits (too many connections)

**Fix:** See [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md) for health triage

---

## References

- ADR-056: LC-22 LIVE_TRADE Write-Path (`docs/DECISIONS.md`)
- [07_HA_OPERATIONS](07_HA_OPERATIONS.md): HA Operations
- [10_FUTURES_MAINNET_TRADE_SMOKE](10_FUTURES_MAINNET_TRADE_SMOKE.md): Futures Mainnet Trade Smoke
- [11_RECONCILIATION_TRIAGE](11_RECONCILIATION_TRIAGE.md): Reconciliation Triage
- `scripts/smoke_lc22_live_trade.py`: Smoke test script
