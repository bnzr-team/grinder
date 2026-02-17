"""Data quality detectors (Launch-03, detect-only).

Provides:
- DataQualityConfig: Frozen config with conservative defaults (safe-by-default).
- GapDetector: Detects tick gaps per stream by comparing consecutive timestamps.
- OutlierFilter: Detects price-jump outliers per stream (bps threshold).

All detectors are stateful (track per-stream history) and deterministic
(no internal time calls; timestamps passed as arguments).

This module has NO dependencies on reconcile/ or observability/ to avoid
circular imports. Metrics recording is done by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class GapEvent(NamedTuple):
    """Emitted when a tick gap exceeds a bucket threshold.

    Attributes:
        stream: Stream identifier (e.g. "book_ticker", "agg_trade").
        gap_ms: Actual gap in milliseconds.
        bucket: Human-readable bucket label (e.g. "500", "2000", "5000", ">5000").
    """

    stream: str
    gap_ms: int
    bucket: str


class OutlierEvent(NamedTuple):
    """Emitted when a price jump exceeds the configured threshold.

    Attributes:
        stream: Stream identifier.
        delta_bps: Absolute price change in basis points.
        kind: Outlier kind (always "price" in v0).
    """

    stream: str
    delta_bps: float
    kind: str


@dataclass(frozen=True)
class DataQualityConfig:
    """Configuration for data quality detectors.

    All defaults are conservative. ``dq_enabled=False`` means detectors
    are instantiated but callers should skip invoking them — zero
    production behaviour change without explicit opt-in.

    Attributes:
        dq_enabled: Master switch. False = no detection (safe-by-default).
        stale_book_ticker_ms: Staleness threshold for book_ticker stream.
        stale_depth_ms: Staleness threshold for depth stream.
        stale_agg_trade_ms: Staleness threshold for agg_trade stream.
        gap_buckets_ms: Ordered tuple of gap bucket boundaries (ascending).
        price_jump_max_bps: Max allowed price jump in basis points.
    """

    dq_enabled: bool = False
    stale_book_ticker_ms: int = 2000
    stale_depth_ms: int = 3000
    stale_agg_trade_ms: int = 5000
    gap_buckets_ms: tuple[int, ...] = (500, 2000, 5000)
    price_jump_max_bps: int = 500


class GapDetector:
    """Detects tick-timestamp gaps per stream.

    Usage::

        detector = GapDetector(config)
        event = detector.observe("book_ticker", ts_ms=1700000000000)
        if event is not None:
            metrics.inc_gap(event.stream, event.bucket)

    The first ``observe()`` call per stream always returns ``None``
    (no previous timestamp to compare against).

    Args:
        config: DataQualityConfig with gap_buckets_ms.
    """

    def __init__(self, config: DataQualityConfig | None = None) -> None:
        cfg = config or DataQualityConfig()
        self._buckets = cfg.gap_buckets_ms
        self._last_ts: dict[str, int] = {}

    def observe(self, stream: str, ts_ms: int) -> GapEvent | None:
        """Record a tick timestamp and return a GapEvent if a gap is detected.

        Args:
            stream: Stream identifier (e.g. "book_ticker").
            ts_ms: Tick timestamp in epoch milliseconds.

        Returns:
            GapEvent if gap >= smallest bucket threshold, else None.
        """
        prev = self._last_ts.get(stream)
        self._last_ts[stream] = ts_ms

        if prev is None:
            return None

        gap_ms = ts_ms - prev
        if gap_ms < 0:
            # Out-of-order tick — don't flag as gap.
            return None

        bucket = self._classify_bucket(gap_ms)
        if bucket is None:
            return None

        return GapEvent(stream=stream, gap_ms=gap_ms, bucket=bucket)

    def _classify_bucket(self, gap_ms: int) -> str | None:
        """Classify gap into a bucket label.

        Returns None if gap is below the smallest bucket boundary.
        """
        if not self._buckets or gap_ms < self._buckets[0]:
            return None

        # Find the highest bucket boundary that gap_ms meets or exceeds.
        for i in range(len(self._buckets) - 1, -1, -1):
            if gap_ms >= self._buckets[i]:
                return (
                    f">{self._buckets[i]}" if i == len(self._buckets) - 1 else str(self._buckets[i])
                )

        return None  # pragma: no cover — unreachable given guard above

    def reset(self) -> None:
        """Clear all per-stream state (for testing)."""
        self._last_ts.clear()


class OutlierFilter:
    """Detects single-tick price jumps exceeding a bps threshold.

    Usage::

        filt = OutlierFilter(config)
        event = filt.observe_price("book_ticker", price=50000.0)
        if event is not None:
            metrics.inc_outlier(event.stream, event.kind)

    The first call per stream always returns ``None`` (no previous price).
    ``last_price`` is updated on **every** call (including outliers) so the
    detector does not "stick" on a single large gap.

    Args:
        config: DataQualityConfig with price_jump_max_bps.
    """

    def __init__(self, config: DataQualityConfig | None = None) -> None:
        cfg = config or DataQualityConfig()
        self._max_bps = cfg.price_jump_max_bps
        self._last_price: dict[str, float] = {}

    def observe_price(self, stream: str, price: float) -> OutlierEvent | None:
        """Record a price tick and return an OutlierEvent if jump is too large.

        Args:
            stream: Stream identifier.
            price: Current mid/last price.

        Returns:
            OutlierEvent if abs(delta_bps) > price_jump_max_bps, else None.
        """
        prev = self._last_price.get(stream)
        self._last_price[stream] = price

        if prev is None:
            return None

        if prev == 0.0:
            # Avoid division by zero; cannot compute bps delta.
            return None

        delta_bps = abs(price - prev) / prev * 10_000
        if delta_bps > self._max_bps:
            return OutlierEvent(stream=stream, delta_bps=delta_bps, kind="price")

        return None

    def reset(self) -> None:
        """Clear all per-stream state (for testing)."""
        self._last_price.clear()


def is_stale(now_ms: int, last_ts_ms: int, threshold_ms: int) -> bool:
    """Check whether a stream is stale.

    Args:
        now_ms: Current epoch time in milliseconds.
        last_ts_ms: Last received tick timestamp in milliseconds.
        threshold_ms: Staleness threshold in milliseconds.

    Returns:
        True if the stream has not received a tick within threshold_ms.
    """
    return (now_ms - last_ts_ms) >= threshold_ms
