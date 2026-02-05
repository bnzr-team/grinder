# Runbook: Mainnet Trade Smoke (LC-08b)

## Overview

This runbook documents the procedure for running a budgeted smoke test on Binance **mainnet** to verify live trading connectivity and order flow.

**CAUTION:** This uses REAL money. Multiple safety guards are in place, but proceed carefully.

---

## Prerequisites

### 1. Binance Mainnet Account

1. Log in to your Binance account at: https://www.binance.com/
2. Generate API keys with **Spot trading** permission
3. Note your API key and secret
4. **Recommended:** Use a subaccount or separate account with limited budget

### 2. Test Budget

- Ensure the account has a small test budget (e.g., $50-100 USDT)
- This limits worst-case loss if something goes wrong
- The script uses far-from-market prices to minimize fill risk

### 3. Environment Setup

```bash
# Required for mainnet orders
export BINANCE_API_KEY="your_mainnet_api_key"
export BINANCE_API_SECRET="your_mainnet_api_secret"
export ARMED=1
export ALLOW_MAINNET_TRADE=1
```

### 4. Python Dependencies

```bash
pip install requests
```

---

## Safety Guards (7 Layers)

Before the script places any mainnet order, ALL of these guards must pass:

| # | Guard | Default | Purpose |
|---|-------|---------|---------|
| 1 | `allow_mainnet` config | `False` | Must be explicitly set to `True` |
| 2 | `ALLOW_MAINNET_TRADE` env var | Not set | Must be `1` |
| 3 | `ARMED` env var | Not set | Must be `1` |
| 4 | `symbol_whitelist` | Empty | Must be non-empty for mainnet |
| 5 | `max_notional_per_order` | `None` | Must be set (default: $50) |
| 6 | `max_orders_per_run` | `1` | Single order per script run |
| 7 | API key/secret | Empty | Must be valid credentials |

**If any guard fails → script exits with error, 0 orders placed.**

---

## Smoke Test Procedure

### Step 1: Dry-Run First (Always)

Run in dry-run mode to verify script works:

```bash
PYTHONPATH=src python -m scripts.smoke_live_testnet
```

**Expected output:**

```
============================================================
DRY-RUN MODE (no real orders)
To place real orders:
  Testnet: --confirm TESTNET
  Mainnet: --confirm MAINNET_TRADE
============================================================

Starting smoke test (mode=dry-run, symbol=BTCUSDT)
  Price: 10000.00, Quantity: 0.001
  Notional: $10.00
  SIMULATED Placing limit order: BTCUSDT BUY 0.001 @ 10000.00
  SIMULATED Order placed: SIM_grinder_BTCUSDT_0_...
  SIMULATED Cancelling order: SIM_grinder_BTCUSDT_0_...
  SIMULATED Order cancelled: True

============================================================
SMOKE TEST RESULT: PASS
============================================================
  Mode: dry-run
  ** SIMULATED - No real HTTP calls made **
  Simulated place: OK
  Simulated cancel: OK
============================================================
```

### Step 2: Live Mainnet Order

After dry-run passes, run with real mainnet orders:

```bash
BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \
    PYTHONPATH=src python -m scripts.smoke_live_testnet --confirm MAINNET_TRADE
```

**Expected output:**

```
============================================================
*** LIVE MAINNET MODE ***
Real orders will be placed on Binance MAINNET
Symbol whitelist: [BTCUSDT]
Max notional per order: $50.00
============================================================

Starting smoke test (mode=live-mainnet, symbol=BTCUSDT)
  Price: 10000.00, Quantity: 0.001
  Notional: $10.00
  Max notional: $50.00
  Base URL: https://api.binance.com
  Placing limit order: BTCUSDT BUY 0.001 @ 10000.00
  Order placed: grinder_BTCUSDT_0_1707123456789_1
  Cancelling order: grinder_BTCUSDT_0_1707123456789_1
  Order cancelled: True

============================================================
SMOKE TEST RESULT: PASS
============================================================
  Mode: live-mainnet
  Order placed: True
  Order ID: grinder_BTCUSDT_0_1707123456789_1
  Order cancelled: True
  Details: {'symbol': 'BTCUSDT', 'price': '10000.00', 'quantity': '0.001',
            'notional': '10.00000', 'base_url': 'https://api.binance.com',
            'is_mainnet': True, 'simulated': False}
============================================================
```

### Step 3: Verify on Binance UI

1. Log in to https://www.binance.com/
2. Go to Orders → Order History
3. Verify the order was placed and cancelled
4. If order was filled (unexpected), verify position and P&L

---

## Failure Scenarios

### Missing Environment Variable

```
SMOKE TEST RESULT: FAIL
  Error: Environment guard failed: ALLOW_MAINNET_TRADE=1 not set (required for mainnet)
```

**Resolution:** Set `ALLOW_MAINNET_TRADE=1` in environment.

### Notional Exceeds Limit

```
SMOKE TEST RESULT: FAIL
  Error: Place order failed: Order notional $100.00 exceeds max_notional_per_order $50.00
```

**Resolution:** Reduce `--quantity` or increase `--max-notional`.

### Order Count Exceeded

```
SMOKE TEST RESULT: FAIL
  Error: Place order failed: Order count limit reached: 1 orders per run
```

**Resolution:** This is expected for second order in same run. Create new port instance.

### Connection Error

```
SMOKE TEST RESULT: FAIL
  Error: Place order failed: Connection error: ...
```

**Resolution:** Check network connectivity to api.binance.com.

### Authentication Error

```
SMOKE TEST RESULT: FAIL
  Error: Place order failed: Binance error -2015: Invalid API-key, IP, or permissions for action
```

**Resolution:** Verify API key has Spot trading permission and is not IP-restricted.

---

## Emergency Procedures

### If Order Gets Filled

1. **Don't panic** — the order is far-from-market, fill is unlikely
2. If filled, you now hold a small position (e.g., 0.001 BTC)
3. Log in to Binance UI and sell the position at market

### If Script Hangs

1. Press Ctrl+C to interrupt
2. Log in to Binance UI and check for open orders
3. Manually cancel any open orders

### If Multiple Orders Placed (Bug)

1. Stop the script immediately
2. Log in to Binance UI
3. Cancel all open orders for the symbol
4. Report the issue with full logs

---

## Post-Mortem Bundle

After any mainnet smoke test, save:

1. **Script output** (full stdout, secrets redacted)
2. **Environment state:**
   ```bash
   env | grep -E "BINANCE|ARMED|ALLOW" | sed 's/=.*/=REDACTED/'
   ```
3. **Binance order history** (screenshot or export)
4. **Timestamp** of test run

Store in: `incidents/mainnet_smoke_YYYYMMDD_HHMMSS/`

---

## Operator Checklist

Before running mainnet smoke test:

- [ ] Dry-run passed first
- [ ] API keys are for mainnet (NOT testnet)
- [ ] API keys have Spot trading permission
- [ ] Account has limited test budget (e.g., $50-100)
- [ ] ARMED=1 set
- [ ] ALLOW_MAINNET_TRADE=1 set
- [ ] Network connectivity to api.binance.com verified

After mainnet smoke test:

- [ ] PASS/FAIL recorded
- [ ] Order visible in Binance UI (if live)
- [ ] Order cancelled (if not filled)
- [ ] No unexpected positions
- [ ] Post-mortem bundle saved

---

## Custom Parameters

Adjust defaults for your test budget:

```bash
# Lower notional limit ($20)
PYTHONPATH=src python -m scripts.smoke_live_testnet --confirm MAINNET_TRADE \
    --max-notional 20

# Different symbol
PYTHONPATH=src python -m scripts.smoke_live_testnet --confirm MAINNET_TRADE \
    --symbol ETHUSDT

# Different price/quantity
PYTHONPATH=src python -m scripts.smoke_live_testnet --confirm MAINNET_TRADE \
    --price 1000 --quantity 0.01
```

---

## Related Runbooks

- [08_SMOKE_TEST_TESTNET.md](08_SMOKE_TEST_TESTNET.md) — Testnet smoke test
- [04_KILL_SWITCH.md](04_KILL_SWITCH.md) — Kill-switch behavior

---

## Design Reference

See ADR-039 for safety guard design decisions.
