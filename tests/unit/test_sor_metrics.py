"""Unit tests for SOR metrics (Launch-14 PR2).

Tests cover:
- Decision counter increments
- Amend savings counter
- Prometheus output format (HELP/TYPE headers, labeled series)
- Zero-value fallback
- Contract patterns match actual output
"""

from __future__ import annotations

import pytest

pytest.importorskip("redis", reason="redis not installed")

from grinder.execution.sor_metrics import (
    METRIC_ENGINE_INITIALIZED,
    METRIC_ROUTER_AMEND_SAVINGS,
    METRIC_ROUTER_DECISION,
    SorMetrics,
    get_sor_metrics,
    reset_sor_metrics,
)
from grinder.observability.live_contract import REQUIRED_METRICS_PATTERNS


class TestSorMetrics:
    """Test SorMetrics dataclass methods."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def test_record_decision_increments(self) -> None:
        """Multiple record_decision calls increment correctly."""
        m = get_sor_metrics()
        m.record_decision("CANCEL_REPLACE", "NO_EXISTING_ORDER")
        m.record_decision("CANCEL_REPLACE", "NO_EXISTING_ORDER")
        m.record_decision("BLOCK", "WOULD_CROSS_SPREAD")

        assert m.decisions[("CANCEL_REPLACE", "NO_EXISTING_ORDER")] == 2
        assert m.decisions[("BLOCK", "WOULD_CROSS_SPREAD")] == 1

    def test_record_amend_saving(self) -> None:
        """Amend savings counter increments."""
        m = get_sor_metrics()
        assert m.amend_savings == 0
        m.record_amend_saving()
        m.record_amend_saving()
        assert m.amend_savings == 2

    def test_prometheus_lines_format(self) -> None:
        """Output includes HELP/TYPE headers and labeled series."""
        m = get_sor_metrics()
        m.record_decision("CANCEL_REPLACE", "NO_EXISTING_ORDER")
        m.record_amend_saving()

        lines = m.to_prometheus_lines()
        text = "\n".join(lines)

        # Decision counter
        assert f"# HELP {METRIC_ROUTER_DECISION}" in text
        assert f"# TYPE {METRIC_ROUTER_DECISION}" in text
        assert (
            f'{METRIC_ROUTER_DECISION}{{decision="CANCEL_REPLACE",reason="NO_EXISTING_ORDER"}} 1'
        ) in text

        # Amend savings counter
        assert f"# HELP {METRIC_ROUTER_AMEND_SAVINGS}" in text
        assert f"# TYPE {METRIC_ROUTER_AMEND_SAVINGS}" in text
        assert f"{METRIC_ROUTER_AMEND_SAVINGS} 1" in text

    def test_prometheus_zero_fallback(self) -> None:
        """Empty metrics still emit valid HELP/TYPE + zero-value output."""
        m = SorMetrics()
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)

        assert f"# HELP {METRIC_ROUTER_DECISION}" in text
        assert f"# TYPE {METRIC_ROUTER_DECISION}" in text
        # Zero-value fallback line
        assert f'{METRIC_ROUTER_DECISION}{{decision="none",reason="none"}} 0' in text
        assert f"{METRIC_ROUTER_AMEND_SAVINGS} 0" in text

    def test_contract_patterns_match(self) -> None:
        """All SOR patterns in REQUIRED_METRICS_PATTERNS match actual output."""
        m = SorMetrics()
        # Record at least one decision so the labeled series appears
        m.record_decision("CANCEL_REPLACE", "NO_EXISTING_ORDER")
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)

        # Find SOR-specific patterns in REQUIRED_METRICS_PATTERNS
        sor_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "router" in p.lower()]
        assert len(sor_patterns) >= 6, (
            f"Expected at least 6 SOR patterns in contract, found {len(sor_patterns)}"
        )

        for pattern in sor_patterns:
            assert pattern in text, f"Contract pattern not found in SOR metrics output: {pattern!r}"


class TestEngineInitializedGauge:
    """PR-C4: Tests for grinder_live_engine_initialized gauge."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def test_default_is_zero(self) -> None:
        """Fresh SorMetrics has engine_initialized=False (gauge=0)."""
        m = SorMetrics()
        assert m.engine_initialized is False
        text = "\n".join(m.to_prometheus_lines())
        assert f"{METRIC_ENGINE_INITIALIZED} 0" in text

    def test_set_engine_initialized(self) -> None:
        """After set_engine_initialized(), gauge=1."""
        m = get_sor_metrics()
        m.set_engine_initialized()
        assert m.engine_initialized is True
        text = "\n".join(m.to_prometheus_lines())
        assert f"{METRIC_ENGINE_INITIALIZED} 1" in text

    def test_prometheus_help_type_present(self) -> None:
        """HELP/TYPE headers emitted for engine_initialized."""
        m = SorMetrics()
        text = "\n".join(m.to_prometheus_lines())
        assert f"# HELP {METRIC_ENGINE_INITIALIZED}" in text
        assert f"# TYPE {METRIC_ENGINE_INITIALIZED} gauge" in text

    def test_contract_pattern_matches(self) -> None:
        """grinder_live_engine_initialized in REQUIRED_METRICS_PATTERNS matches output."""
        m = SorMetrics()
        text = "\n".join(m.to_prometheus_lines())
        engine_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "live_engine_initialized" in p]
        assert len(engine_patterns) == 3  # HELP, TYPE, value
        for pattern in engine_patterns:
            assert pattern in text, f"Contract pattern missing: {pattern!r}"
