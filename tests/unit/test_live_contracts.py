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

from grinder.gating import reset_gating_metrics
from grinder.ha.role import HARole, reset_ha_state, set_ha_state
from grinder.observability import (
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


@pytest.fixture(autouse=True)
def reset_state() -> None:
    """Reset global state before each test."""
    reset_start_time()
    reset_gating_metrics()
    reset_metrics_builder()
    reset_ha_state()


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
