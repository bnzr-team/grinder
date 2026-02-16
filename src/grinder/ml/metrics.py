"""ML observability metrics (M8-02c-2, ADR-065, M8-02d).

Provides:
- Reason codes for ACTIVE mode blocking (SSOT constants)
- ML active gauge state (per-snapshot 0/1)
- Latency histogram for inference SLO tracking (M8-02d)
- Prometheus metrics export

Reason code priority (first match wins):
1. KILL_SWITCH_ENV - ML_KILL_SWITCH=1 env var
2. KILL_SWITCH_CONFIG - ml_kill_switch=True config
3. INFER_DISABLED - ml_infer_enabled=False
4. ACTIVE_DISABLED - ml_active_enabled=False
5. BAD_ACK - ml_active_ack != expected
6. ONNX_UNAVAILABLE - onnxruntime not installed
7. ARTIFACT_DIR_MISSING - onnx_artifact_dir not found
8. MANIFEST_INVALID - manifest.json invalid or missing
9. MODEL_NOT_LOADED - ONNX model is None
10. ENV_NOT_ALLOWED - GRINDER_ENV not in allowlist

Latency SLO thresholds (M8-02d):
- p95 < 50ms (target)
- p99 < 100ms (warning)
- p99.9 < 250ms (critical)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class MlInferenceMode(StrEnum):
    """Inference mode label for latency histogram."""

    SHADOW = "shadow"
    ACTIVE = "active"


# Histogram buckets for inference latency (milliseconds)
# Covers typical ONNX inference: 1ms to 500ms range
LATENCY_BUCKETS_MS: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0)


class MlBlockReason(StrEnum):
    """Reason codes for ACTIVE mode blocking (ADR-065).

    Priority order: first match in truth table wins.
    String values are used for logging and metrics labels.
    """

    KILL_SWITCH_ENV = "KILL_SWITCH_ENV"
    KILL_SWITCH_CONFIG = "KILL_SWITCH_CONFIG"
    INFER_DISABLED = "INFER_DISABLED"
    ACTIVE_DISABLED = "ACTIVE_DISABLED"
    BAD_ACK = "BAD_ACK"
    ONNX_UNAVAILABLE = "ONNX_UNAVAILABLE"
    ARTIFACT_DIR_MISSING = "ARTIFACT_DIR_MISSING"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    MODEL_NOT_LOADED = "MODEL_NOT_LOADED"
    ENV_NOT_ALLOWED = "ENV_NOT_ALLOWED"


@dataclass
class MlMetricsState:
    """Current state of ML metrics for Prometheus export.

    Attributes:
        ml_active_on: Whether ACTIVE mode is enabled (0 or 1)
        last_block_reason: Last reason ACTIVE was blocked (None if active)
        block_counts: Count of blocks by reason {reason: count}
        inference_count: Total successful inferences
        inference_error_count: Total inference errors
        latency_samples: Histogram samples by mode {mode: [latency_ms, ...]}
        latency_sum: Sum of latency samples by mode {mode: total_ms}
        latency_count: Count of latency samples by mode {mode: count}
    """

    ml_active_on: int = 0
    last_block_reason: MlBlockReason | None = None
    block_counts: dict[str, int] = field(default_factory=dict)
    inference_count: int = 0
    inference_error_count: int = 0
    # Histogram state: latency_buckets tracks cumulative counts per mode
    latency_buckets: dict[str, dict[float, int]] = field(default_factory=dict)
    latency_sum: dict[str, float] = field(default_factory=dict)
    latency_count: dict[str, int] = field(default_factory=dict)


# Module-level state for ML metrics (updated per-snapshot)
_ml_state: list[MlMetricsState | None] = [None]


def get_ml_metrics_state() -> MlMetricsState:
    """Get current ML metrics state (creates if needed)."""
    state = _ml_state[0]
    if state is None:
        state = MlMetricsState()
        _ml_state[0] = state
    return state


def set_ml_active_on(active: bool, block_reason: MlBlockReason | None = None) -> None:
    """Update ML active gauge (called per-snapshot).

    Args:
        active: Whether ACTIVE mode is enabled this snapshot
        block_reason: If not active, the reason code
    """
    state = get_ml_metrics_state()
    state.ml_active_on = 1 if active else 0
    state.last_block_reason = block_reason

    # Track block counts by reason
    if not active and block_reason is not None:
        reason_key = block_reason.value
        state.block_counts[reason_key] = state.block_counts.get(reason_key, 0) + 1


def record_ml_inference_success() -> None:
    """Record successful ML inference."""
    state = get_ml_metrics_state()
    state.inference_count += 1


def record_ml_inference_error() -> None:
    """Record ML inference error."""
    state = get_ml_metrics_state()
    state.inference_error_count += 1


def record_ml_inference_latency(latency_ms: float, mode: MlInferenceMode) -> None:
    """Record ML inference latency observation (M8-02d).

    Updates histogram buckets, sum, and count for the given mode.

    Args:
        latency_ms: Inference latency in milliseconds
        mode: Inference mode (shadow or active)
    """
    state = get_ml_metrics_state()
    mode_key = mode.value

    # Initialize buckets for this mode if needed
    if mode_key not in state.latency_buckets:
        state.latency_buckets[mode_key] = dict.fromkeys(LATENCY_BUCKETS_MS, 0)
        state.latency_sum[mode_key] = 0.0
        state.latency_count[mode_key] = 0

    # Update histogram buckets (cumulative)
    for bucket in LATENCY_BUCKETS_MS:
        if latency_ms <= bucket:
            state.latency_buckets[mode_key][bucket] += 1

    # Update sum and count
    state.latency_sum[mode_key] += latency_ms
    state.latency_count[mode_key] += 1


def reset_ml_metrics_state() -> None:
    """Reset ML metrics state (for testing)."""
    _ml_state[0] = None


def ml_metrics_to_prometheus_lines() -> list[str]:
    """Export ML metrics in Prometheus text format.

    Returns:
        List of Prometheus-compatible metric lines.
    """
    state = get_ml_metrics_state()

    lines = [
        "# HELP grinder_ml_active_on Whether ML ACTIVE mode is enabled (1=yes, 0=no)",
        "# TYPE grinder_ml_active_on gauge",
        f"grinder_ml_active_on {state.ml_active_on}",
        "# HELP grinder_ml_block_total Total times ACTIVE was blocked by reason",
        "# TYPE grinder_ml_block_total counter",
    ]

    # Add block counts by reason
    for reason in MlBlockReason:
        count = state.block_counts.get(reason.value, 0)
        lines.append(f'grinder_ml_block_total{{reason="{reason.value}"}} {count}')

    lines.extend(
        [
            "# HELP grinder_ml_inference_total Total successful ML inferences",
            "# TYPE grinder_ml_inference_total counter",
            f"grinder_ml_inference_total {state.inference_count}",
            "# HELP grinder_ml_inference_errors_total Total ML inference errors",
            "# TYPE grinder_ml_inference_errors_total counter",
            f"grinder_ml_inference_errors_total {state.inference_error_count}",
        ]
    )

    # Latency histogram (M8-02d)
    lines.extend(
        [
            "# HELP grinder_ml_inference_latency_ms ML inference latency in milliseconds",
            "# TYPE grinder_ml_inference_latency_ms histogram",
        ]
    )

    for mode in MlInferenceMode:
        mode_key = mode.value
        buckets = state.latency_buckets.get(mode_key, {})
        total_sum = state.latency_sum.get(mode_key, 0.0)
        total_count = state.latency_count.get(mode_key, 0)

        # Emit bucket lines (already cumulative from recording)
        for bucket in LATENCY_BUCKETS_MS:
            bucket_count = buckets.get(bucket, 0)
            lines.append(
                f'grinder_ml_inference_latency_ms_bucket{{mode="{mode_key}",le="{bucket}"}} {bucket_count}'
            )
        # +Inf bucket (always equals total count)
        lines.append(
            f'grinder_ml_inference_latency_ms_bucket{{mode="{mode_key}",le="+Inf"}} {total_count}'
        )
        # Sum and count
        lines.append(f'grinder_ml_inference_latency_ms_sum{{mode="{mode_key}"}} {total_sum}')
        lines.append(f'grinder_ml_inference_latency_ms_count{{mode="{mode_key}"}} {total_count}')

    return lines
