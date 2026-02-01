"""Tests for live runtime contract (healthz and metrics endpoints).

Enforces the contract defined in src/grinder/observability/live_contract.py:

GET /healthz:
    - Body is valid JSON with "status" and "uptime_s" keys
    - "status" value is "ok"

GET /metrics:
    - Body is Prometheus text format
    - Contains all required metrics with HELP/TYPE lines
"""

from __future__ import annotations

import json

import pytest

from grinder.gating import reset_gating_metrics
from grinder.observability import (
    REQUIRED_HEALTHZ_KEYS,
    REQUIRED_METRICS_PATTERNS,
    build_healthz_body,
    build_metrics_body,
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


class TestContractConstants:
    """Tests for contract constant definitions."""

    def test_required_healthz_keys_defined(self) -> None:
        """Test REQUIRED_HEALTHZ_KEYS is properly defined."""
        assert "status" in REQUIRED_HEALTHZ_KEYS
        assert "uptime_s" in REQUIRED_HEALTHZ_KEYS

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
