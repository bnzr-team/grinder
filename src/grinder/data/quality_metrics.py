"""Data quality Prometheus metrics (Launch-03, detect-only).

Provides counters for data quality events:
- grinder_data_stale_total{stream=...}
- grinder_data_gap_total{stream=...,bucket=...}
- grinder_data_outlier_total{stream=...,kind=...}

Labels use ``stream`` only (plus ``bucket`` / ``kind``).
No ``symbol``, ``venue``, or other high-cardinality labels.

This module has NO dependencies on reconcile/ to avoid circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Metric names (stable contract — do not rename without updating metrics_contract.py)
METRIC_DATA_STALE = "grinder_data_stale_total"
METRIC_DATA_GAP = "grinder_data_gap_total"
METRIC_DATA_OUTLIER = "grinder_data_outlier_total"


@dataclass
class DataQualityMetrics:
    """Prometheus counters for data quality events.

    Thread-safe via simple dict operations (GIL protection).
    """

    stale_counts: dict[str, int] = field(default_factory=dict)
    gap_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    outlier_counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def inc_stale(self, stream: str) -> None:
        """Increment staleness counter for a stream."""
        self.stale_counts[stream] = self.stale_counts.get(stream, 0) + 1

    def inc_gap(self, stream: str, bucket: str) -> None:
        """Increment gap counter for a stream/bucket pair."""
        key = (stream, bucket)
        self.gap_counts[key] = self.gap_counts.get(key, 0) + 1

    def inc_outlier(self, stream: str, kind: str = "price") -> None:
        """Increment outlier counter for a stream/kind pair."""
        key = (stream, kind)
        self.outlier_counts[key] = self.outlier_counts.get(key, 0) + 1

    def to_prometheus_lines(self) -> list[str]:
        """Render Prometheus text-format lines.

        Always emits HELP/TYPE headers even when counters are empty,
        so the metrics contract test passes with zero-value visibility.
        """
        lines: list[str] = []

        # --- stale ---
        lines.append(f"# HELP {METRIC_DATA_STALE} Total data staleness events by stream")
        lines.append(f"# TYPE {METRIC_DATA_STALE} counter")
        if self.stale_counts:
            for stream, count in sorted(self.stale_counts.items()):
                lines.append(f'{METRIC_DATA_STALE}{{stream="{stream}"}} {count}')
        else:
            lines.append(f'{METRIC_DATA_STALE}{{stream="none"}} 0')

        # --- gap ---
        lines.append(f"# HELP {METRIC_DATA_GAP} Total data gap events by stream and bucket")
        lines.append(f"# TYPE {METRIC_DATA_GAP} counter")
        if self.gap_counts:
            for (stream, bucket), count in sorted(self.gap_counts.items()):
                lines.append(f'{METRIC_DATA_GAP}{{stream="{stream}",bucket="{bucket}"}} {count}')
        else:
            lines.append(f'{METRIC_DATA_GAP}{{stream="none",bucket="none"}} 0')

        # --- outlier ---
        lines.append(f"# HELP {METRIC_DATA_OUTLIER} Total data outlier events by stream and kind")
        lines.append(f"# TYPE {METRIC_DATA_OUTLIER} counter")
        if self.outlier_counts:
            for (stream, kind), count in sorted(self.outlier_counts.items()):
                lines.append(f'{METRIC_DATA_OUTLIER}{{stream="{stream}",kind="{kind}"}} {count}')
        else:
            lines.append(f'{METRIC_DATA_OUTLIER}{{stream="none",kind="none"}} 0')

        return lines

    def reset(self) -> None:
        """Reset all counters (for testing)."""
        self.stale_counts.clear()
        self.gap_counts.clear()
        self.outlier_counts.clear()


# Global singleton
_metrics: DataQualityMetrics | None = None


def get_data_quality_metrics() -> DataQualityMetrics:
    """Get or create global data quality metrics."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = DataQualityMetrics()
    return _metrics


def reset_data_quality_metrics() -> None:
    """Reset data quality metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
