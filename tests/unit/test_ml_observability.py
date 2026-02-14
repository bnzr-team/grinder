"""Tests for M8-02c-2 ML observability (ADR-065).

Tests for:
1. grinder_ml_active_on gauge (0/1 per snapshot)
2. Reason codes for ACTIVE blocking
3. Block count tracking
4. Prometheus metrics export
"""

from __future__ import annotations

import pytest

from grinder.ml.metrics import (
    MlBlockReason,
    get_ml_metrics_state,
    ml_metrics_to_prometheus_lines,
    record_ml_inference_error,
    record_ml_inference_success,
    reset_ml_metrics_state,
    set_ml_active_on,
)


@pytest.fixture(autouse=True)
def reset_metrics() -> None:
    """Reset ML metrics state before each test."""
    reset_ml_metrics_state()


class TestMlBlockReason:
    """Test reason code enum."""

    def test_reason_codes_are_strings(self) -> None:
        """Reason codes should be string values for logging/metrics."""
        assert MlBlockReason.KILL_SWITCH_ENV.value == "KILL_SWITCH_ENV"
        assert MlBlockReason.KILL_SWITCH_CONFIG.value == "KILL_SWITCH_CONFIG"
        assert MlBlockReason.INFER_DISABLED.value == "INFER_DISABLED"
        assert MlBlockReason.ACTIVE_DISABLED.value == "ACTIVE_DISABLED"
        assert MlBlockReason.BAD_ACK.value == "BAD_ACK"
        assert MlBlockReason.ONNX_UNAVAILABLE.value == "ONNX_UNAVAILABLE"
        assert MlBlockReason.ARTIFACT_DIR_MISSING.value == "ARTIFACT_DIR_MISSING"
        assert MlBlockReason.MANIFEST_INVALID.value == "MANIFEST_INVALID"
        assert MlBlockReason.MODEL_NOT_LOADED.value == "MODEL_NOT_LOADED"
        assert MlBlockReason.ENV_NOT_ALLOWED.value == "ENV_NOT_ALLOWED"

    def test_all_10_reason_codes_exist(self) -> None:
        """ADR-065 requires 10 distinct reason codes."""
        assert len(MlBlockReason) == 10


class TestMlMetricsState:
    """Test ML metrics state management."""

    def test_default_state(self) -> None:
        """Default state: gauge=0, no blocks, no inferences."""
        state = get_ml_metrics_state()

        assert state.ml_active_on == 0
        assert state.last_block_reason is None
        assert state.block_counts == {}
        assert state.inference_count == 0
        assert state.inference_error_count == 0

    def test_set_ml_active_on_true(self) -> None:
        """Setting active ON sets gauge=1."""
        set_ml_active_on(True)

        state = get_ml_metrics_state()
        assert state.ml_active_on == 1
        assert state.last_block_reason is None

    def test_set_ml_active_on_false_with_reason(self) -> None:
        """Setting active OFF with reason sets gauge=0 and tracks block."""
        set_ml_active_on(False, MlBlockReason.KILL_SWITCH_ENV)

        state = get_ml_metrics_state()
        assert state.ml_active_on == 0
        assert state.last_block_reason == MlBlockReason.KILL_SWITCH_ENV
        assert state.block_counts["KILL_SWITCH_ENV"] == 1

    def test_block_counts_accumulate(self) -> None:
        """Multiple blocks should accumulate by reason."""
        set_ml_active_on(False, MlBlockReason.KILL_SWITCH_ENV)
        set_ml_active_on(False, MlBlockReason.KILL_SWITCH_ENV)
        set_ml_active_on(False, MlBlockReason.INFER_DISABLED)

        state = get_ml_metrics_state()
        assert state.block_counts["KILL_SWITCH_ENV"] == 2
        assert state.block_counts["INFER_DISABLED"] == 1

    def test_inference_success_count(self) -> None:
        """Successful inferences should be tracked."""
        record_ml_inference_success()
        record_ml_inference_success()

        state = get_ml_metrics_state()
        assert state.inference_count == 2

    def test_inference_error_count(self) -> None:
        """Inference errors should be tracked."""
        record_ml_inference_error()
        record_ml_inference_error()
        record_ml_inference_error()

        state = get_ml_metrics_state()
        assert state.inference_error_count == 3


class TestPrometheusExport:
    """Test Prometheus metrics export."""

    def test_default_metrics_output(self) -> None:
        """Default state should export valid Prometheus metrics."""
        lines = ml_metrics_to_prometheus_lines()

        # Check gauge
        assert "# HELP grinder_ml_active_on" in "\n".join(lines)
        assert "# TYPE grinder_ml_active_on gauge" in "\n".join(lines)
        assert "grinder_ml_active_on 0" in lines

        # Check block counter (all reasons should be present with 0)
        assert "# TYPE grinder_ml_block_total counter" in "\n".join(lines)
        for reason in MlBlockReason:
            assert f'grinder_ml_block_total{{reason="{reason.value}"}} 0' in lines

        # Check inference counters
        assert "grinder_ml_inference_total 0" in lines
        assert "grinder_ml_inference_errors_total 0" in lines

    def test_active_on_metrics_output(self) -> None:
        """Active ON should export gauge=1."""
        set_ml_active_on(True)

        lines = ml_metrics_to_prometheus_lines()
        assert "grinder_ml_active_on 1" in lines

    def test_block_metrics_output(self) -> None:
        """Blocked state should export correct counts."""
        set_ml_active_on(False, MlBlockReason.KILL_SWITCH_ENV)
        set_ml_active_on(False, MlBlockReason.KILL_SWITCH_ENV)
        set_ml_active_on(False, MlBlockReason.BAD_ACK)

        lines = ml_metrics_to_prometheus_lines()
        assert 'grinder_ml_block_total{reason="KILL_SWITCH_ENV"} 2' in lines
        assert 'grinder_ml_block_total{reason="BAD_ACK"} 1' in lines

    def test_inference_metrics_output(self) -> None:
        """Inference counts should be exported."""
        record_ml_inference_success()
        record_ml_inference_success()
        record_ml_inference_error()

        lines = ml_metrics_to_prometheus_lines()
        assert "grinder_ml_inference_total 2" in lines
        assert "grinder_ml_inference_errors_total 1" in lines


class TestGaugePerSnapshot:
    """Test gauge updates per snapshot (not cached)."""

    def test_gauge_updates_each_call(self) -> None:
        """Gauge should update on each set_ml_active_on call."""
        # Start OFF
        state = get_ml_metrics_state()
        assert state.ml_active_on == 0

        # Turn ON
        set_ml_active_on(True)
        assert get_ml_metrics_state().ml_active_on == 1

        # Turn OFF (blocked)
        set_ml_active_on(False, MlBlockReason.KILL_SWITCH_CONFIG)
        assert get_ml_metrics_state().ml_active_on == 0

        # Turn ON again
        set_ml_active_on(True)
        assert get_ml_metrics_state().ml_active_on == 1

    def test_last_block_reason_updates(self) -> None:
        """Last block reason should reflect most recent block."""
        set_ml_active_on(False, MlBlockReason.INFER_DISABLED)
        assert get_ml_metrics_state().last_block_reason == MlBlockReason.INFER_DISABLED

        set_ml_active_on(False, MlBlockReason.ENV_NOT_ALLOWED)
        assert get_ml_metrics_state().last_block_reason == MlBlockReason.ENV_NOT_ALLOWED

        # Active ON clears the reason
        set_ml_active_on(True)
        assert get_ml_metrics_state().last_block_reason is None

    def test_gauge_transitions_on_to_off(self) -> None:
        """Tick N: ACTIVE ON → Tick N+1: disabled → gauge = 0.

        Simulates: active on tick N, then ml_active_enabled turned off on tick N+1.
        Gauge must not "stick" at 1.
        """
        # Tick N: ACTIVE inference succeeds
        set_ml_active_on(True)
        assert get_ml_metrics_state().ml_active_on == 1

        # Tick N+1: ACTIVE mode disabled (simulated by setting gauge off with reason)
        set_ml_active_on(False, MlBlockReason.ACTIVE_DISABLED)
        assert get_ml_metrics_state().ml_active_on == 0
        assert get_ml_metrics_state().last_block_reason == MlBlockReason.ACTIVE_DISABLED

    def test_gauge_inference_failure_resets_to_zero(self) -> None:
        """If inference fails after being allowed, gauge should be 0.

        Simulates: active allowed but inference returns False (no block reason).
        """
        # Start with successful inference
        set_ml_active_on(True)
        assert get_ml_metrics_state().ml_active_on == 1

        # Next tick: inference allowed but fails (no reason code)
        set_ml_active_on(False)
        assert get_ml_metrics_state().ml_active_on == 0
        # No block reason when inference fails (it's a runtime failure, not gate block)
        assert get_ml_metrics_state().last_block_reason is None
