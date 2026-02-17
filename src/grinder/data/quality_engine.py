"""Data quality engine — thin wrapper for DQ pipeline (Launch-03 PR2/PR3).

Combines GapDetector, OutlierFilter, is_stale, and DataQualityMetrics
into a single ``observe_tick()`` call that the live ingestion path can
invoke on every snapshot.

Side-effects: metrics increments + verdict for optional gating (PR3).

This module has NO imports from reconcile/ or execution/.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from grinder.data.quality import (
    DataQualityConfig,
    GapDetector,
    OutlierFilter,
    is_stale,
)
from grinder.data.quality_metrics import get_data_quality_metrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataQualityVerdict:
    """Result of a single DQ observation.

    Consumers (e.g. remediation gate) check fields to decide
    whether to block execution.  All fields are False/None when
    data quality is acceptable.
    """

    stale: bool = False
    gap_bucket: str | None = None
    outlier_kind: str | None = None

    @property
    def is_ok(self) -> bool:
        """True when no issues detected."""
        return not self.stale and self.gap_bucket is None and self.outlier_kind is None


class DataQualityEngine:
    """Stateful DQ pipeline: observe ticks, increment metrics.

    Usage::

        dq = DataQualityEngine(config)
        # On each snapshot:
        dq.observe_tick(stream="live_feed", ts_ms=snap.ts, price=float(snap.mid_price))

    All timestamps are explicit (no internal ``time.time()`` calls)
    to keep behaviour deterministic and testable.

    Args:
        config: DataQualityConfig (frozen). ``dq_enabled`` is checked
                by the *caller*, not inside this engine.
    """

    def __init__(self, config: DataQualityConfig | None = None) -> None:
        cfg = config or DataQualityConfig()
        self._config = cfg
        self._gap = GapDetector(cfg)
        self._outlier = OutlierFilter(cfg)
        self._metrics = get_data_quality_metrics()
        self._last_ts: dict[str, int] = {}

    def observe_tick(
        self,
        stream: str,
        ts_ms: int,
        price: float,
        now_ms: int | None = None,
    ) -> DataQualityVerdict:
        """Process one tick through all DQ detectors and record metrics.

        Args:
            stream: Stable stream identifier (e.g. ``"live_feed"``).
                    Must NOT contain symbol names to avoid forbidden labels.
            ts_ms: Tick timestamp in epoch milliseconds.
            price: Current mid/last price (float).
            now_ms: Current wall-clock time in epoch ms (for staleness).
                    If None, staleness check is skipped.

        Returns:
            DataQualityVerdict summarising detected issues (if any).
        """
        found_stale = False
        found_gap_bucket: str | None = None
        found_outlier_kind: str | None = None

        # --- staleness ---
        if now_ms is not None:
            prev_ts = self._last_ts.get(stream)
            if prev_ts is not None:
                threshold = self._staleness_threshold(stream)
                if is_stale(now_ms, prev_ts, threshold):
                    self._metrics.inc_stale(stream)
                    found_stale = True

        self._last_ts[stream] = ts_ms

        # --- gap ---
        gap_event = self._gap.observe(stream, ts_ms)
        if gap_event is not None:
            self._metrics.inc_gap(gap_event.stream, gap_event.bucket)
            found_gap_bucket = gap_event.bucket

        # --- outlier (price-only v0) ---
        outlier_event = self._outlier.observe_price(stream, price)
        if outlier_event is not None:
            self._metrics.inc_outlier(outlier_event.stream, outlier_event.kind)
            found_outlier_kind = outlier_event.kind

        return DataQualityVerdict(
            stale=found_stale,
            gap_bucket=found_gap_bucket,
            outlier_kind=found_outlier_kind,
        )

    def _staleness_threshold(self, stream: str) -> int:
        """Return staleness threshold for a stream (ms)."""
        thresholds: dict[str, int] = {
            "book_ticker": self._config.stale_book_ticker_ms,
            "depth": self._config.stale_depth_ms,
            "agg_trade": self._config.stale_agg_trade_ms,
        }
        return thresholds.get(stream, self._config.stale_book_ticker_ms)

    def reset(self) -> None:
        """Reset all internal state (for testing)."""
        self._gap.reset()
        self._outlier.reset()
        self._last_ts.clear()
