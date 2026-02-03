"""Mid-bar OHLC construction from snapshot ticks.

Builds deterministic OHLC bars from mid-price stream:
- Bar boundaries aligned to interval (floor division)
- Same tick sequence always produces identical bars
- No synthesized bars for gaps (correct behavior)

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.5.1
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class MidBar:
    """OHLC bar constructed from mid-price ticks.

    All prices stored as Decimal for precision.
    bar_ts is the bar's START timestamp (aligned to interval boundary).
    """

    bar_ts: int  # Bar start timestamp (ms), aligned to interval boundary
    open: Decimal  # First mid_price in bar
    high: Decimal  # Highest mid_price in bar
    low: Decimal  # Lowest mid_price in bar
    close: Decimal  # Last mid_price in bar
    tick_count: int  # Number of ticks in bar

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict with Decimal as string."""
        return {
            "bar_ts": self.bar_ts,
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "tick_count": self.tick_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MidBar:
        """Deserialize from dict."""
        return cls(
            bar_ts=d["bar_ts"],
            open=Decimal(d["open"]),
            high=Decimal(d["high"]),
            low=Decimal(d["low"]),
            close=Decimal(d["close"]),
            tick_count=d["tick_count"],
        )


@dataclass
class _BarAccumulator:
    """Internal accumulator for building a bar."""

    bar_ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_count: int = 1

    def update(self, mid_price: Decimal) -> None:
        """Update bar with new tick."""
        self.high = max(self.high, mid_price)
        self.low = min(self.low, mid_price)
        self.close = mid_price
        self.tick_count += 1

    def finalize(self) -> MidBar:
        """Convert to immutable MidBar."""
        return MidBar(
            bar_ts=self.bar_ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            tick_count=self.tick_count,
        )


@dataclass
class BarBuilderConfig:
    """Configuration for bar building."""

    bar_interval_ms: int = 60_000  # 1 minute bars by default
    max_bars: int = 1000  # Maximum completed bars to keep

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.bar_interval_ms <= 0:
            raise ValueError(f"bar_interval_ms must be positive, got {self.bar_interval_ms}")
        if self.max_bars <= 0:
            raise ValueError(f"max_bars must be positive, got {self.max_bars}")


@dataclass
class BarBuilder:
    """Builds OHLC bars from mid-price ticks.

    Deterministic: same tick sequence always produces same bars.
    Bars are emitted at interval boundaries (floor division).
    """

    config: BarBuilderConfig = field(default_factory=BarBuilderConfig)

    # Internal state
    _current_bar: _BarAccumulator | None = field(default=None, repr=False)
    _completed_bars: deque[MidBar] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize deque with maxlen."""
        if self._completed_bars is None:
            self._completed_bars = deque(maxlen=self.config.max_bars)

    def _align_ts(self, ts: int) -> int:
        """Align timestamp to bar boundary (floor)."""
        return (ts // self.config.bar_interval_ms) * self.config.bar_interval_ms

    def process_tick(self, ts: int, mid_price: Decimal) -> MidBar | None:
        """Process a tick and return completed bar if boundary crossed.

        Args:
            ts: Tick timestamp (ms)
            mid_price: Mid price at this tick

        Returns:
            Completed MidBar if bar boundary crossed, None otherwise.
        """
        bar_ts = self._align_ts(ts)

        if self._current_bar is None:
            # First tick - start new bar
            self._current_bar = _BarAccumulator(
                bar_ts=bar_ts,
                open=mid_price,
                high=mid_price,
                low=mid_price,
                close=mid_price,
            )
            return None

        if bar_ts > self._current_bar.bar_ts:
            # Bar boundary crossed - complete current bar
            completed = self._current_bar.finalize()
            assert self._completed_bars is not None  # set in __post_init__
            self._completed_bars.append(completed)
            # Start new bar
            self._current_bar = _BarAccumulator(
                bar_ts=bar_ts,
                open=mid_price,
                high=mid_price,
                low=mid_price,
                close=mid_price,
            )
            return completed

        # Same bar - accumulate
        self._current_bar.update(mid_price)
        return None

    def get_bars(self, count: int | None = None) -> list[MidBar]:
        """Get completed bars (oldest first).

        Args:
            count: Maximum number of bars to return. None for all.

        Returns:
            List of MidBar, oldest first.
        """
        assert self._completed_bars is not None  # set in __post_init__
        bars = list(self._completed_bars)
        if count is not None:
            return bars[-count:] if len(bars) > count else bars
        return bars

    @property
    def bar_count(self) -> int:
        """Number of completed bars available."""
        assert self._completed_bars is not None  # set in __post_init__
        return len(self._completed_bars)

    def reset(self) -> None:
        """Reset all state."""
        self._current_bar = None
        assert self._completed_bars is not None  # set in __post_init__
        self._completed_bars.clear()
