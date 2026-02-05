# Runbook: Testnet Smoke Test

## Overview

This runbook documents the procedure for running a smoke test on Binance Testnet to verify live trading connectivity and order flow.

**Scope:** Testnet only. Mainnet is forbidden in this procedure.

---

## Prerequisites

### 1. Binance Testnet Account

1. Create a testnet account at: https://testnet.binance.vision/
2. Generate API keys (testnet keys, NOT mainnet)
3. Note your API key and secret

**Important:** Binance testnet may require KYC verification for API key generation. If you cannot obtain testnet credentials, run the smoke test in dry-run mode only (see Step 1 below).

### 2. Environment Setup

```bash
# Required for real testnet orders
export BINANCE_API_KEY="your_testnet_api_key"
export BINANCE_API_SECRET="your_testnet_api_secret"
export ARMED=1
export ALLOW_TESTNET_TRADE=1
```

### 3. Python Dependencies

```bash
pip install requests
```

---

## Smoke Test Procedure

### Step 1: Dry-Run (Recommended First)

Run in dry-run mode to verify script works without making real API calls:

```bash
PYTHONPATH=src python -m scripts.smoke_live_testnet
```

**Expected output:**

```
============================================================
DRY-RUN MODE (no real orders)
To place real orders on testnet, use: --confirm TESTNET
============================================================

Starting smoke test (mode=dry-run, symbol=BTCUSDT)
  Price: 10000.00, Quantity: 0.001
  Placing limit order: BTCUSDT BUY 0.001 @ 10000.00
  Order placed: grinder_BTCUSDT_0_...
  Cancelling order: grinder_BTCUSDT_0_...
  Order cancelled: True

============================================================
SMOKE TEST RESULT: PASS
============================================================
  Mode: dry-run
  Order placed: True
  Order ID: grinder_BTCUSDT_0_...
  Order cancelled: True
============================================================
```

### Step 2: Live Testnet Order

After dry-run passes, run with real testnet orders:

```bash
PYTHONPATH=src python -m scripts.smoke_live_testnet --confirm TESTNET
```

**Expected output:**

```
============================================================
LIVE TESTNET MODE
Real orders will be placed on Binance Testnet
============================================================

Starting smoke test (mode=live-testnet, symbol=BTCUSDT)
  Price: 10000.00, Quantity: 0.001
  Placing limit order: BTCUSDT BUY 0.001 @ 10000.00
  Order placed: grinder_BTCUSDT_0_1707123456789_1
  Cancelling order: grinder_BTCUSDT_0_1707123456789_1
  Order cancelled: True

============================================================
SMOKE TEST RESULT: PASS
============================================================
  Mode: live-testnet
  Order placed: True
  Order ID: grinder_BTCUSDT_0_1707123456789_1
  Order cancelled: True
============================================================
```

### Step 3: Verify on Testnet UI

1. Log in to https://testnet.binance.vision/
2. Go to Orders → Order History
3. Verify the order was placed and cancelled

---

## Kill-Switch Verification

Test that kill-switch correctly blocks order placement:

```bash
PYTHONPATH=src python -m scripts.smoke_live_testnet --kill-switch
```

**Expected output:**

```
Starting smoke test (mode=dry-run, symbol=BTCUSDT)
  Kill-switch is ACTIVE - PLACE blocked, CANCEL allowed

============================================================
SMOKE TEST RESULT: PASS
============================================================
  Mode: dry-run
  Order placed: False
  Error: Kill-switch active - order placement blocked (expected)
============================================================
```

---

## Failure Scenarios

### Missing API Keys

```
SMOKE TEST RESULT: FAIL
  Error: Environment guard failed: BINANCE_API_KEY not set
```

**Resolution:** Set BINANCE_API_KEY and BINANCE_API_SECRET environment variables.

### Not Armed

```
SMOKE TEST RESULT: FAIL
  Error: Environment guard failed: ARMED=1 not set
```

**Resolution:** Set `ARMED=1` to enable trading.

### Testnet Permission Not Set

```
SMOKE TEST RESULT: FAIL
  Error: Environment guard failed: ALLOW_TESTNET_TRADE=1 not set
```

**Resolution:** Set `ALLOW_TESTNET_TRADE=1` to confirm testnet trading.

### Order Placement Failed

```
SMOKE TEST RESULT: FAIL
  Error: Place order failed: Binance error -1013: ...
```

**Resolution:** Check price/quantity against symbol filters. Use testnet UI to verify account has funds.

### Connection Error

```
SMOKE TEST RESULT: FAIL
  Error: Place order failed: Connection error: ...
```

**Resolution:** Check network connectivity to testnet.binance.vision.

---

## Post-Mortem Bundle

After any smoke test (pass or fail), save:

1. **Script output** (full stdout)
2. **Environment state:**
   ```bash
   env | grep -E "BINANCE|ARMED|ALLOW"
   ```
3. **Testnet order history** (screenshot or export)
4. **Timestamp** of test run

Store in: `incidents/smoke_test_YYYYMMDD_HHMMSS/`

---

## Operator Checklist

Before running smoke test:

- [ ] Testnet API keys configured (NOT mainnet)
- [ ] ARMED=1 set (if running live)
- [ ] ALLOW_TESTNET_TRADE=1 set (if running live)
- [ ] Dry-run passed first
- [ ] Network connectivity to testnet.binance.vision verified

After smoke test:

- [ ] PASS/FAIL recorded
- [ ] Order visible in testnet UI (if live)
- [ ] Order cancelled (if not filled)
- [ ] Post-mortem bundle saved (if failure)

---

## Safety Notes

1. **Mainnet is FORBIDDEN** - The script blocks any mainnet URL
2. **Dry-run by default** - No real orders without --confirm TESTNET
3. **Multiple guards required** - ARMED + ALLOW_TESTNET_TRADE + keys
4. **Kill-switch respected** - PLACE blocked when kill-switch active
5. **Micro lots only** - Default 0.001 BTC (~$50 at current prices)
6. **Far-from-market price** - Default $10,000 won't fill

---

## Related Runbooks

- [04_KILL_SWITCH.md](04_KILL_SWITCH.md) - Kill-switch detection and recovery
- [01_STARTUP_SHUTDOWN.md](01_STARTUP_SHUTDOWN.md) - Starting/stopping the system
