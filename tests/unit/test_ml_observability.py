"""Tests for M8-02c-2 and M8-02d ML observability (ADR-065).

Tests for:
1. grinder_ml_active_on gauge (0/1 per snapshot)
2. Reason codes for ACTIVE blocking
3. Block count tracking
4. Prometheus metrics export
5. Latency histogram (M8-02d)
"""

from __future__ import annotations

import pytest

from grinder.ml.metrics import (
    LATENCY_BUCKETS_MS,
    MlBlockReason,
    MlInferenceMode,
    get_ml_metrics_state,
    ml_metrics_to_prometheus_lines,
    record_ml_inference_error,
    record_ml_inference_latency,
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


class TestMlInferenceMode:
    """Test inference mode enum (M8-02d)."""

    def test_mode_values(self) -> None:
        """Mode values should be shadow and active."""
        assert MlInferenceMode.SHADOW.value == "shadow"
        assert MlInferenceMode.ACTIVE.value == "active"

    def test_exactly_two_modes(self) -> None:
        """Should have exactly two inference modes."""
        assert len(MlInferenceMode) == 2


class TestLatencyBuckets:
    """Test latency bucket configuration (M8-02d)."""

    def test_buckets_are_sorted(self) -> None:
        """Buckets should be in ascending order."""
        assert tuple(sorted(LATENCY_BUCKETS_MS)) == LATENCY_BUCKETS_MS

    def test_bucket_range(self) -> None:
        """Buckets should cover 1ms to 500ms range."""
        assert LATENCY_BUCKETS_MS[0] == 1.0
        assert LATENCY_BUCKETS_MS[-1] == 500.0

    def test_slo_buckets_present(self) -> None:
        """Key SLO thresholds (50ms, 100ms, 250ms) should be bucket boundaries."""
        assert 50.0 in LATENCY_BUCKETS_MS  # p95 target
        assert 100.0 in LATENCY_BUCKETS_MS  # p99 warning
        assert 250.0 in LATENCY_BUCKETS_MS  # p99.9 critical


class TestLatencyHistogram:
    """Test latency histogram recording (M8-02d)."""

    def test_record_single_latency(self) -> None:
        """Single latency observation should update state."""
        record_ml_inference_latency(5.0, MlInferenceMode.SHADOW)

        state = get_ml_metrics_state()
        assert state.latency_count["shadow"] == 1
        assert state.latency_sum["shadow"] == 5.0

    def test_record_multiple_latencies(self) -> None:
        """Multiple observations should accumulate."""
        record_ml_inference_latency(5.0, MlInferenceMode.SHADOW)
        record_ml_inference_latency(10.0, MlInferenceMode.SHADOW)
        record_ml_inference_latency(15.0, MlInferenceMode.ACTIVE)

        state = get_ml_metrics_state()
        assert state.latency_count["shadow"] == 2
        assert state.latency_sum["shadow"] == 15.0
        assert state.latency_count["active"] == 1
        assert state.latency_sum["active"] == 15.0

    def test_bucket_assignment_low(self) -> None:
        """Low latency should fill lower buckets."""
        record_ml_inference_latency(0.5, MlInferenceMode.SHADOW)

        state = get_ml_metrics_state()
        # 0.5ms <= 1.0ms, so it should be in all buckets
        for bucket in LATENCY_BUCKETS_MS:
            assert state.latency_buckets["shadow"][bucket] == 1

    def test_bucket_assignment_high(self) -> None:
        """High latency should only fill higher buckets."""
        record_ml_inference_latency(300.0, MlInferenceMode.ACTIVE)

        state = get_ml_metrics_state()
        buckets = state.latency_buckets["active"]

        # 300ms > 250ms, so only 500ms bucket should have count
        assert buckets[1.0] == 0
        assert buckets[50.0] == 0
        assert buckets[100.0] == 0
        assert buckets[250.0] == 0
        assert buckets[500.0] == 1

    def test_bucket_assignment_boundary(self) -> None:
        """Boundary values should be included in exact bucket."""
        record_ml_inference_latency(100.0, MlInferenceMode.SHADOW)

        state = get_ml_metrics_state()
        buckets = state.latency_buckets["shadow"]

        # 100.0ms <= 100.0ms, so 100ms and higher buckets should have count
        assert buckets[50.0] == 0
        assert buckets[100.0] == 1
        assert buckets[250.0] == 1
        assert buckets[500.0] == 1

    def test_modes_tracked_separately(self) -> None:
        """Shadow and active modes should have separate histograms."""
        record_ml_inference_latency(5.0, MlInferenceMode.SHADOW)
        record_ml_inference_latency(10.0, MlInferenceMode.ACTIVE)

        state = get_ml_metrics_state()
        assert state.latency_count["shadow"] == 1
        assert state.latency_count["active"] == 1
        assert state.latency_sum["shadow"] == 5.0
        assert state.latency_sum["active"] == 10.0


class TestLatencyPrometheusExport:
    """Test latency histogram Prometheus export (M8-02d)."""

    def test_histogram_help_and_type(self) -> None:
        """Histogram should have HELP and TYPE lines."""
        lines = ml_metrics_to_prometheus_lines()
        output = "\n".join(lines)

        assert "# HELP grinder_ml_inference_latency_ms" in output
        assert "# TYPE grinder_ml_inference_latency_ms histogram" in output

    def test_empty_histogram_export(self) -> None:
        """Empty histogram should export zeroes for all modes."""
        lines = ml_metrics_to_prometheus_lines()

        for mode in MlInferenceMode:
            mode_key = mode.value
            # Sum and count should be zero
            assert f'grinder_ml_inference_latency_ms_sum{{mode="{mode_key}"}} 0.0' in lines
            assert f'grinder_ml_inference_latency_ms_count{{mode="{mode_key}"}} 0' in lines

    def test_histogram_bucket_lines(self) -> None:
        """Histogram should export cumulative bucket counts."""
        record_ml_inference_latency(5.0, MlInferenceMode.SHADOW)

        lines = ml_metrics_to_prometheus_lines()

        # Check that bucket lines exist
        assert 'grinder_ml_inference_latency_ms_bucket{mode="shadow",le="1.0"} 0' in lines
        assert 'grinder_ml_inference_latency_ms_bucket{mode="shadow",le="5.0"} 1' in lines
        assert 'grinder_ml_inference_latency_ms_bucket{mode="shadow",le="10.0"} 1' in lines
        assert 'grinder_ml_inference_latency_ms_bucket{mode="shadow",le="+Inf"} 1' in lines

    def test_histogram_sum_and_count(self) -> None:
        """Histogram should export sum and count."""
        record_ml_inference_latency(10.0, MlInferenceMode.ACTIVE)
        record_ml_inference_latency(20.0, MlInferenceMode.ACTIVE)

        lines = ml_metrics_to_prometheus_lines()

        assert 'grinder_ml_inference_latency_ms_sum{mode="active"} 30.0' in lines
        assert 'grinder_ml_inference_latency_ms_count{mode="active"} 2' in lines

    def test_histogram_cumulative_buckets(self) -> None:
        """Bucket counts should be cumulative (not per-bucket)."""
        # Add observations at different latencies
        record_ml_inference_latency(3.0, MlInferenceMode.SHADOW)  # <= 5ms
        record_ml_inference_latency(8.0, MlInferenceMode.SHADOW)  # <= 10ms
        record_ml_inference_latency(30.0, MlInferenceMode.SHADOW)  # <= 50ms

        lines = ml_metrics_to_prometheus_lines()

        # Buckets should be cumulative
        # 3ms: in 5ms, 10ms, 25ms, 50ms... buckets
        # 8ms: in 10ms, 25ms, 50ms... buckets
        # 30ms: in 50ms, 100ms... buckets
        assert (
            'grinder_ml_inference_latency_ms_bucket{mode="shadow",le="5.0"} 1' in lines
        )  # 3ms only
        assert (
            'grinder_ml_inference_latency_ms_bucket{mode="shadow",le="10.0"} 2' in lines
        )  # 3ms + 8ms
        assert 'grinder_ml_inference_latency_ms_bucket{mode="shadow",le="50.0"} 3' in lines  # all 3
        assert 'grinder_ml_inference_latency_ms_bucket{mode="shadow",le="+Inf"} 3' in lines  # all 3
