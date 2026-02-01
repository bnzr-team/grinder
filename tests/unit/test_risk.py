"""Unit tests for risk management components.

Tests DrawdownGuard and KillSwitch:
- HWM tracking
- Drawdown calculation
- Trigger conditions
- Kill-switch latching behavior
- Idempotency
- Reset semantics

See: ADR-013 for design decisions
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.risk import (
    DrawdownCheckResult,
    DrawdownGuard,
    KillSwitch,
    KillSwitchReason,
    KillSwitchState,
)


class TestKillSwitch:
    """Unit tests for KillSwitch."""

    def test_initial_state_not_triggered(self) -> None:
        """Kill-switch starts in non-triggered state."""
        ks = KillSwitch()
        assert not ks.is_triggered
        assert ks.state.triggered is False
        assert ks.state.reason is None
        assert ks.state.triggered_at_ts is None

    def test_trip_activates_kill_switch(self) -> None:
        """Tripping sets triggered state with reason."""
        ks = KillSwitch()

        state = ks.trip(KillSwitchReason.DRAWDOWN_LIMIT, ts=1000)

        assert ks.is_triggered
        assert state.triggered is True
        assert state.reason == KillSwitchReason.DRAWDOWN_LIMIT
        assert state.triggered_at_ts == 1000

    def test_trip_is_idempotent(self) -> None:
        """Tripping twice returns same state (no change)."""
        ks = KillSwitch()

        # First trip
        state1 = ks.trip(KillSwitchReason.DRAWDOWN_LIMIT, ts=1000)

        # Second trip with different reason and timestamp
        state2 = ks.trip(KillSwitchReason.MANUAL, ts=2000)

        # Should keep original reason and timestamp
        assert state2.reason == KillSwitchReason.DRAWDOWN_LIMIT
        assert state2.triggered_at_ts == 1000
        assert state1 == state2

    def test_trip_with_details(self) -> None:
        """Trip can include additional details."""
        ks = KillSwitch()

        details = {"equity": "9500", "drawdown_pct": 5.0}
        state = ks.trip(KillSwitchReason.DRAWDOWN_LIMIT, ts=1000, details=details)

        assert state.details == details

    def test_reset_clears_state(self) -> None:
        """Reset returns to non-triggered state."""
        ks = KillSwitch()
        ks.trip(KillSwitchReason.DRAWDOWN_LIMIT, ts=1000)

        ks.reset()

        assert not ks.is_triggered
        assert ks.state.reason is None
        assert ks.state.triggered_at_ts is None

    def test_state_to_dict(self) -> None:
        """State serializes to dict correctly."""
        ks = KillSwitch()
        ks.trip(KillSwitchReason.DRAWDOWN_LIMIT, ts=1000, details={"key": "value"})

        d = ks.state.to_dict()

        assert d["triggered"] is True
        assert d["reason"] == "DRAWDOWN_LIMIT"
        assert d["triggered_at_ts"] == 1000
        assert d["details"] == {"key": "value"}

    def test_untriggered_state_to_dict(self) -> None:
        """Untriggered state serializes with None values."""
        ks = KillSwitch()

        d = ks.state.to_dict()

        assert d["triggered"] is False
        assert d["reason"] is None
        assert d["triggered_at_ts"] is None


class TestDrawdownGuard:
    """Unit tests for DrawdownGuard."""

    def test_init_with_valid_params(self) -> None:
        """Guard initializes with valid parameters."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        assert guard.high_water_mark == Decimal("10000")
        assert guard.max_drawdown_pct == 5.0
        assert not guard.is_triggered

    def test_init_rejects_zero_capital(self) -> None:
        """Guard rejects zero initial capital."""
        with pytest.raises(ValueError, match="initial_capital must be positive"):
            DrawdownGuard(initial_capital=Decimal("0"), max_drawdown_pct=5.0)

    def test_init_rejects_negative_capital(self) -> None:
        """Guard rejects negative initial capital."""
        with pytest.raises(ValueError, match="initial_capital must be positive"):
            DrawdownGuard(initial_capital=Decimal("-1000"), max_drawdown_pct=5.0)

    def test_init_rejects_invalid_drawdown_pct(self) -> None:
        """Guard rejects invalid drawdown percentage."""
        with pytest.raises(ValueError, match="max_drawdown_pct must be between"):
            DrawdownGuard(initial_capital=Decimal("10000"), max_drawdown_pct=0)

        with pytest.raises(ValueError, match="max_drawdown_pct must be between"):
            DrawdownGuard(initial_capital=Decimal("10000"), max_drawdown_pct=101)

    def test_hwm_updates_on_equity_increase(self) -> None:
        """HWM updates when equity exceeds current HWM."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        # Equity above initial capital
        result = guard.update(Decimal("10500"))

        assert guard.high_water_mark == Decimal("10500")
        assert result.high_water_mark == Decimal("10500")
        assert result.drawdown_pct == 0.0

    def test_hwm_unchanged_on_equity_decrease(self) -> None:
        """HWM stays unchanged when equity drops."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        # Equity below initial capital
        result = guard.update(Decimal("9800"))

        assert guard.high_water_mark == Decimal("10000")
        assert result.high_water_mark == Decimal("10000")

    def test_drawdown_calculation(self) -> None:
        """Drawdown percentage is calculated correctly."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=10.0,  # 10% threshold
        )

        # 5% drawdown: (10000 - 9500) / 10000 * 100 = 5%
        result = guard.update(Decimal("9500"))

        assert result.drawdown_pct == pytest.approx(5.0)
        assert not result.triggered

    def test_trigger_at_exact_threshold(self) -> None:
        """Guard triggers at exactly the threshold."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        # Exactly 5% drawdown: (10000 - 9500) / 10000 * 100 = 5%
        result = guard.update(Decimal("9500"))

        assert result.drawdown_pct == pytest.approx(5.0)
        assert result.triggered is True
        assert guard.is_triggered

    def test_no_trigger_just_below_threshold(self) -> None:
        """Guard does NOT trigger just below threshold."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        # 4.99% drawdown: (10000 - 9501) / 10000 * 100 = 4.99%
        result = guard.update(Decimal("9501"))

        assert result.drawdown_pct == pytest.approx(4.99)
        assert result.triggered is False
        assert not guard.is_triggered

    def test_trigger_above_threshold(self) -> None:
        """Guard triggers when drawdown exceeds threshold."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        # Equity at 9400 causes 6 percent drawdown from 10000 HWM
        result = guard.update(Decimal("9400"))

        assert result.drawdown_pct == pytest.approx(6.0)
        assert result.triggered is True
        assert guard.is_triggered
        assert result.details is not None
        assert "trigger_equity" in result.details

    def test_trigger_latches(self) -> None:
        """Once triggered, guard stays triggered."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        # Trigger
        guard.update(Decimal("9400"))
        assert guard.is_triggered

        # Equity recovers
        result = guard.update(Decimal("10500"))

        # Still triggered (latched)
        assert guard.is_triggered
        assert result.triggered is True
        assert result.details is not None
        assert result.details.get("previously_triggered") is True

    def test_reset_clears_trigger(self) -> None:
        """Reset clears triggered state and resets HWM."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        # Trigger
        guard.update(Decimal("9400"))
        assert guard.is_triggered

        # Reset
        guard.reset()

        assert not guard.is_triggered
        assert guard.high_water_mark == Decimal("10000")

    def test_reset_with_new_capital(self) -> None:
        """Reset can set new initial capital."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        guard.reset(initial_capital=Decimal("20000"))

        assert guard.high_water_mark == Decimal("20000")

    def test_drawdown_check_result_to_dict(self) -> None:
        """DrawdownCheckResult serializes correctly."""
        result = DrawdownCheckResult(
            equity=Decimal("9500"),
            high_water_mark=Decimal("10000"),
            drawdown_pct=5.0,
            threshold_pct=5.0,
            triggered=True,
            details={"key": "value"},
        )

        d = result.to_dict()

        assert d["equity"] == "9500"
        assert d["high_water_mark"] == "10000"
        assert d["drawdown_pct"] == 5.0
        assert d["threshold_pct"] == 5.0
        assert d["triggered"] is True
        assert d["details"] == {"key": "value"}

    def test_hwm_tracks_progressive_gains(self) -> None:
        """HWM updates through progressive gains."""
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,
        )

        # Progressive gains
        guard.update(Decimal("10100"))
        assert guard.high_water_mark == Decimal("10100")

        guard.update(Decimal("10200"))
        assert guard.high_water_mark == Decimal("10200")

        guard.update(Decimal("10300"))
        assert guard.high_water_mark == Decimal("10300")

        # Now drawdown from new HWM (10300)
        # 5% of 10300 = 515, so trigger at 10300 - 515 = 9785
        result = guard.update(Decimal("9785"))
        assert result.drawdown_pct == pytest.approx(5.0, rel=0.01)
        assert result.triggered is True


class TestKillSwitchReason:
    """Tests for KillSwitchReason enum."""

    def test_reason_values(self) -> None:
        """Reason enum has expected values."""
        assert KillSwitchReason.DRAWDOWN_LIMIT.value == "DRAWDOWN_LIMIT"
        assert KillSwitchReason.MANUAL.value == "MANUAL"
        assert KillSwitchReason.ERROR.value == "ERROR"


class TestKillSwitchState:
    """Tests for KillSwitchState dataclass."""

    def test_default_state(self) -> None:
        """Default state is untriggered."""
        state = KillSwitchState()
        assert state.triggered is False
        assert state.reason is None
        assert state.triggered_at_ts is None
        assert state.details is None

    def test_triggered_state(self) -> None:
        """Triggered state stores all fields."""
        state = KillSwitchState(
            triggered=True,
            reason=KillSwitchReason.DRAWDOWN_LIMIT,
            triggered_at_ts=1234567890,
            details={"equity": "9500"},
        )

        assert state.triggered is True
        assert state.reason == KillSwitchReason.DRAWDOWN_LIMIT
        assert state.triggered_at_ts == 1234567890
        assert state.details == {"equity": "9500"}
