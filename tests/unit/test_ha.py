"""Tests for HA (High Availability) module.

Tests cover:
- HARole enum and HAState
- Fail-safe semantics (lock loss → STANDBY → /readyz 503)
- Thread-safe state management
"""

from __future__ import annotations

import pytest

from grinder.ha.leader import LeaderElectorConfig
from grinder.ha.role import HARole, HAState, get_ha_state, reset_ha_state, set_ha_state
from grinder.observability import build_readyz_body


@pytest.fixture(autouse=True)
def reset_state() -> None:
    """Reset HA state before each test."""
    reset_ha_state()


class TestHARole:
    """Tests for HARole enum."""

    def test_role_values(self) -> None:
        """Test HARole enum has expected values."""
        assert HARole.ACTIVE.value == "active"
        assert HARole.STANDBY.value == "standby"
        assert HARole.UNKNOWN.value == "unknown"

    def test_all_roles_defined(self) -> None:
        """Test all expected roles exist."""
        roles = list(HARole)
        assert len(roles) == 3
        assert HARole.ACTIVE in roles
        assert HARole.STANDBY in roles
        assert HARole.UNKNOWN in roles


class TestHAState:
    """Tests for HAState dataclass."""

    def test_default_state(self) -> None:
        """Test HAState default values."""
        state = HAState()
        assert state.role == HARole.UNKNOWN
        assert state.instance_id == ""
        assert state.lock_holder is None
        assert state.last_lock_attempt_ms == 0
        assert state.lock_failures == 0

    def test_state_with_values(self) -> None:
        """Test HAState with custom values."""
        state = HAState(
            role=HARole.ACTIVE,
            instance_id="test-123",
            lock_holder="test-123",
            last_lock_attempt_ms=1000,
            lock_failures=0,
        )
        assert state.role == HARole.ACTIVE
        assert state.instance_id == "test-123"


class TestHAStateManagement:
    """Tests for thread-safe state management functions."""

    def test_initial_state_is_unknown(self) -> None:
        """Test that initial HA state is UNKNOWN."""
        state = get_ha_state()
        assert state.role == HARole.UNKNOWN

    def test_set_role(self) -> None:
        """Test setting HA role."""
        set_ha_state(role=HARole.ACTIVE)
        state = get_ha_state()
        assert state.role == HARole.ACTIVE

    def test_set_multiple_fields(self) -> None:
        """Test setting multiple fields at once."""
        set_ha_state(
            role=HARole.STANDBY,
            instance_id="grinder-abc123",
            lock_failures=3,
        )
        state = get_ha_state()
        assert state.role == HARole.STANDBY
        assert state.instance_id == "grinder-abc123"
        assert state.lock_failures == 3

    def test_partial_update_preserves_other_fields(self) -> None:
        """Test that partial updates don't overwrite unspecified fields."""
        set_ha_state(role=HARole.ACTIVE, instance_id="test-1")
        set_ha_state(role=HARole.STANDBY)  # Only update role

        state = get_ha_state()
        assert state.role == HARole.STANDBY
        assert state.instance_id == "test-1"  # Preserved

    def test_reset_state(self) -> None:
        """Test reset_ha_state returns to defaults."""
        set_ha_state(role=HARole.ACTIVE, instance_id="test", lock_failures=5)
        reset_ha_state()

        state = get_ha_state()
        assert state.role == HARole.UNKNOWN
        assert state.instance_id == ""
        assert state.lock_failures == 0


class TestFailSafeSemantics:
    """Tests for fail-safe behavior.

    Critical for single-active safety:
    - STANDBY/UNKNOWN → /readyz returns 503 (not ready)
    - Only ACTIVE → /readyz returns 200 (ready)
    """

    def test_unknown_role_not_ready(self) -> None:
        """Test UNKNOWN role returns not ready (fail-safe default)."""
        # Initial state is UNKNOWN
        _, is_ready = build_readyz_body()
        assert is_ready is False

    def test_standby_role_not_ready(self) -> None:
        """Test STANDBY role returns not ready.

        This is the key fail-safe behavior:
        - When lock is lost → become STANDBY
        - STANDBY → /readyz 503 → not ready for traffic
        """
        set_ha_state(role=HARole.STANDBY)
        _, is_ready = build_readyz_body()
        assert is_ready is False

    def test_only_active_is_ready(self) -> None:
        """Test only ACTIVE role returns ready."""
        set_ha_state(role=HARole.ACTIVE)
        _, is_ready = build_readyz_body()
        assert is_ready is True

    def test_lock_loss_scenario(self) -> None:
        """Simulate lock loss: ACTIVE → STANDBY → not ready.

        This tests the fail-safe transition that prevents split-brain.
        """
        # Start as ACTIVE (holding lock)
        set_ha_state(role=HARole.ACTIVE)
        _, is_ready = build_readyz_body()
        assert is_ready is True

        # Lock renewal fails → become STANDBY (fail-safe)
        set_ha_state(role=HARole.STANDBY, lock_failures=1)
        _, is_ready = build_readyz_body()
        assert is_ready is False

    def test_redis_unavailable_scenario(self) -> None:
        """Simulate Redis unavailable: any role → STANDBY → not ready.

        When Redis is down, all instances should become STANDBY.
        """
        # Even if we were ACTIVE, Redis down means STANDBY
        set_ha_state(role=HARole.ACTIVE)
        # Simulate Redis connection lost
        set_ha_state(role=HARole.STANDBY, lock_failures=5)

        _, is_ready = build_readyz_body()
        assert is_ready is False


class TestLeaderElectorConfig:
    """Tests for LeaderElectorConfig."""

    def test_default_config(self) -> None:
        """Test LeaderElectorConfig default values."""
        config = LeaderElectorConfig()
        assert config.lock_ttl_ms == 10000  # 10 seconds
        assert config.renew_interval_ms == 3000  # 3 seconds
        assert config.lock_key == "grinder:leader:lock"
        assert config.instance_id.startswith("grinder-")

    def test_renew_must_be_less_than_ttl(self) -> None:
        """Test that renew_interval_ms must be < lock_ttl_ms."""
        with pytest.raises(ValueError, match=r"renew_interval_ms.*must be < lock_ttl_ms"):
            LeaderElectorConfig(lock_ttl_ms=5000, renew_interval_ms=5000)

        with pytest.raises(ValueError, match=r"renew_interval_ms.*must be < lock_ttl_ms"):
            LeaderElectorConfig(lock_ttl_ms=5000, renew_interval_ms=6000)

    def test_ttl_minimum(self) -> None:
        """Test that lock_ttl_ms has a minimum value for safety."""
        with pytest.raises(ValueError, match=r"lock_ttl_ms.*should be >= 1000ms"):
            LeaderElectorConfig(lock_ttl_ms=500)

    def test_custom_config(self) -> None:
        """Test LeaderElectorConfig with custom values."""
        config = LeaderElectorConfig(
            redis_url="redis://custom:6380/1",
            lock_key="custom:lock",
            lock_ttl_ms=15000,
            renew_interval_ms=5000,
            instance_id="custom-instance",
        )
        assert config.redis_url == "redis://custom:6380/1"
        assert config.lock_key == "custom:lock"
        assert config.lock_ttl_ms == 15000
        assert config.renew_interval_ms == 5000
        assert config.instance_id == "custom-instance"
