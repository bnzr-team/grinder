"""Live trading data types.

This module defines data structures for the live data plane:
- LiveFeaturesUpdate: Output from the live feed pipeline
- WsMessage: Raw WebSocket message wrapper

See ADR-037 for design decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grinder.features.types import FeatureSnapshot


@dataclass(frozen=True)
class LiveFeaturesUpdate:
    """Output from live feed pipeline.

    Contains the computed features from a single market data tick,
    along with metadata about the pipeline state.

    Attributes:
        ts: Timestamp of the update (ms)
        symbol: Trading symbol
        features: Computed feature snapshot
        bar_completed: Whether a new bar was completed on this tick
        bars_available: Number of completed bars available
        is_warmed_up: Whether enough bars for full feature computation
        latency_ms: Processing latency (ws receive → features computed)
    """

    ts: int
    symbol: str
    features: FeatureSnapshot
    bar_completed: bool = False
    bars_available: int = 0
    is_warmed_up: bool = False
    latency_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "features": self.features.to_dict(),
            "bar_completed": self.bar_completed,
            "bars_available": self.bars_available,
            "is_warmed_up": self.is_warmed_up,
            "latency_ms": self.latency_ms,
        }


@dataclass
class WsMessage:
    """Raw WebSocket message wrapper.

    Attributes:
        data: Raw message data (JSON string or dict)
        recv_ts: Local timestamp when message was received (ms)
        stream: Stream name (e.g., "btcusdt@bookTicker")
    """

    data: dict[str, Any]
    recv_ts: int
    stream: str = ""

    @classmethod
    def from_binance_bookticker(cls, data: dict[str, Any], recv_ts: int) -> WsMessage:
        """Create from Binance bookTicker message.

        Expected format:
        {
            "u": 400900217,     # order book updateId
            "s": "BTCUSDT",     # symbol
            "b": "25.35190000", # best bid price
            "B": "31.21000000", # best bid qty
            "a": "25.36520000", # best ask price
            "A": "40.66000000"  # best ask qty
        }
        """
        return cls(
            data=data,
            recv_ts=recv_ts,
            stream=f"{data.get('s', '').lower()}@bookTicker",
        )


@dataclass
class BookTickerData:
    """Parsed Binance bookTicker data.

    Attributes:
        symbol: Trading symbol (e.g., "BTCUSDT")
        bid_price: Best bid price
        bid_qty: Best bid quantity
        ask_price: Best ask price
        ask_qty: Best ask quantity
        update_id: Order book update ID
    """

    symbol: str
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    update_id: int

    @classmethod
    def from_ws_message(cls, msg: WsMessage) -> BookTickerData:
        """Parse from WsMessage containing bookTicker data."""
        d = msg.data
        return cls(
            symbol=d["s"],
            bid_price=Decimal(d["b"]),
            bid_qty=Decimal(d["B"]),
            ask_price=Decimal(d["a"]),
            ask_qty=Decimal(d["A"]),
            update_id=d.get("u", 0),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "symbol": self.symbol,
            "bid_price": str(self.bid_price),
            "bid_qty": str(self.bid_qty),
            "ask_price": str(self.ask_price),
            "ask_qty": str(self.ask_qty),
            "update_id": self.update_id,
        }


@dataclass
class LiveFeedStats:
    """Statistics for live feed pipeline.

    Attributes:
        ticks_received: Total ticks received from WS
        ticks_processed: Ticks successfully processed
        bars_completed: Total bars completed
        errors: Error count
        last_ts: Last processed timestamp
        avg_latency_ms: Average processing latency
    """

    ticks_received: int = 0
    ticks_processed: int = 0
    bars_completed: int = 0
    errors: int = 0
    last_ts: int = 0
    avg_latency_ms: float = 0.0
    _latency_sum: float = field(default=0.0, repr=False)

    def record_tick(self, latency_ms: float | None = None) -> None:
        """Record a processed tick."""
        self.ticks_received += 1
        self.ticks_processed += 1
        if latency_ms is not None:
            self._latency_sum += latency_ms
            self.avg_latency_ms = self._latency_sum / self.ticks_processed

    def record_bar(self) -> None:
        """Record a completed bar."""
        self.bars_completed += 1

    def record_error(self) -> None:
        """Record an error."""
        self.errors += 1

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ticks_received": self.ticks_received,
            "ticks_processed": self.ticks_processed,
            "bars_completed": self.bars_completed,
            "errors": self.errors,
            "last_ts": self.last_ts,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
        }
