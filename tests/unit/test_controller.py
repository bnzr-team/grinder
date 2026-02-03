"""Unit tests for Adaptive Controller v0.

These tests verify the deterministic behavior of AdaptiveController.
See: ADR-011 for decision rules and thresholds.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from grinder.controller import (
    AdaptiveController,
    ControllerDecision,
    ControllerMode,
    ControllerReason,
)
from grinder.paper import PaperEngine


class TestAdaptiveController:
    """Tests for AdaptiveController class."""

    def test_decide_empty_returns_tighten(self) -> None:
        """Deciding with no history returns TIGHTEN (vol=0 < 50)."""
        controller = AdaptiveController()
        decision = controller.decide("BTCUSDT")

        assert decision.mode == ControllerMode.TIGHTEN
        assert decision.reason == ControllerReason.LOW_VOL
        assert decision.vol_bps == 0
        assert decision.spread_bps_max == 0
        assert decision.window_size == 0

    def test_decide_single_event_returns_tighten(self) -> None:
        """Single event means no volatility → TIGHTEN."""
        controller = AdaptiveController()
        controller.record(1000, "BTCUSDT", Decimal("50000"), 1.0)

        decision = controller.decide("BTCUSDT")
        assert decision.mode == ControllerMode.TIGHTEN
        assert decision.reason == ControllerReason.LOW_VOL
        assert decision.vol_bps == 0  # Can't compute with 1 event

    def test_base_mode_medium_volatility(self) -> None:
        """Medium volatility (50-300 bps) triggers BASE mode."""
        controller = AdaptiveController()

        # Create ~100 bps volatility (5 steps of ~20 bps each)
        prices = [
            Decimal("100"),
            Decimal("100.20"),
            Decimal("100.40"),
            Decimal("100.60"),
            Decimal("100.80"),
        ]
        for i, price in enumerate(prices):
            controller.record(i * 1000, "TEST", price, 1.0)

        decision = controller.decide("TEST")
        assert decision.mode == ControllerMode.BASE
        assert decision.reason == ControllerReason.NORMAL
        assert 50 <= decision.vol_bps <= 300
        assert decision.spacing_multiplier == 1.0

    def test_widen_mode_high_volatility(self) -> None:
        """High volatility (> 300 bps) triggers WIDEN mode."""
        controller = AdaptiveController()

        # Create ~320 bps volatility (4 steps of ~80 bps each)
        prices = [
            Decimal("100"),
            Decimal("100.80"),
            Decimal("101.60"),
            Decimal("102.40"),
            Decimal("103.20"),
        ]
        for i, price in enumerate(prices):
            controller.record(i * 1000, "TEST", price, 1.0)

        decision = controller.decide("TEST")
        assert decision.mode == ControllerMode.WIDEN
        assert decision.reason == ControllerReason.HIGH_VOL
        assert decision.vol_bps > 300
        assert decision.spacing_multiplier == 1.5

    def test_tighten_mode_low_volatility(self) -> None:
        """Low volatility (< 50 bps) triggers TIGHTEN mode."""
        controller = AdaptiveController()

        # Create ~5 bps volatility (very small moves)
        prices = [
            Decimal("100"),
            Decimal("100.01"),
            Decimal("100.02"),
            Decimal("100.03"),
            Decimal("100.04"),
        ]
        for i, price in enumerate(prices):
            controller.record(i * 1000, "TEST", price, 1.0)

        decision = controller.decide("TEST")
        assert decision.mode == ControllerMode.TIGHTEN
        assert decision.reason == ControllerReason.LOW_VOL
        assert decision.vol_bps < 50
        assert decision.spacing_multiplier == 0.8

    def test_pause_mode_wide_spread(self) -> None:
        """Wide spread (> 50 bps) triggers PAUSE mode."""
        controller = AdaptiveController()

        # Record with wide spread
        controller.record(1000, "TEST", Decimal("100"), 60.0)  # 60 bps spread
        controller.record(2000, "TEST", Decimal("100.10"), 60.0)

        decision = controller.decide("TEST")
        assert decision.mode == ControllerMode.PAUSE
        assert decision.reason == ControllerReason.WIDE_SPREAD
        assert decision.spread_bps_max == 60

    def test_pause_priority_over_widen(self) -> None:
        """PAUSE has priority over WIDEN (even with high volatility)."""
        controller = AdaptiveController()

        # High volatility AND wide spread
        prices = [Decimal("100"), Decimal("101"), Decimal("102"), Decimal("103")]
        for i, price in enumerate(prices):
            controller.record(i * 1000, "TEST", price, 60.0)  # 60 bps spread

        decision = controller.decide("TEST")
        assert decision.mode == ControllerMode.PAUSE
        assert decision.reason == ControllerReason.WIDE_SPREAD

    def test_custom_thresholds(self) -> None:
        """Custom thresholds override defaults."""
        controller = AdaptiveController(
            vol_widen_bps=200,  # Lower threshold
            vol_tighten_bps=50,  # Lower threshold
            spread_pause_bps=100,  # Higher threshold
            widen_multiplier=2.0,
            tighten_multiplier=0.5,
        )

        # Create 250 bps volatility (should be WIDEN with custom threshold 200)
        prices = [Decimal("100"), Decimal("101.00"), Decimal("102.00"), Decimal("103.00")]
        for i, price in enumerate(prices):
            controller.record(i * 1000, "TEST", price, 5.0)

        decision = controller.decide("TEST")
        # 3 steps x ~100 bps each = ~300 bps, > 200 threshold
        assert decision.mode == ControllerMode.WIDEN
        assert decision.spacing_multiplier == 2.0

    def test_window_size_limits_history(self) -> None:
        """Window size limits the number of events stored per symbol."""
        controller = AdaptiveController(window_size=3)

        # Record more events than window allows
        for i in range(10):
            controller.record(i * 1000, "A", Decimal(str(100 + i * 0.1)), 1.0)

        # Only last 3 events should be kept
        assert len(controller._history["A"]) == 3

    def test_reset_clears_state(self) -> None:
        """Reset clears all recorded history."""
        controller = AdaptiveController()
        controller.record(1000, "A", Decimal("100"), 1.0)
        controller.record(2000, "A", Decimal("110"), 1.0)
        controller.record(1000, "B", Decimal("200"), 1.0)

        controller.reset()

        assert controller.get_all_symbols() == []
        assert controller._history == {}

    def test_get_all_symbols(self) -> None:
        """get_all_symbols returns all recorded symbols."""
        controller = AdaptiveController()
        controller.record(1000, "A", Decimal("100"), 1.0)
        controller.record(1000, "B", Decimal("200"), 1.0)
        controller.record(1000, "C", Decimal("300"), 1.0)

        symbols = controller.get_all_symbols()
        assert set(symbols) == {"A", "B", "C"}

    def test_deterministic_decisions(self) -> None:
        """Decisions are deterministic across multiple runs."""
        decisions = []
        for _ in range(5):
            controller = AdaptiveController()
            prices = [
                Decimal("100"),
                Decimal("100.50"),
                Decimal("101.00"),
                Decimal("101.50"),
                Decimal("102.00"),
            ]
            for i, price in enumerate(prices):
                controller.record(i * 1000, "TEST", price, 5.0)
            decisions.append(controller.decide("TEST").to_dict())

        # All runs should produce identical results
        assert all(d == decisions[0] for d in decisions)

    def test_integer_bps_determinism(self) -> None:
        """vol_bps and spread_bps_max are integers (no float precision issues)."""
        controller = AdaptiveController()

        # Use prices that could cause float precision issues
        controller.record(1000, "TEST", Decimal("100.123456789"), 10.5)
        controller.record(2000, "TEST", Decimal("100.223456789"), 10.7)
        controller.record(3000, "TEST", Decimal("100.323456789"), 10.3)

        decision = controller.decide("TEST")
        assert isinstance(decision.vol_bps, int)
        assert isinstance(decision.spread_bps_max, int)


class TestControllerDecision:
    """Tests for ControllerDecision dataclass."""

    def test_to_dict(self) -> None:
        """to_dict returns correct structure."""
        decision = ControllerDecision(
            mode=ControllerMode.WIDEN,
            reason=ControllerReason.HIGH_VOL,
            spacing_multiplier=1.5,
            vol_bps=350,
            spread_bps_max=10,
            window_size=5,
        )
        d = decision.to_dict()
        assert d == {
            "mode": "WIDEN",
            "reason": "HIGH_VOL",
            "spacing_multiplier": 1.5,
            "vol_bps": 350,
            "spread_bps_max": 10,
            "window_size": 5,
        }

    def test_from_dict(self) -> None:
        """from_dict recreates decision correctly."""
        original = ControllerDecision(
            mode=ControllerMode.PAUSE,
            reason=ControllerReason.WIDE_SPREAD,
            spacing_multiplier=1.0,
            vol_bps=100,
            spread_bps_max=60,
            window_size=3,
        )
        d = original.to_dict()
        recreated = ControllerDecision.from_dict(d)

        assert recreated.mode == original.mode
        assert recreated.reason == original.reason
        assert recreated.spacing_multiplier == original.spacing_multiplier
        assert recreated.vol_bps == original.vol_bps
        assert recreated.spread_bps_max == original.spread_bps_max
        assert recreated.window_size == original.window_size


class TestControllerIntegration:
    """Integration tests for Controller with paper trading."""

    def test_controller_fixture_triggers_modes(self) -> None:
        """sample_day_controller fixture triggers expected modes."""
        engine = PaperEngine(controller_enabled=True)
        result = engine.run(Path("tests/fixtures/sample_day_controller"))

        assert result.controller_enabled is True
        assert len(result.controller_decisions) == 3

        # Build decision map
        decisions = {d["symbol"]: d for d in result.controller_decisions}

        # WIDENUSDT should be WIDEN (high volatility)
        assert decisions["WIDENUSDT"]["mode"] == "WIDEN"
        assert decisions["WIDENUSDT"]["reason"] == "HIGH_VOL"
        assert decisions["WIDENUSDT"]["vol_bps"] > 300

        # TIGHTENUSDT should be TIGHTEN (low volatility)
        assert decisions["TIGHTENUSDT"]["mode"] == "TIGHTEN"
        assert decisions["TIGHTENUSDT"]["reason"] == "LOW_VOL"
        assert decisions["TIGHTENUSDT"]["vol_bps"] < 50

        # BASEUSDT should be BASE (medium volatility)
        assert decisions["BASEUSDT"]["mode"] == "BASE"
        assert decisions["BASEUSDT"]["reason"] == "NORMAL"
        assert 50 <= decisions["BASEUSDT"]["vol_bps"] <= 300

    def test_controller_disabled_no_decisions(self) -> None:
        """With controller disabled, no decisions are recorded."""
        engine = PaperEngine(controller_enabled=False)  # Default
        result = engine.run(Path("tests/fixtures/sample_day_controller"))

        assert result.controller_enabled is False
        assert result.controller_decisions == []

    def test_existing_digests_preserved_with_controller_disabled(self) -> None:
        """Existing canonical digests are preserved when controller is disabled.

        Note: Digests updated in PR-ASM-P0-01 for crossing/touch fill model (v1.1).
        """
        expected = {
            "sample_day": "66b29a4e92192f8f",  # blocked by gating, 0 fills
            "sample_day_allowed": "3ecf49cd03db1b07",  # v1.1 crossing/touch fills
            "sample_day_toxic": "a31ead72fc1f197e",  # v1.1 crossing/touch fills
            "sample_day_multisymbol": "22acba5cb8b81ab4",  # v1.1 crossing/touch fills
        }

        for fixture, expected_digest in expected.items():
            engine = PaperEngine(controller_enabled=False)
            result = engine.run(Path(f"tests/fixtures/{fixture}"))
            assert result.digest == expected_digest, (
                f"Digest mismatch for {fixture}: expected {expected_digest}, got {result.digest}"
            )

    def test_controller_fixture_digest(self) -> None:
        """sample_day_controller has expected digest."""
        engine = PaperEngine(controller_enabled=True)
        result = engine.run(Path("tests/fixtures/sample_day_controller"))

        assert result.digest == "f3a0a321c39cc411"

    def test_controller_deterministic_across_runs(self) -> None:
        """Controller produces deterministic results across runs."""
        digests = []
        for _ in range(3):
            engine = PaperEngine(controller_enabled=True)
            result = engine.run(Path("tests/fixtures/sample_day_controller"))
            digests.append(result.digest)

        assert all(d == digests[0] for d in digests)
        assert digests[0] == "f3a0a321c39cc411"
