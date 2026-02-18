"""Fill event tracker (Launch-06 PR1, detect-only).

In-memory tracker for fill-like events. Records counts, quantities,
notional values, fees, and maker/taker + buy/sell splits.

NOT wired into live execution paths in this PR.  Only callable
from tests (and future PRs that connect real fill sources).

Design:
- Frozen FillEvent dataclass (immutable input).
- FillTracker with record() + snapshot() (mutable accumulator).
- FillSnapshot frozen dataclass (immutable output).
- Rejects NaN/Inf in qty/price/fee.
- No file IO, no network.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


class FillValidationError(Exception):
    """Invalid fill event data."""


# Allowed source values (extensible in future PRs)
FILL_SOURCES: frozenset[str] = frozenset({"reconcile", "sim", "manual", "none"})

# Allowed side values
FILL_SIDES: frozenset[str] = frozenset({"buy", "sell", "none"})

# Allowed liquidity values
FILL_LIQUIDITY: frozenset[str] = frozenset({"maker", "taker", "none"})


@dataclass(frozen=True)
class FillEvent:
    """A single fill event.

    Attributes:
        ts_ms: Event timestamp in milliseconds.
        source: Origin of the fill (reconcile, sim, manual, none).
        side: Trade side (buy, sell, none).
        liquidity: Maker or taker (maker, taker, none).
        qty: Fill quantity (>= 0).
        price: Fill price (>= 0).
        fee: Fee amount (>= 0).
        fee_asset: Fee currency (informational only, NOT a metric label).
    """

    ts_ms: int
    source: str
    side: str
    liquidity: str
    qty: float
    price: float
    fee: float
    fee_asset: str = ""


def _validate_fill(event: FillEvent) -> None:
    """Validate fill event fields. Raises FillValidationError on bad data."""
    for name, val in [("qty", event.qty), ("price", event.price), ("fee", event.fee)]:
        if math.isnan(val) or math.isinf(val):
            raise FillValidationError(f"FillEvent.{name} is {val} (NaN/Inf not allowed)")
        if val < 0:
            raise FillValidationError(f"FillEvent.{name} is {val} (must be >= 0)")


@dataclass(frozen=True)
class FillSnapshot:
    """Immutable aggregate snapshot of fill activity.

    Attributes:
        total_fills: Total number of recorded fill events.
        total_qty: Sum of all fill quantities.
        total_notional: Sum of qty * price for all fills.
        total_fees: Sum of all fees.
        buy_fills: Number of buy fills.
        sell_fills: Number of sell fills.
        buy_notional: Total notional for buy fills.
        sell_notional: Total notional for sell fills.
        maker_fills: Number of maker fills.
        taker_fills: Number of taker fills.
        maker_notional: Total notional for maker fills.
        taker_notional: Total notional for taker fills.
    """

    total_fills: int = 0
    total_qty: float = 0.0
    total_notional: float = 0.0
    total_fees: float = 0.0
    buy_fills: int = 0
    sell_fills: int = 0
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    maker_fills: int = 0
    taker_fills: int = 0
    maker_notional: float = 0.0
    taker_notional: float = 0.0


@dataclass
class FillTracker:
    """In-memory fill event accumulator.

    Thread-safe via simple dict/counter operations (GIL protection).
    """

    _total_fills: int = 0
    _total_qty: float = 0.0
    _total_notional: float = 0.0
    _total_fees: float = 0.0
    _buy_fills: int = 0
    _sell_fills: int = 0
    _buy_notional: float = 0.0
    _sell_notional: float = 0.0
    _maker_fills: int = 0
    _taker_fills: int = 0
    _maker_notional: float = 0.0
    _taker_notional: float = 0.0
    # Per-label counters for metrics (source, side, liquidity)
    _fills_by_label: dict[tuple[str, str, str], int] = field(default_factory=dict)
    _notional_by_label: dict[tuple[str, str, str], float] = field(default_factory=dict)
    _fees_by_label: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def record(self, event: FillEvent) -> None:
        """Record a fill event. Raises FillValidationError on bad data."""
        _validate_fill(event)

        notional = event.qty * event.price

        self._total_fills += 1
        self._total_qty += event.qty
        self._total_notional += notional
        self._total_fees += event.fee

        if event.side == "buy":
            self._buy_fills += 1
            self._buy_notional += notional
        elif event.side == "sell":
            self._sell_fills += 1
            self._sell_notional += notional

        if event.liquidity == "maker":
            self._maker_fills += 1
            self._maker_notional += notional
        elif event.liquidity == "taker":
            self._taker_fills += 1
            self._taker_notional += notional

        # Per-label tracking for metrics
        label_key = (event.source, event.side, event.liquidity)
        self._fills_by_label[label_key] = self._fills_by_label.get(label_key, 0) + 1
        self._notional_by_label[label_key] = self._notional_by_label.get(label_key, 0.0) + notional
        self._fees_by_label[label_key] = self._fees_by_label.get(label_key, 0.0) + event.fee

    def snapshot(self) -> FillSnapshot:
        """Return an immutable snapshot of current fill aggregates."""
        return FillSnapshot(
            total_fills=self._total_fills,
            total_qty=self._total_qty,
            total_notional=self._total_notional,
            total_fees=self._total_fees,
            buy_fills=self._buy_fills,
            sell_fills=self._sell_fills,
            buy_notional=self._buy_notional,
            sell_notional=self._sell_notional,
            maker_fills=self._maker_fills,
            taker_fills=self._taker_fills,
            maker_notional=self._maker_notional,
            taker_notional=self._taker_notional,
        )
