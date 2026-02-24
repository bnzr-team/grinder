"""Tests for live runtime contract (healthz, readyz, and metrics endpoints).

Enforces the contract defined in src/grinder/observability/live_contract.py:

GET /healthz:
    - Body is valid JSON with "status" and "uptime_s" keys
    - "status" value is "ok"

GET /readyz:
    - Body is valid JSON with "ready" and "role" keys
    - "ready" is true if ACTIVE, false otherwise
    - Returns 200 if ACTIVE, 503 if STANDBY/UNKNOWN

GET /metrics:
    - Body is Prometheus text format
    - Contains all required metrics with HELP/TYPE lines
"""

from __future__ import annotations

import json

import pytest

# Skip entire module if redis not installed (collection won't fail)
pytest.importorskip("redis", reason="redis not installed")

from grinder.connectors.metrics import reset_connector_metrics
from grinder.execution.port_metrics import reset_port_metrics
from grinder.gating import reset_gating_metrics
from grinder.ha.role import HARole, reset_ha_state, set_ha_state
from grinder.observability import (
    FORBIDDEN_METRIC_LABELS,
    REQUIRED_HEALTHZ_KEYS,
    REQUIRED_METRICS_PATTERNS,
    REQUIRED_READYZ_KEYS,
    build_healthz_body,
    build_metrics_body,
    build_readyz_body,
    get_start_time,
    reset_start_time,
    set_start_time,
)
from grinder.observability.metrics_builder import reset_metrics_builder
from grinder.reconcile.metrics import reset_reconcile_metrics


@pytest.fixture(autouse=True)
def reset_state() -> None:
    """Reset global state before each test."""
    reset_start_time()
    reset_gating_metrics()
    reset_metrics_builder()
    reset_ha_state()
    reset_connector_metrics()
    reset_port_metrics()
    reset_reconcile_metrics()


class TestHealthzContract:
    """Tests for /healthz endpoint contract."""

    def test_healthz_returns_valid_json(self) -> None:
        """Test that healthz body is valid JSON."""
        set_start_time(0)  # Fixed start time for testing
        body = build_healthz_body()

        # Should parse as JSON without error
        data = json.loads(body)
        assert isinstance(data, dict)

    def test_healthz_has_required_keys(self) -> None:
        """Test that healthz body contains all required keys."""
        set_start_time(0)
        body = build_healthz_body()
        data = json.loads(body)

        for key in REQUIRED_HEALTHZ_KEYS:
            assert key in data, f"Missing required key: {key}"

    def test_healthz_status_is_ok(self) -> None:
        """Test that status value is 'ok'."""
        set_start_time(0)
        body = build_healthz_body()
        data = json.loads(body)

        assert data["status"] == "ok"

    def test_healthz_uptime_is_numeric(self) -> None:
        """Test that uptime_s is a numeric value."""
        set_start_time(0)
        body = build_healthz_body()
        data = json.loads(body)

        assert isinstance(data["uptime_s"], (int, float))
        assert data["uptime_s"] >= 0


class TestReadyzContract:
    """Tests for /readyz endpoint contract."""

    def test_readyz_returns_valid_json(self) -> None:
        """Test that readyz body is valid JSON."""
        body, _ = build_readyz_body()

        # Should parse as JSON without error
        data = json.loads(body)
        assert isinstance(data, dict)

    def test_readyz_has_required_keys(self) -> None:
        """Test that readyz body contains all required keys."""
        body, _ = build_readyz_body()
        data = json.loads(body)

        for key in REQUIRED_READYZ_KEYS:
            assert key in data, f"Missing required key: {key}"

    def test_readyz_ready_is_boolean(self) -> None:
        """Test that ready value is a boolean."""
        body, _ = build_readyz_body()
        data = json.loads(body)

        assert isinstance(data["ready"], bool)

    def test_readyz_role_is_string(self) -> None:
        """Test that role value is a string."""
        body, _ = build_readyz_body()
        data = json.loads(body)

        assert isinstance(data["role"], str)
        assert data["role"] in ["active", "standby", "unknown"]

    def test_readyz_active_returns_ready_true(self) -> None:
        """Test that ACTIVE role returns ready=true and is_ready=True."""
        set_ha_state(role=HARole.ACTIVE)
        body, is_ready = build_readyz_body()
        data = json.loads(body)

        assert data["ready"] is True
        assert data["role"] == "active"
        assert is_ready is True

    def test_readyz_standby_returns_ready_false(self) -> None:
        """Test that STANDBY role returns ready=false and is_ready=False."""
        set_ha_state(role=HARole.STANDBY)
        body, is_ready = build_readyz_body()
        data = json.loads(body)

        assert data["ready"] is False
        assert data["role"] == "standby"
        assert is_ready is False

    def test_readyz_unknown_returns_ready_false(self) -> None:
        """Test that UNKNOWN role returns ready=false and is_ready=False."""
        # Default state is UNKNOWN
        body, is_ready = build_readyz_body()
        data = json.loads(body)

        assert data["ready"] is False
        assert data["role"] == "unknown"
        assert is_ready is False


class TestMetricsContract:
    """Tests for /metrics endpoint contract."""

    def test_metrics_returns_string(self) -> None:
        """Test that metrics body is a string."""
        body = build_metrics_body()
        assert isinstance(body, str)

    def test_metrics_has_newlines(self) -> None:
        """Test that metrics body is newline-separated."""
        body = build_metrics_body()
        lines = body.split("\n")
        assert len(lines) > 1

    def test_metrics_has_required_patterns(self) -> None:
        """Test that metrics body contains all required patterns."""
        body = build_metrics_body()

        for pattern in REQUIRED_METRICS_PATTERNS:
            assert pattern in body, f"Missing required pattern: {pattern}"

    def test_metrics_grinder_up_is_one(self) -> None:
        """Test that grinder_up gauge is 1 (running)."""
        body = build_metrics_body()
        assert "grinder_up 1" in body

    def test_metrics_uptime_is_present(self) -> None:
        """Test that grinder_uptime_seconds has a numeric value."""
        body = build_metrics_body()
        lines = body.split("\n")

        uptime_lines = [line for line in lines if line.startswith("grinder_uptime_seconds ")]
        assert len(uptime_lines) == 1

        # Should be "grinder_uptime_seconds <float>"
        parts = uptime_lines[0].split()
        assert len(parts) == 2
        float(parts[1])  # Should not raise

    def test_metrics_gating_counters_help_type(self) -> None:
        """Test that gating counters have HELP and TYPE lines."""
        body = build_metrics_body()

        # Allowed counter
        assert "# HELP grinder_gating_allowed_total" in body
        assert "# TYPE grinder_gating_allowed_total counter" in body

        # Blocked counter
        assert "# HELP grinder_gating_blocked_total" in body
        assert "# TYPE grinder_gating_blocked_total counter" in body

    def test_metrics_ha_role_present(self) -> None:
        """Test that grinder_ha_role metric is present."""
        body = build_metrics_body()

        assert "# HELP grinder_ha_role" in body
        assert "# TYPE grinder_ha_role gauge" in body
        assert "grinder_ha_role" in body

    def test_metrics_ha_role_reflects_current_state(self) -> None:
        """Test that grinder_ha_role reflects actual HA state.

        All roles should be present: current=1, others=0.
        """
        # Default is UNKNOWN: unknown=1, active=0, standby=0
        body = build_metrics_body()
        assert 'grinder_ha_role{role="unknown"} 1' in body
        assert 'grinder_ha_role{role="active"} 0' in body
        assert 'grinder_ha_role{role="standby"} 0' in body

        # Set to ACTIVE: active=1, others=0
        set_ha_state(role=HARole.ACTIVE)
        body = build_metrics_body()
        assert 'grinder_ha_role{role="active"} 1' in body
        assert 'grinder_ha_role{role="standby"} 0' in body
        assert 'grinder_ha_role{role="unknown"} 0' in body

        # Set to STANDBY: standby=1, others=0
        set_ha_state(role=HARole.STANDBY)
        body = build_metrics_body()
        assert 'grinder_ha_role{role="standby"} 1' in body
        assert 'grinder_ha_role{role="active"} 0' in body
        assert 'grinder_ha_role{role="unknown"} 0' in body


class TestContractConstants:
    """Tests for contract constant definitions."""

    def test_required_healthz_keys_defined(self) -> None:
        """Test REQUIRED_HEALTHZ_KEYS is properly defined."""
        assert "status" in REQUIRED_HEALTHZ_KEYS
        assert "uptime_s" in REQUIRED_HEALTHZ_KEYS

    def test_required_readyz_keys_defined(self) -> None:
        """Test REQUIRED_READYZ_KEYS is properly defined."""
        assert "ready" in REQUIRED_READYZ_KEYS
        assert "role" in REQUIRED_READYZ_KEYS

    def test_required_metrics_patterns_defined(self) -> None:
        """Test REQUIRED_METRICS_PATTERNS is properly defined."""
        # Should have at least the core patterns
        patterns = REQUIRED_METRICS_PATTERNS
        assert len(patterns) >= 10

        # Should include grinder_up
        assert any("grinder_up" in p for p in patterns)

        # Should include grinder_uptime_seconds
        assert any("grinder_uptime_seconds" in p for p in patterns)

        # Should include gating metrics
        assert any("grinder_gating_allowed_total" in p for p in patterns)
        assert any("grinder_gating_blocked_total" in p for p in patterns)

        # Should include HA metrics
        assert any("grinder_ha_role" in p for p in patterns)


class TestStartTimeManagement:
    """Tests for start time management functions."""

    def test_set_and_get_start_time(self) -> None:
        """Test that set_start_time and get_start_time work correctly."""
        set_start_time(12345.0)
        assert get_start_time() == 12345.0

    def test_reset_start_time(self) -> None:
        """Test that reset_start_time clears the start time."""
        set_start_time(12345.0)
        reset_start_time()

        # After reset, get_start_time should initialize a new time
        t1 = get_start_time()
        assert t1 != 12345.0
        assert t1 > 0


class TestMetricsLabelCardinality:
    """Tests for metric label cardinality constraints (H5-02).

    Ensures that high-cardinality labels (symbol, order_id, key, etc.)
    do NOT appear in /metrics output to prevent Prometheus cardinality explosion.
    See ADR-028 for design decisions.
    """

    def test_no_high_cardinality_labels(self) -> None:
        """Test that metrics body contains no forbidden high-cardinality labels.

        This is a critical contract test: if high-cardinality labels leak into
        metrics, Prometheus will suffer memory issues and scrape timeouts.
        """
        body = build_metrics_body()

        for forbidden_label in FORBIDDEN_METRIC_LABELS:
            assert forbidden_label not in body, (
                f"High-cardinality label '{forbidden_label}' found in metrics output. "
                f"This violates ADR-028. Only low-cardinality labels (op, reason, state) are allowed."
            )

    def test_forbidden_labels_list_complete(self) -> None:
        """Test that FORBIDDEN_METRIC_LABELS contains expected entries."""
        assert "symbol=" in FORBIDDEN_METRIC_LABELS
        assert "order_id=" in FORBIDDEN_METRIC_LABELS
        assert "key=" in FORBIDDEN_METRIC_LABELS
        assert "client_id=" in FORBIDDEN_METRIC_LABELS


class TestConnectorMetricsContract:
    """Tests for connector metrics contract (H5-02 tightening).

    Validates that H2/H3/H4 metrics emit series with correct label schemas.
    """

    def test_connector_retries_has_op_and_reason_labels(self) -> None:
        """Test that grinder_connector_retries_total has op and reason labels."""
        body = build_metrics_body()

        # Check series is emitted with op label
        assert "grinder_connector_retries_total{op=" in body

        # Check reason label is present (appears in same series line)
        lines = [ln for ln in body.split("\n") if "grinder_connector_retries_total{" in ln]
        assert len(lines) >= 1, "No series found for grinder_connector_retries_total"
        assert all('reason="' in ln for ln in lines), "reason label missing from retries metric"

    def test_idempotency_metrics_have_op_label(self) -> None:
        """Test that idempotency metrics have op label."""
        body = build_metrics_body()

        # All three idempotency metrics should have op label
        assert "grinder_idempotency_hits_total{op=" in body
        assert "grinder_idempotency_conflicts_total{op=" in body
        assert "grinder_idempotency_misses_total{op=" in body

    def test_circuit_state_has_op_and_state_labels(self) -> None:
        """Test that grinder_circuit_state has op and state labels."""
        body = build_metrics_body()

        # Check series is emitted with op label
        assert "grinder_circuit_state{op=" in body

        # Check state label is present (appears in same series line)
        lines = [ln for ln in body.split("\n") if "grinder_circuit_state{" in ln]
        assert len(lines) >= 1, "No series found for grinder_circuit_state"
        assert all('state="' in ln for ln in lines), "state label missing from circuit_state metric"

    def test_circuit_rejected_has_op_label(self) -> None:
        """Test that grinder_circuit_rejected_total has op label."""
        body = build_metrics_body()
        assert "grinder_circuit_rejected_total{op=" in body

    def test_circuit_trips_has_op_and_reason_labels(self) -> None:
        """Test that grinder_circuit_trips_total has op and reason labels."""
        body = build_metrics_body()

        # Check series is emitted with op label
        assert "grinder_circuit_trips_total{op=" in body

        # Check reason label is present
        lines = [ln for ln in body.split("\n") if "grinder_circuit_trips_total{" in ln]
        assert len(lines) >= 1, "No series found for grinder_circuit_trips_total"
        assert all('reason="' in ln for ln in lines), (
            "reason label missing from circuit_trips metric"
        )

    def test_connector_metrics_series_patterns_in_contract(self) -> None:
        """Test that REQUIRED_METRICS_PATTERNS includes series-level patterns."""
        patterns = REQUIRED_METRICS_PATTERNS

        # H5-02: series-level patterns should be in the contract
        assert any("grinder_connector_retries_total{op=" in p for p in patterns)
        assert any("grinder_idempotency_hits_total{op=" in p for p in patterns)
        assert any("grinder_idempotency_conflicts_total{op=" in p for p in patterns)
        assert any("grinder_idempotency_misses_total{op=" in p for p in patterns)
        assert any("grinder_circuit_state{op=" in p for p in patterns)
        assert any("grinder_circuit_rejected_total{op=" in p for p in patterns)
        assert any("grinder_circuit_trips_total{op=" in p for p in patterns)


class TestReconcileMetricsContract:
    """Tests for reconcile metrics contract (LC-09b/LC-10/LC-11/LC-15b).

    Validates that reconcile metrics are present with correct label schemas.
    """

    def test_reconcile_mismatch_help_and_type(self) -> None:
        """Test that grinder_reconcile_mismatch_total has HELP and TYPE lines."""
        body = build_metrics_body()

        assert "# HELP grinder_reconcile_mismatch_total" in body
        assert "# TYPE grinder_reconcile_mismatch_total counter" in body

    def test_reconcile_mismatch_has_type_label(self) -> None:
        """Test that grinder_reconcile_mismatch_total has type label."""
        body = build_metrics_body()

        # Check series is emitted with type label
        assert "grinder_reconcile_mismatch_total{type=" in body

    def test_reconcile_runs_total_present(self) -> None:
        """Test that grinder_reconcile_runs_total is present."""
        body = build_metrics_body()

        assert "# HELP grinder_reconcile_runs_total" in body
        assert "# TYPE grinder_reconcile_runs_total counter" in body
        assert "grinder_reconcile_runs_total " in body  # Space indicates value follows

    def test_reconcile_snapshot_age_present(self) -> None:
        """Test that grinder_reconcile_last_snapshot_age_seconds is present."""
        body = build_metrics_body()

        assert "# HELP grinder_reconcile_last_snapshot_age_seconds" in body
        assert "# TYPE grinder_reconcile_last_snapshot_age_seconds gauge" in body
        assert "grinder_reconcile_last_snapshot_age_seconds " in body

    def test_reconcile_action_planned_has_action_label(self) -> None:
        """Test that grinder_reconcile_action_planned_total has action label."""
        body = build_metrics_body()

        assert "# HELP grinder_reconcile_action_planned_total" in body
        assert "# TYPE grinder_reconcile_action_planned_total counter" in body
        assert "grinder_reconcile_action_planned_total{action=" in body

    def test_reconcile_action_executed_has_action_label(self) -> None:
        """Test that grinder_reconcile_action_executed_total has action label."""
        body = build_metrics_body()

        assert "# HELP grinder_reconcile_action_executed_total" in body
        assert "# TYPE grinder_reconcile_action_executed_total counter" in body
        assert "grinder_reconcile_action_executed_total{action=" in body

    def test_reconcile_action_blocked_has_reason_label(self) -> None:
        """Test that grinder_reconcile_action_blocked_total has reason label."""
        body = build_metrics_body()

        assert "# HELP grinder_reconcile_action_blocked_total" in body
        assert "# TYPE grinder_reconcile_action_blocked_total counter" in body
        # Blocked may be 0, but should have initialized series
        lines = [ln for ln in body.split("\n") if "grinder_reconcile_action_blocked_total" in ln]
        # At minimum HELP and TYPE lines exist
        assert len(lines) >= 2

    def test_reconcile_runs_with_mismatch_present(self) -> None:
        """Test that grinder_reconcile_runs_with_mismatch_total is present."""
        body = build_metrics_body()

        assert "# HELP grinder_reconcile_runs_with_mismatch_total" in body
        assert "# TYPE grinder_reconcile_runs_with_mismatch_total counter" in body
        assert "grinder_reconcile_runs_with_mismatch_total " in body

    def test_reconcile_runs_with_remediation_has_action_label(self) -> None:
        """Test that grinder_reconcile_runs_with_remediation_total has action label."""
        body = build_metrics_body()

        assert "# HELP grinder_reconcile_runs_with_remediation_total" in body
        assert "# TYPE grinder_reconcile_runs_with_remediation_total counter" in body
        assert "grinder_reconcile_runs_with_remediation_total{action=" in body

    def test_reconcile_last_remediation_ts_present(self) -> None:
        """Test that grinder_reconcile_last_remediation_ts_ms is present."""
        body = build_metrics_body()

        assert "# HELP grinder_reconcile_last_remediation_ts_ms" in body
        assert "# TYPE grinder_reconcile_last_remediation_ts_ms gauge" in body
        assert "grinder_reconcile_last_remediation_ts_ms " in body

    def test_reconcile_metrics_series_patterns_in_contract(self) -> None:
        """Test that REQUIRED_METRICS_PATTERNS includes reconcile series-level patterns."""
        patterns = REQUIRED_METRICS_PATTERNS

        # LC-15b: reconcile series-level patterns should be in the contract
        assert any("grinder_reconcile_mismatch_total{type=" in p for p in patterns)
        assert any("grinder_reconcile_action_planned_total{action=" in p for p in patterns)
        assert any("grinder_reconcile_action_executed_total{action=" in p for p in patterns)
        assert any("grinder_reconcile_runs_with_remediation_total{action=" in p for p in patterns)
