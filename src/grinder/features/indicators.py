"""Technical indicators for feature computation.

Implements:
- ATR/NATR (Average True Range / Normalized ATR)
- L1 microstructure features (imbalance, thin side)
- Range/trend indicators

All calculations use Decimal for precision, output as integer bps for determinism.

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.5.2, §17.5.3, §17.5.5
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.features.bar import MidBar


def compute_true_range(bar: MidBar, prev_close: Decimal) -> Decimal:
    """Compute True Range for a bar.

    TR = max(high - low, abs(high - prev_close), abs(low - prev_close))

    Args:
        bar: Current bar
        prev_close: Previous bar's close price

    Returns:
        True Range as Decimal
    """
    hl = bar.high - bar.low
    hpc = abs(bar.high - prev_close)
    lpc = abs(bar.low - prev_close)
    return max(hl, hpc, lpc)


def compute_atr(bars: list[MidBar], period: int = 14) -> Decimal | None:
    """Compute Average True Range over period bars.

    Uses simple moving average of True Ranges.
    Requires period+1 bars (need prev_close for first TR).

    Args:
        bars: List of MidBars (oldest first)
        period: ATR period (default 14)

    Returns:
        ATR as Decimal, or None if insufficient data
    """
    if len(bars) < period + 1:
        return None

    trs: list[Decimal] = []
    for i in range(1, len(bars)):
        tr = compute_true_range(bars[i], bars[i - 1].close)
        trs.append(tr)

    # Simple moving average of last `period` TRs
    recent_trs = trs[-period:]
    return sum(recent_trs) / Decimal(period)


def compute_natr_bps(bars: list[MidBar], period: int = 14) -> int:
    """Compute Normalized ATR in integer basis points.

    NATR = ATR / close

    Args:
        bars: List of MidBars (oldest first)
        period: ATR period (default 14)

    Returns:
        NATR in integer bps (e.g., 100 = 1%), 0 if insufficient data
    """
    if len(bars) < period + 1:
        return 0

    atr = compute_atr(bars, period)
    if atr is None:
        return 0

    close = bars[-1].close
    if close == 0:
        return 0

    natr = atr / close
    return int((natr * Decimal("10000")).quantize(Decimal("1")))


def compute_imbalance_l1_bps(bid_qty: Decimal, ask_qty: Decimal) -> int:
    """Compute L1 order book imbalance in integer basis points.

    imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty + eps)

    Positive = bid pressure (more buyers)
    Negative = ask pressure (more sellers)

    Args:
        bid_qty: Best bid quantity
        ask_qty: Best ask quantity

    Returns:
        Imbalance in integer bps [-10000, 10000]
    """
    eps = Decimal("1e-8")
    denom = bid_qty + ask_qty + eps
    imbalance = (bid_qty - ask_qty) / denom
    return int((imbalance * Decimal("10000")).quantize(Decimal("1")))


def compute_thin_l1(bid_qty: Decimal, ask_qty: Decimal) -> Decimal:
    """Compute thin side depth (minimum of bid/ask quantity).

    Low value indicates one-sided liquidity (potential for slippage).

    Args:
        bid_qty: Best bid quantity
        ask_qty: Best ask quantity

    Returns:
        Minimum of bid_qty and ask_qty
    """
    return min(bid_qty, ask_qty)


def compute_range_trend(bars: list[MidBar], horizon: int = 14) -> tuple[int, int, int]:
    """Compute range/trend indicators over horizon bars.

    Args:
        bars: List of MidBars (oldest first)
        horizon: Number of bars for calculation (default 14)

    Returns:
        (sum_abs_returns_bps, net_return_bps, range_score)

        sum_abs_returns_bps: Sum of |return_i| for each bar in bps
        net_return_bps: |p_end/p_start - 1| in bps
        range_score: sum_abs_returns / (net_return + 1)

        High range_score = choppy (lots of movement, little net progress)
        Low range_score = trending (movement aligned with direction)
    """
    if len(bars) < horizon + 1:
        return (0, 0, 0)

    recent_bars = bars[-(horizon + 1) :]

    # Sum of absolute returns
    sum_abs = Decimal("0")
    for i in range(1, len(recent_bars)):
        prev_close = recent_bars[i - 1].close
        curr_close = recent_bars[i].close
        if prev_close > 0:
            ret = abs((curr_close - prev_close) / prev_close)
            sum_abs += ret

    # Net return
    start_close = recent_bars[0].close
    end_close = recent_bars[-1].close
    net_ret = Decimal("0")
    if start_close > 0:
        net_ret = abs((end_close / start_close) - 1)

    # Convert to integer bps
    sum_abs_bps = int((sum_abs * Decimal("10000")).quantize(Decimal("1")))
    net_ret_bps = int((net_ret * Decimal("10000")).quantize(Decimal("1")))

    # Range score: sum_abs / (net_ret + 1) to avoid div by zero
    # Using +1 bps rather than epsilon for cleaner integer math
    range_score = sum_abs_bps // (net_ret_bps + 1)

    return (sum_abs_bps, net_ret_bps, range_score)
