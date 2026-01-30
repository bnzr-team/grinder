"""Tests for core types."""

from grinder.core import GridMode, OrderSide, OrderState, SystemState, ToxicityLevel


class TestGridMode:
    """Tests for GridMode enum."""

    def test_bilateral_mode(self) -> None:
        """Test BILATERAL mode value."""
        assert GridMode.BILATERAL.value == "BILATERAL"

    def test_all_modes_defined(self) -> None:
        """Test all expected modes exist."""
        expected = {"BILATERAL", "UNI_LONG", "UNI_SHORT", "THROTTLE", "PAUSE", "EMERGENCY"}
        actual = {m.value for m in GridMode}
        assert actual == expected


class TestSystemState:
    """Tests for SystemState enum."""

    def test_init_state(self) -> None:
        """Test INIT state value."""
        assert SystemState.INIT.value == "INIT"

    def test_all_states_defined(self) -> None:
        """Test all expected states exist."""
        expected = {"INIT", "READY", "ACTIVE", "THROTTLED", "PAUSED", "DEGRADED", "EMERGENCY"}
        actual = {s.value for s in SystemState}
        assert actual == expected


class TestToxicityLevel:
    """Tests for ToxicityLevel enum."""

    def test_levels(self) -> None:
        """Test toxicity levels."""
        assert ToxicityLevel.LOW.value == "LOW"
        assert ToxicityLevel.MID.value == "MID"
        assert ToxicityLevel.HIGH.value == "HIGH"


class TestOrderSide:
    """Tests for OrderSide enum."""

    def test_sides(self) -> None:
        """Test order sides."""
        assert OrderSide.BUY.value == "BUY"
        assert OrderSide.SELL.value == "SELL"


class TestOrderState:
    """Tests for OrderState enum."""

    def test_all_states(self) -> None:
        """Test all order states exist."""
        expected = {
            "PENDING",
            "OPEN",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
        }
        actual = {s.value for s in OrderState}
        assert actual == expected
