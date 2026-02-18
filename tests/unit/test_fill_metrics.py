"""Tests for grinder.observability.fill_metrics (Launch-06 PR1).

Covers:
- FillMetrics.to_prometheus_lines() placeholder output
- REQUIRED_METRICS_PATTERNS satisfied by placeholder output
- Forbidden labels absent
- Only allowlisted label keys present
- Recording fills updates counters correctly
"""

from __future__ import annotations

from grinder.observability.fill_metrics import (
    METRIC_FILL_FEES,
    METRIC_FILL_NOTIONAL,
    METRIC_FILLS,
    FillMetrics,
    get_fill_metrics,
    reset_fill_metrics,
)
from grinder.observability.metrics_contract import (
    FORBIDDEN_METRIC_LABELS,
    REQUIRED_METRICS_PATTERNS,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _output(fm: FillMetrics) -> str:
    """Join prometheus lines for matching."""
    return "\n".join(fm.to_prometheus_lines())


# ---------------------------------------------------------------------------
# Placeholder / contract tests
# ---------------------------------------------------------------------------


class TestFillMetricsPlaceholders:
    """Empty FillMetrics emits zero-value placeholders for contract visibility."""

    def test_placeholder_lines_present(self) -> None:
        fm = FillMetrics()
        output = _output(fm)
        assert 'source="none"' in output
        assert 'side="none"' in output
        assert 'liquidity="none"' in output

    def test_placeholder_values_zero(self) -> None:
        fm = FillMetrics()
        output = _output(fm)
        assert f'{METRIC_FILLS}{{source="none",side="none",liquidity="none"}} 0' in output
        assert f'{METRIC_FILL_NOTIONAL}{{source="none",side="none",liquidity="none"}} 0' in output
        assert f'{METRIC_FILL_FEES}{{source="none",side="none",liquidity="none"}} 0' in output


class TestMetricsContractSatisfied:
    """Fill metrics placeholders satisfy REQUIRED_METRICS_PATTERNS."""

    def test_all_required_patterns_present(self) -> None:
        fm = FillMetrics()
        output = _output(fm)
        # Filter to fill-related patterns only
        fill_patterns = [
            p for p in REQUIRED_METRICS_PATTERNS if "grinder_fill" in p or "grinder_fills" in p
        ]
        assert len(fill_patterns) > 0, "No fill patterns found in contract"
        for pattern in fill_patterns:
            assert pattern in output, f"Missing required pattern: {pattern!r}"


# ---------------------------------------------------------------------------
# Forbidden labels tests
# ---------------------------------------------------------------------------


class TestForbiddenLabels:
    """Fill metrics output must NOT contain high-cardinality labels."""

    def test_no_forbidden_labels_in_placeholders(self) -> None:
        fm = FillMetrics()
        output = _output(fm)
        for label in FORBIDDEN_METRIC_LABELS:
            assert label not in output, f"Forbidden label {label!r} found in output"

    def test_no_forbidden_labels_after_recording(self) -> None:
        fm = FillMetrics()
        fm.record_fill(source="sim", side="buy", liquidity="taker", notional_value=100.0, fee=0.1)
        fm.record_fill(
            source="reconcile", side="sell", liquidity="maker", notional_value=50.0, fee=0.05
        )
        output = _output(fm)
        for label in FORBIDDEN_METRIC_LABELS:
            assert label not in output, f"Forbidden label {label!r} found in output"


class TestAllowlistedLabelsOnly:
    """Only allowed label keys appear in fill metrics output."""

    ALLOWED_LABEL_KEYS: frozenset[str] = frozenset({"source", "side", "liquidity"})

    def test_only_allowed_labels(self) -> None:
        fm = FillMetrics()
        fm.record_fill(source="sim", side="buy", liquidity="taker", notional_value=100.0, fee=0.1)
        output = _output(fm)
        for line in output.split("\n"):
            if "{" not in line:
                continue
            label_str = line.split("{", 1)[1].split("}", 1)[0]
            keys = {kv.split("=", 1)[0] for kv in label_str.split(",") if "=" in kv}
            assert keys.issubset(self.ALLOWED_LABEL_KEYS), (
                f"Unexpected label keys {keys - self.ALLOWED_LABEL_KEYS} in: {line}"
            )


# ---------------------------------------------------------------------------
# Recording tests
# ---------------------------------------------------------------------------


class TestFillMetricsRecording:
    """FillMetrics.record_fill() updates counters correctly."""

    def test_single_record(self) -> None:
        fm = FillMetrics()
        fm.record_fill(source="sim", side="buy", liquidity="taker", notional_value=100.0, fee=0.1)
        output = _output(fm)
        assert f'{METRIC_FILLS}{{source="sim",side="buy",liquidity="taker"}} 1' in output
        assert (
            f'{METRIC_FILL_NOTIONAL}{{source="sim",side="buy",liquidity="taker"}} 100.0' in output
        )
        assert f'{METRIC_FILL_FEES}{{source="sim",side="buy",liquidity="taker"}} 0.1' in output

    def test_multiple_same_label(self) -> None:
        fm = FillMetrics()
        fm.record_fill(source="sim", side="buy", liquidity="taker", notional_value=100.0, fee=0.1)
        fm.record_fill(source="sim", side="buy", liquidity="taker", notional_value=200.0, fee=0.2)
        output = _output(fm)
        assert f'{METRIC_FILLS}{{source="sim",side="buy",liquidity="taker"}} 2' in output

    def test_different_labels(self) -> None:
        fm = FillMetrics()
        fm.record_fill(source="sim", side="buy", liquidity="taker", notional_value=100.0, fee=0.1)
        fm.record_fill(
            source="reconcile", side="sell", liquidity="maker", notional_value=50.0, fee=0.05
        )
        output = _output(fm)
        assert f'{METRIC_FILLS}{{source="reconcile",side="sell",liquidity="maker"}} 1' in output
        assert f'{METRIC_FILLS}{{source="sim",side="buy",liquidity="taker"}} 1' in output

    def test_placeholders_replaced_after_record(self) -> None:
        """Once real data is recorded, placeholder 'none' labels disappear."""
        fm = FillMetrics()
        fm.record_fill(source="sim", side="buy", liquidity="taker", notional_value=100.0, fee=0.1)
        output = _output(fm)
        assert 'source="none"' not in output


# ---------------------------------------------------------------------------
# Singleton tests
# ---------------------------------------------------------------------------


class TestSingleton:
    """Global singleton lifecycle."""

    def test_get_returns_same_instance(self) -> None:
        reset_fill_metrics()
        a = get_fill_metrics()
        b = get_fill_metrics()
        assert a is b

    def test_reset_creates_new_instance(self) -> None:
        reset_fill_metrics()
        a = get_fill_metrics()
        reset_fill_metrics()
        b = get_fill_metrics()
        assert a is not b


# ---------------------------------------------------------------------------
# Reset tests
# ---------------------------------------------------------------------------


class TestReset:
    """FillMetrics.reset() clears all counters."""

    def test_reset_clears(self) -> None:
        fm = FillMetrics()
        fm.record_fill(source="sim", side="buy", liquidity="taker", notional_value=100.0, fee=0.1)
        fm.reset()
        output = _output(fm)
        assert 'source="none"' in output
        assert f"{METRIC_FILLS}" in output
