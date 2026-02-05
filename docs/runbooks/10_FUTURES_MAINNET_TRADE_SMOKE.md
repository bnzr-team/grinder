# Runbook: Futures USDT-M Mainnet Trade Smoke (LC-08b-F)

## Overview

This runbook documents the procedure for running a budgeted smoke test on Binance **Futures USDT-M mainnet** to verify live trading connectivity and order flow.

**CAUTION:** This uses REAL money on FUTURES. Multiple safety guards are in place, but proceed carefully.

---

## Prerequisites

### 1. Binance Futures Account

1. Log in to your Binance account at: https://www.binance.com/
2. Enable Futures trading (requires KYC verification)
3. Generate API keys with **Futures trading** permission
4. Note your API key and secret
5. **Recommended:** Use a subaccount or separate account with limited budget

### 2. Test Budget

- Ensure the account has a small test budget (e.g., $50-100 USDT)
- Margin required: ~$40 at default 3x leverage ($120 notional / 3)
- This limits worst-case loss if something goes wrong
- The script uses far-from-market prices ($40k vs ~$97k market) to minimize fill risk
- Default leverage is 3x to reduce margin requirement; safe because order won't fill

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

## Safety Guards (9 Layers)

Before the script places any mainnet order, ALL of these guards must pass:

| # | Guard | Default | Purpose |
|---|-------|---------|---------|
| 1 | `--dry-run` mode | Active | No real orders unless `--confirm` |
| 2 | `allow_mainnet` config | `False` | Must be explicitly set to `True` |
| 3 | `ALLOW_MAINNET_TRADE` env var | Not set | Must be `1` |
| 4 | `ARMED` env var | Not set | Must be `1` |
| 5 | `symbol_whitelist` | Empty | Must be non-empty for mainnet |
| 6 | `max_notional_per_order` | `None` | Must be set (default: $125, above $100 min) |
| 7 | `max_orders_per_run` | `1` | Single order per script run |
| 8 | `target_leverage` | `3` | Reduces margin req (safe: far-from-market, cancelled) |
| 9 | API key/secret | Empty | Must be valid credentials |

**If any guard fails → script exits with error, 0 orders placed.**

---

## Smoke Test Procedure

### Step 1: Dry-Run First (Always)

Run in dry-run mode to verify script works:

```bash
PYTHONPATH=src python -m scripts.smoke_futures_mainnet
```

**Expected output:**

```
============================================================
DRY-RUN MODE (no real orders)
To place real orders:
  Futures mainnet: --confirm FUTURES_MAINNET_TRADE
============================================================

Starting futures smoke test (mode=dry-run, symbol=BTCUSDT)
  Base URL: https://fapi.binance.com
  Price: 40000.00, Quantity: 0.003
  Notional: $120.00
  Max notional: $125.00
  Target leverage: 3x

  [Step 1] Getting account info...
  Position mode: one-way

  [Step 2] Setting leverage to 3x...
  Leverage set to: 3x

  [Step 3] Checking existing position...
  No existing position

  [Step 4] Placing limit order: BTCUSDT BUY 0.003 @ 40000.00
  Order placed: grinder_BTCUSDT_0_...

  [Step 5] Cancelling order: grinder_BTCUSDT_0_...
  Order cancelled: True

  [Step 6] Checking for position to close...
  No position to close

  [Step 7] Final position check...
  Position is 0 (clean)

============================================================
FUTURES SMOKE TEST RESULT: PASS
============================================================
  Mode: dry-run
  ** SIMULATED - No real HTTP calls made **
============================================================
```

### Step 2: Live Futures Mainnet Order

After dry-run passes, run with real mainnet orders:

```bash
BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \
    PYTHONPATH=src python -m scripts.smoke_futures_mainnet --confirm FUTURES_MAINNET_TRADE
```

**Expected output:**

```
============================================================
*** LIVE FUTURES MAINNET MODE ***
Real orders will be placed on Binance Futures USDT-M
Symbol whitelist: [BTCUSDT]
Max notional per order: $50.00
Target leverage: 1x
============================================================

Starting futures smoke test (mode=live-futures-mainnet, symbol=BTCUSDT)
  Base URL: https://fapi.binance.com
  ...
  [Step 1] Getting account info...
  Position mode: one-way

  [Step 2] Setting leverage to 1x...
  Leverage set to: 1x

  [Step 3] Checking existing position...
  No existing position

  [Step 4] Placing limit order: BTCUSDT BUY 0.001 @ 80000.00
  Order placed: 123456789

  [Step 5] Cancelling order: 123456789
  Order cancelled: True

  [Step 6] Checking for position to close...
  No position to close

  [Step 7] Final position check...
  Position is 0 (clean)

============================================================
FUTURES SMOKE TEST RESULT: PASS
============================================================
  Mode: live-futures-mainnet
  Position mode: one-way
  Target leverage: 1x
  Actual leverage: 1x
  Order placed: True
  Binance order ID: 123456789
  Order cancelled: True
============================================================
```

### Step 3: Verify on Binance UI

1. Log in to https://www.binance.com/
2. Go to Derivatives → USDT-M Futures → Order History
3. Verify the order was placed and cancelled
4. Check Positions tab → should be empty (no position)
5. If position exists (unexpected), close it manually

---

## Failure Scenarios

### Missing Environment Variable

```
FUTURES SMOKE TEST RESULT: FAIL
  Error: Environment guard failed: ALLOW_MAINNET_TRADE=1 not set (required for futures mainnet)
```

**Resolution:** Set `ALLOW_MAINNET_TRADE=1` in environment.

### Notional Exceeds Limit

```
FUTURES SMOKE TEST RESULT: FAIL
  Error: Place order failed: Order notional $200.00 exceeds max_notional_per_order $125.00
```

**Resolution:** Reduce `--quantity` or increase `--max-notional`.

### Notional Below Binance Minimum

```
FUTURES SMOKE TEST RESULT: FAIL
  Error: Place order failed: Binance error -4164: Order's notional must be no smaller than 100
```

**Resolution:** BTCUSDT requires $100+ notional. Increase quantity or price.

### Leverage Error

```
FUTURES SMOKE TEST RESULT: FAIL
  Error: Leverage must be 1-125, got 0
```

**Resolution:** Use valid leverage value (default: 1).

### Position Not Closed

```
  [Step 7] Final position check...
  WARNING: Remaining position: 0.001
```

**Resolution:** Order was filled. Close position manually on Binance UI.

---

## Emergency Procedures

### If Order Gets Filled

1. **Don't panic** — the order is far-from-market, fill is unlikely
2. If filled, the script attempts automatic position cleanup
3. If cleanup fails, log in to Binance UI
4. Go to Derivatives → USDT-M Futures → Positions
5. Close the position with market order

### If Script Hangs

1. Press Ctrl+C to interrupt
2. Log in to Binance UI and check for open orders
3. Cancel any open orders
4. Check Positions tab and close any positions

### If Multiple Orders Placed (Bug)

1. Stop the script immediately
2. Log in to Binance UI
3. Cancel all open orders for the symbol
4. Close any positions
5. Report the issue with full logs

---

## Post-Mortem Bundle

After any futures mainnet smoke test, save:

1. **Script output** (full stdout, secrets redacted)
2. **Environment state:**
   ```bash
   env | grep -E "BINANCE|ARMED|ALLOW" | sed 's/=.*/=REDACTED/'
   ```
3. **Binance order history** (screenshot or export from Futures Order History)
4. **Position check** (screenshot of Positions tab)
5. **Timestamp** of test run

Store in: `incidents/futures_smoke_YYYYMMDD_HHMMSS/`

---

## Operator Checklist

Before running futures mainnet smoke test:

- [ ] Dry-run passed first
- [ ] API keys are for mainnet (NOT testnet)
- [ ] API keys have **Futures trading** permission
- [ ] Account has limited test budget (e.g., $50-100)
- [ ] ARMED=1 set
- [ ] ALLOW_MAINNET_TRADE=1 set
- [ ] Network connectivity to fapi.binance.com verified
- [ ] No existing positions on the symbol

After futures mainnet smoke test:

- [ ] PASS/FAIL recorded
- [ ] Order visible in Futures Order History (if live)
- [ ] Order cancelled (if not filled)
- [ ] No positions remaining (Positions tab empty)
- [ ] Post-mortem bundle saved

---

## Custom Parameters

Adjust defaults for your test budget:

```bash
# Lower notional limit ($20)
PYTHONPATH=src python -m scripts.smoke_futures_mainnet --confirm FUTURES_MAINNET_TRADE \
    --max-notional 20

# Different symbol
PYTHONPATH=src python -m scripts.smoke_futures_mainnet --confirm FUTURES_MAINNET_TRADE \
    --symbol ETHUSDT

# Different price/quantity
PYTHONPATH=src python -m scripts.smoke_futures_mainnet --confirm FUTURES_MAINNET_TRADE \
    --price 90000 --quantity 0.0005

# Different leverage (NOT recommended for smoke test)
PYTHONPATH=src python -m scripts.smoke_futures_mainnet --confirm FUTURES_MAINNET_TRADE \
    --leverage 2
```

---

## Related Runbooks

- [08_SMOKE_TEST_TESTNET.md](08_SMOKE_TEST_TESTNET.md) — Spot testnet smoke test
- [09_MAINNET_TRADE_SMOKE.md](09_MAINNET_TRADE_SMOKE.md) — Spot mainnet smoke test
- [04_KILL_SWITCH.md](04_KILL_SWITCH.md) — Kill-switch behavior

---

## Design Reference

See ADR-040 for safety guard design decisions.
