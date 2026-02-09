"""L2 order book feature indicators.

Implements SPEC_V2_0.md §B formulas:
- Impact-Lite (VWAP slippage) §B.3
- Wall Score §B.4
- Depth imbalance

All calculations use Decimal for precision, output as integer for determinism.

See: docs/smart_grid/SPEC_V2_0.md Addendum B
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from grinder.replay.l2_snapshot import IMPACT_INSUFFICIENT_DEPTH_BPS, QTY_REF_BASELINE

if TYPE_CHECKING:
    from grinder.replay.l2_snapshot import BookLevel


def compute_depth_totals(
    bids: tuple[BookLevel, ...],
    asks: tuple[BookLevel, ...],
) -> tuple[Decimal, Decimal]:
    """Compute total depth on bid and ask sides.

    Args:
        bids: Bid levels sorted descending by price
        asks: Ask levels sorted ascending by price

    Returns:
        (bid_total_qty, ask_total_qty)
    """
    bid_total = sum((level.qty for level in bids), Decimal("0"))
    ask_total = sum((level.qty for level in asks), Decimal("0"))
    return bid_total, ask_total


def compute_depth_imbalance_bps(
    bids: tuple[BookLevel, ...],
    asks: tuple[BookLevel, ...],
) -> int:
    """Compute depth imbalance in integer basis points.

    imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth + eps)

    Positive = bid pressure (more buyers)
    Negative = ask pressure (more sellers)

    Args:
        bids: Bid levels sorted descending by price
        asks: Ask levels sorted ascending by price

    Returns:
        Imbalance in integer bps [-10000, 10000]
    """
    bid_total, ask_total = compute_depth_totals(bids, asks)
    eps = Decimal("1e-8")
    denom = bid_total + ask_total + eps
    imbalance = (bid_total - ask_total) / denom
    return round(imbalance * Decimal("10000"))


def compute_impact_buy_bps(
    asks: tuple[BookLevel, ...],
    qty_ref: Decimal = QTY_REF_BASELINE,
) -> int:
    """Compute buy-side VWAP slippage in bps from best ask.

    Walks the ask book to fill qty_ref and computes VWAP slippage.

    Args:
        asks: Ask levels sorted ascending by price
        qty_ref: Reference quantity to fill (default QTY_REF_BASELINE)

    Returns:
        Impact in integer bps, or IMPACT_INSUFFICIENT_DEPTH_BPS if depth exhausted

    See: SPEC_V2_0.md §B.3
    """
    if not asks:
        return IMPACT_INSUFFICIENT_DEPTH_BPS

    best_ask = asks[0].price
    remaining = qty_ref
    cost = Decimal("0")

    for level in asks:
        if remaining <= 0:
            break
        fill = min(remaining, level.qty)
        cost += fill * level.price
        remaining -= fill

    if remaining > 0:
        return IMPACT_INSUFFICIENT_DEPTH_BPS

    vwap = cost / qty_ref
    slippage_bps = (vwap - best_ask) / best_ask * Decimal("10000")
    return round(slippage_bps)


def compute_impact_sell_bps(
    bids: tuple[BookLevel, ...],
    qty_ref: Decimal = QTY_REF_BASELINE,
) -> int:
    """Compute sell-side VWAP slippage in bps from best bid.

    Walks the bid book to fill qty_ref and computes VWAP slippage.

    Args:
        bids: Bid levels sorted descending by price
        qty_ref: Reference quantity to fill (default QTY_REF_BASELINE)

    Returns:
        Impact in integer bps, or IMPACT_INSUFFICIENT_DEPTH_BPS if depth exhausted

    See: SPEC_V2_0.md §B.3
    """
    if not bids:
        return IMPACT_INSUFFICIENT_DEPTH_BPS

    best_bid = bids[0].price
    remaining = qty_ref
    proceeds = Decimal("0")

    for level in bids:
        if remaining <= 0:
            break
        fill = min(remaining, level.qty)
        proceeds += fill * level.price
        remaining -= fill

    if remaining > 0:
        return IMPACT_INSUFFICIENT_DEPTH_BPS

    vwap = proceeds / qty_ref
    slippage_bps = (best_bid - vwap) / best_bid * Decimal("10000")
    return round(slippage_bps)


def compute_wall_score_x1000(levels: tuple[BookLevel, ...]) -> int:
    """Compute wall score as max_qty / median_qty, stored as x1000 integer.

    Wall score detects unusually large orders relative to the book.

    Args:
        levels: Book levels for one side (bids or asks)

    Returns:
        Wall score * 1000, rounded to integer (1000 = no wall)

    See: SPEC_V2_0.md §B.4
    """
    if len(levels) < 3:
        return 1000  # Default: no wall detected

    quantities = sorted(level.qty for level in levels)
    n = len(quantities)

    if n % 2 == 1:
        median_qty = quantities[n // 2]
    else:
        median_qty = (quantities[n // 2 - 1] + quantities[n // 2]) / 2

    if median_qty <= 0:
        return 1000

    max_qty = max(level.qty for level in levels)
    wall_score = max_qty / median_qty
    return round(wall_score * Decimal("1000"))
