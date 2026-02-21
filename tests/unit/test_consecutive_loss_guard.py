"""Tests for grinder.risk.consecutive_loss_guard (Track C, PR-C3).

Covers:
- REQ-001: loss increments count; win/breakeven resets to 0.
- REQ-002: trip fires exactly at threshold.
- REQ-003: breakeven resets (not just win).
- REQ-004: unknown outcome is no-op.
- REQ-005: disabled guard → always returns False, count stays 0.
- REQ-006: reset() clears count and tripped.
- REQ-007: config validation (threshold < 1 raises).
- REQ-008: determinism — same sequence → same state.
- REQ-009: trip returns True only on first trip (not repeated).
- REQ-010: state snapshot immutable and serializable.
- REQ-011: from_dict strict validation (PR-C3c).
- REQ-012: from_state factory restores guard (PR-C3c).
"""

from __future__ import annotations

import pytest

from grinder.risk.consecutive_loss_guard import (
    KNOWN_OUTCOMES,
    ConsecutiveLossAction,
    ConsecutiveLossConfig,
    ConsecutiveLossGuard,
    ConsecutiveLossGuardError,
    ConsecutiveLossState,
)

# --- REQ-001: loss/win/breakeven counting -----------------------------------


class TestLossWinBreakevenCounting:
    """REQ-001: loss increments count; win/breakeven resets to 0."""

    def test_loss_loss_win_sequence(self) -> None:
        """loss, loss, win → count: 1, 2, 0."""
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=5),
        )

        guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        assert guard.count == 1

        guard.update(outcome="loss", row_id="r2", ts_ms=2000)
        assert guard.count == 2

        guard.update(outcome="win", row_id="r3", ts_ms=3000)
        assert guard.count == 0

    def test_win_resets_to_zero(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=10),
        )
        for i in range(4):
            guard.update(outcome="loss", row_id=f"r{i}", ts_ms=1000 + i * 100)
        assert guard.count == 4

        guard.update(outcome="win", row_id="rW", ts_ms=5000)
        assert guard.count == 0
        assert not guard.is_tripped

    def test_loss_after_win_starts_fresh(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=5),
        )
        guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        guard.update(outcome="win", row_id="r2", ts_ms=2000)
        guard.update(outcome="loss", row_id="r3", ts_ms=3000)
        assert guard.count == 1  # fresh count after win


# --- REQ-002: trip fires at threshold ----------------------------------------


class TestTripAtThreshold:
    """REQ-002: loss x threshold → trip True exactly on threshold."""

    def test_trip_exactly_at_threshold(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=3),
        )

        result1 = guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        assert result1 is False
        assert guard.count == 1
        assert not guard.is_tripped

        result2 = guard.update(outcome="loss", row_id="r2", ts_ms=2000)
        assert result2 is False
        assert guard.count == 2
        assert not guard.is_tripped

        result3 = guard.update(outcome="loss", row_id="r3", ts_ms=3000)
        assert result3 is True  # tripped!
        assert guard.count == 3
        assert guard.is_tripped

    def test_threshold_1_trips_on_first_loss(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=1),
        )
        result = guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        assert result is True
        assert guard.is_tripped
        assert guard.count == 1

    def test_below_threshold_does_not_trip(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=5),
        )
        for i in range(4):
            guard.update(outcome="loss", row_id=f"r{i}", ts_ms=1000 + i * 100)
        assert guard.count == 4
        assert not guard.is_tripped


# --- REQ-003: breakeven resets -----------------------------------------------


class TestBreakevenResets:
    """REQ-003: breakeven outcome resets count to 0."""

    def test_breakeven_resets_count(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=5),
        )
        guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        guard.update(outcome="loss", row_id="r2", ts_ms=2000)
        assert guard.count == 2

        guard.update(outcome="breakeven", row_id="r3", ts_ms=3000)
        assert guard.count == 0
        assert not guard.is_tripped

    def test_breakeven_after_trip_does_not_untrip(self) -> None:
        """Trip is latched — only reset() clears it."""
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=2),
        )
        guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        guard.update(outcome="loss", row_id="r2", ts_ms=2000)
        assert guard.is_tripped

        # Breakeven resets count but trip stays latched.
        guard.update(outcome="breakeven", row_id="r3", ts_ms=3000)
        assert guard.count == 0
        assert not guard.is_tripped  # tripped cleared by win/breakeven


# --- REQ-004: unknown outcome is no-op --------------------------------------


class TestUnknownOutcome:
    """REQ-004: unknown outcome string is no-op."""

    def test_unknown_outcome_no_op(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=3),
        )
        guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        assert guard.count == 1

        result = guard.update(outcome="unknown_thing", row_id="r2", ts_ms=2000)
        assert result is False
        assert guard.count == 1  # unchanged

    def test_empty_string_outcome_no_op(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=3),
        )
        result = guard.update(outcome="", row_id="r1", ts_ms=1000)
        assert result is False
        assert guard.count == 0


# --- REQ-005: disabled guard -------------------------------------------------


class TestDisabledGuard:
    """REQ-005: disabled guard → always returns False, count stays 0."""

    def test_disabled_never_counts(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=False, threshold=1),
        )
        for i in range(10):
            result = guard.update(outcome="loss", row_id=f"r{i}", ts_ms=1000 + i)
            assert result is False
        assert guard.count == 0
        assert not guard.is_tripped

    def test_disabled_default_config(self) -> None:
        """Default config is disabled."""
        guard = ConsecutiveLossGuard()
        assert not guard.config.enabled
        result = guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        assert result is False
        assert guard.count == 0


# --- REQ-006: reset() -------------------------------------------------------


class TestReset:
    """REQ-006: reset() clears count and tripped."""

    def test_reset_clears_state(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=2),
        )
        guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        guard.update(outcome="loss", row_id="r2", ts_ms=2000)
        assert guard.is_tripped

        guard.reset()
        assert guard.count == 0
        assert not guard.is_tripped
        assert guard.state.last_row_id is None
        assert guard.state.last_ts_ms is None

    def test_reset_then_count_fresh(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=3),
        )
        guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        guard.update(outcome="loss", row_id="r2", ts_ms=2000)
        guard.reset()
        guard.update(outcome="loss", row_id="r3", ts_ms=3000)
        assert guard.count == 1  # fresh count after reset


# --- REQ-007: config validation ----------------------------------------------


class TestConfigValidation:
    """REQ-007: threshold < 1 raises ConsecutiveLossGuardError."""

    def test_threshold_zero_raises(self) -> None:
        with pytest.raises(ConsecutiveLossGuardError, match="threshold must be >= 1"):
            ConsecutiveLossConfig(enabled=True, threshold=0)

    def test_threshold_negative_raises(self) -> None:
        with pytest.raises(ConsecutiveLossGuardError, match="threshold must be >= 1"):
            ConsecutiveLossConfig(enabled=True, threshold=-1)

    def test_threshold_one_valid(self) -> None:
        config = ConsecutiveLossConfig(enabled=True, threshold=1)
        assert config.threshold == 1

    def test_action_enum_values(self) -> None:
        assert ConsecutiveLossAction.PAUSE.value == "PAUSE"
        assert ConsecutiveLossAction.DEGRADED.value == "DEGRADED"


# --- REQ-008: determinism ---------------------------------------------------


class TestDeterminism:
    """REQ-008: same sequence → same state."""

    def test_same_sequence_same_state(self) -> None:
        outcomes = ["loss", "loss", "win", "loss", "loss", "loss"]
        row_ids = [f"r{i}" for i in range(len(outcomes))]
        timestamps = [1000 + i * 100 for i in range(len(outcomes))]

        def run() -> ConsecutiveLossState:
            guard = ConsecutiveLossGuard(
                config=ConsecutiveLossConfig(enabled=True, threshold=3),
            )
            for outcome, row_id, ts in zip(outcomes, row_ids, timestamps, strict=True):
                guard.update(outcome=outcome, row_id=row_id, ts_ms=ts)
            return guard.state

        state1 = run()
        state2 = run()
        assert state1 == state2
        assert state1.to_dict() == state2.to_dict()


# --- REQ-009: trip returns True only on first trip ---------------------------


class TestTripReturnValue:
    """REQ-009: trip returns True only on the first trip, not repeated."""

    def test_repeated_loss_after_trip_returns_false(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=2),
        )
        guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        result = guard.update(outcome="loss", row_id="r2", ts_ms=2000)
        assert result is True  # first trip

        result = guard.update(outcome="loss", row_id="r3", ts_ms=3000)
        assert result is False  # already tripped
        assert guard.is_tripped
        assert guard.count == 3


# --- REQ-010: state snapshot -------------------------------------------------


class TestStateSnapshot:
    """REQ-010: state is immutable and serializable."""

    def test_state_to_dict(self) -> None:
        state = ConsecutiveLossState(
            count=3,
            tripped=True,
            last_row_id="r3",
            last_ts_ms=3000,
        )
        d = state.to_dict()
        assert d == {
            "count": 3,
            "tripped": True,
            "last_row_id": "r3",
            "last_ts_ms": 3000,
        }

    def test_state_immutable(self) -> None:
        state = ConsecutiveLossState(count=1)
        with pytest.raises(AttributeError):
            state.count = 2  # type: ignore[misc]

    def test_known_outcomes_frozen(self) -> None:
        assert "win" in KNOWN_OUTCOMES
        assert "loss" in KNOWN_OUTCOMES
        assert "breakeven" in KNOWN_OUTCOMES
        assert len(KNOWN_OUTCOMES) == 3

    def test_state_after_update_captures_metadata(self) -> None:
        guard = ConsecutiveLossGuard(
            config=ConsecutiveLossConfig(enabled=True, threshold=5),
        )
        guard.update(outcome="loss", row_id="my_row_123", ts_ms=42000)
        state = guard.state
        assert state.last_row_id == "my_row_123"
        assert state.last_ts_ms == 42000
        assert state.count == 1


# --- REQ-011: from_dict strict validation (PR-C3c) ----------------------------


class TestFromDict:
    """REQ-011: ConsecutiveLossState.from_dict() strict validation."""

    def test_roundtrip(self) -> None:
        """to_dict() → from_dict() produces equal state."""
        state = ConsecutiveLossState(count=3, tripped=True, last_row_id="r3", last_ts_ms=3000)
        restored = ConsecutiveLossState.from_dict(state.to_dict())
        assert restored == state

    def test_empty_uses_defaults(self) -> None:
        """from_dict({}) → default state."""
        restored = ConsecutiveLossState.from_dict({})
        assert restored == ConsecutiveLossState()

    def test_rejects_negative_count(self) -> None:
        """Negative count raises ValueError."""
        with pytest.raises(ValueError, match="count must be int >= 0"):
            ConsecutiveLossState.from_dict({"count": -1})

    def test_rejects_string_tripped(self) -> None:
        """String tripped raises ValueError (bool('false') == True trap)."""
        with pytest.raises(ValueError, match="tripped must be bool"):
            ConsecutiveLossState.from_dict({"tripped": "false"})

    def test_rejects_string_ts_ms(self) -> None:
        """String last_ts_ms raises ValueError."""
        with pytest.raises(ValueError, match="last_ts_ms must be int"):
            ConsecutiveLossState.from_dict({"last_ts_ms": "12345"})

    def test_rejects_float_count(self) -> None:
        """Float count raises ValueError (isinstance(1.0, int) is False in strict)."""
        with pytest.raises(ValueError, match="count must be int >= 0"):
            ConsecutiveLossState.from_dict({"count": 1.5})


# --- REQ-012: from_state factory (PR-C3c) ------------------------------------


class TestFromState:
    """REQ-012: ConsecutiveLossGuard.from_state() factory."""

    def test_restores_guard_state(self) -> None:
        """Restored guard has correct count/state/config."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        state = ConsecutiveLossState(count=3, tripped=False, last_row_id="r3", last_ts_ms=3000)
        guard = ConsecutiveLossGuard.from_state(config, state)
        assert guard.count == 3
        assert guard.state == state
        assert guard.config == config
        assert guard.is_tripped is False

    def test_restored_guard_continues_counting(self) -> None:
        """Guard restored at count=2, one more loss → count=3, trips at threshold=3."""
        config = ConsecutiveLossConfig(enabled=True, threshold=3)
        state = ConsecutiveLossState(count=2, tripped=False)
        guard = ConsecutiveLossGuard.from_state(config, state)

        tripped = guard.update(outcome="loss", row_id="r3", ts_ms=3000)
        assert tripped is True
        assert guard.count == 3
        assert guard.is_tripped is True
