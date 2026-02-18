"""Fill ingestion: Binance userTrades -> FillTracker + FillMetrics (Launch-06 PR2).

Converts raw Binance Futures ``/fapi/v1/userTrades`` response dicts into
``FillEvent`` objects, records them in the ``FillTracker``, pushes counters
to ``FillMetrics``, and advances the persistent cursor.

This module is **read-only** — it never places, cancels, or modifies orders.
It is called once per reconcile iteration from ``fetch_snapshot()``.

Label safety:
- ``source`` is always ``"reconcile"`` (hardcoded).
- ``side`` comes from ``trade["side"].lower()`` (buy / sell).
- ``liquidity`` comes from ``trade["maker"]`` (maker / taker).
- No ``symbol``, ``order_id``, or other high-cardinality labels.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from grinder.execution.fill_tracker import FILL_LIQUIDITY, FILL_SIDES, FillEvent, FillTracker

if TYPE_CHECKING:
    from grinder.execution.fill_cursor import FillCursor

logger = logging.getLogger(__name__)

# Hardcoded source for reconcile-ingested fills
_SOURCE = "reconcile"


def parse_binance_trade(raw: dict[str, Any]) -> FillEvent | None:
    """Parse a single Binance Futures userTrade dict into a FillEvent.

    Returns None if the trade has invalid/missing fields (logged as warning).

    Binance response fields used:
        - ``time``   (int ms) -> ts_ms
        - ``side``   ("BUY"/"SELL") -> side (lowercased)
        - ``maker``  (bool) -> liquidity ("maker"/"taker")
        - ``qty``    (str float) -> qty
        - ``price``  (str float) -> price
        - ``commission``     (str float) -> fee
        - ``commissionAsset`` (str) -> fee_asset
    """
    try:
        side = str(raw.get("side", "")).lower()
        if side not in FILL_SIDES or side == "none":
            side = "none"

        is_maker = raw.get("maker", False)
        liquidity = "maker" if is_maker else "taker"
        if liquidity not in FILL_LIQUIDITY:
            liquidity = "none"

        return FillEvent(
            ts_ms=int(raw["time"]),
            source=_SOURCE,
            side=side,
            liquidity=liquidity,
            qty=float(raw["qty"]),
            price=float(raw["price"]),
            fee=float(raw["commission"]),
            fee_asset=str(raw.get("commissionAsset", "")),
        )
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("FILL_PARSE_ERROR", extra={"error": str(e), "trade_id": raw.get("id")})
        return None


def ingest_fills(
    raw_trades: list[dict[str, Any]],
    tracker: FillTracker,
    cursor: FillCursor,
) -> int:
    """Parse raw trades, record in tracker, advance cursor.

    Only processes trades with ``id > cursor.last_trade_id`` to guarantee
    deduplication even when Binance returns overlapping pages.

    Args:
        raw_trades: Raw dicts from ``port.fetch_user_trades_raw()``.
        tracker: FillTracker to record events into.
        cursor: Mutable cursor — updated in-place with the newest trade ID/ts.

    Returns:
        Number of new fills recorded.
    """
    recorded = 0
    for raw in raw_trades:
        trade_id = int(raw.get("id", 0))
        if trade_id <= cursor.last_trade_id:
            continue

        event = parse_binance_trade(raw)
        if event is None:
            continue

        tracker.record(event)
        recorded += 1

        # Advance cursor
        if trade_id > cursor.last_trade_id:
            cursor.last_trade_id = trade_id
            cursor.last_ts_ms = event.ts_ms

    if recorded:
        logger.info(
            "FILLS_INGESTED",
            extra={"count": recorded, "cursor_trade_id": cursor.last_trade_id},
        )

    return recorded


def push_tracker_to_metrics(
    tracker: FillTracker,
    fill_metrics: Any,
) -> None:
    """Push per-label counters from tracker to FillMetrics.

    Reads the tracker's internal per-label dicts and records deltas into
    FillMetrics.  This is called after ``ingest_fills()`` so that the
    Prometheus counters reflect the latest state.

    Note: FillMetrics is a cumulative counter.  We push the full tracker
    state each time (tracker is also cumulative).  To avoid double-counting,
    FillMetrics must be reset before each push, OR we track deltas.
    We choose the simpler approach: FillMetrics mirrors tracker totals by
    replacing its state each cycle.
    """
    # Replace FillMetrics state with tracker's per-label totals
    fill_metrics.fills = dict(tracker._fills_by_label)
    fill_metrics.notional = dict(tracker._notional_by_label)
    fill_metrics.fees = dict(tracker._fees_by_label)
