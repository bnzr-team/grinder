"""Unit tests for enablement ceremony smoke script contract (LC-15a).

Tests verify:
1. Each stage returns valid StageResult
2. Zero port calls in all stages (detect-only, plan-only, blocked)
3. FakePort tracks calls correctly
"""
# ruff: noqa: PLC0415

from __future__ import annotations

from decimal import Decimal


class TestFakePort:
    """Tests for FakePort execution tracking."""

    def test_cancel_order_records_call(self) -> None:
        """Cancel order records call with correct fields."""
        from scripts.smoke_enablement_ceremony import FakePort

        port = FakePort()
        result = port.cancel_order("BTCUSDT", "grinder_test_001")

        assert len(port.calls) == 1
        call = port.calls[0]
        assert call["action"] == "cancel_order"
        assert call["symbol"] == "BTCUSDT"
        assert call["client_order_id"] == "grinder_test_001"
        assert "ts" in call
        assert result["status"] == "CANCELED"

    def test_place_market_order_records_call(self) -> None:
        """Market order records call with correct fields."""
        from scripts.smoke_enablement_ceremony import FakePort

        port = FakePort()
        result = port.place_market_order("BTCUSDT", "BUY", Decimal("0.001"))

        assert len(port.calls) == 1
        call = port.calls[0]
        assert call["action"] == "place_market_order"
        assert call["symbol"] == "BTCUSDT"
        assert call["side"] == "BUY"
        assert call["quantity"] == "0.001"
        assert "ts" in call
        assert result["status"] == "FILLED"

    def test_multiple_calls_tracked(self) -> None:
        """Multiple calls are all tracked."""
        from scripts.smoke_enablement_ceremony import FakePort

        port = FakePort()
        port.cancel_order("BTCUSDT", "order_1")
        port.cancel_order("ETHUSDT", "order_2")
        port.place_market_order("BTCUSDT", "SELL", Decimal("0.01"))

        assert len(port.calls) == 3


class TestStageResult:
    """Tests for StageResult dataclass."""

    def test_stage_result_fields(self) -> None:
        """StageResult has all required fields."""
        from scripts.smoke_enablement_ceremony import StageResult

        result = StageResult(
            name="Test Stage",
            passed=True,
            port_calls=0,
            planned_count=5,
            executed_count=0,
            blocked_count=2,
        )

        assert result.name == "Test Stage"
        assert result.passed is True
        assert result.port_calls == 0
        assert result.planned_count == 5
        assert result.executed_count == 0
        assert result.blocked_count == 2
        assert result.blocked_reasons == []
        assert result.error is None

    def test_stage_result_with_error(self) -> None:
        """StageResult records error message."""
        from scripts.smoke_enablement_ceremony import StageResult

        result = StageResult(
            name="Failed Stage",
            passed=False,
            port_calls=0,
            error="Test error message",
        )

        assert result.passed is False
        assert result.error == "Test error message"


class TestStageADetectOnly:
    """Tests for Stage A: Detect-only mode."""

    def test_stage_a_zero_port_calls(self) -> None:
        """Stage A makes zero port calls."""
        from scripts.smoke_enablement_ceremony import FakePort, run_stage_a_detect_only

        port = FakePort()
        result = run_stage_a_detect_only(port, inject_mismatch=False)

        assert result.port_calls == 0
        assert len(port.calls) == 0
        assert result.passed is True

    def test_stage_a_with_mismatch_still_zero_calls(self) -> None:
        """Stage A with injected mismatch still makes zero port calls."""
        from scripts.smoke_enablement_ceremony import FakePort, run_stage_a_detect_only

        port = FakePort()
        result = run_stage_a_detect_only(port, inject_mismatch=True)

        assert result.port_calls == 0
        assert len(port.calls) == 0
        assert result.passed is True

    def test_stage_a_returns_valid_result(self) -> None:
        """Stage A returns valid StageResult."""
        from scripts.smoke_enablement_ceremony import FakePort, run_stage_a_detect_only

        port = FakePort()
        result = run_stage_a_detect_only(port)

        assert result.name == "A: Detect-only"
        assert result.executed_count == 0


class TestStageBPlanOnly:
    """Tests for Stage B: Plan-only mode."""

    def test_stage_b_zero_port_calls(self) -> None:
        """Stage B makes zero port calls."""
        from scripts.smoke_enablement_ceremony import FakePort, run_stage_b_plan_only

        port = FakePort()
        result = run_stage_b_plan_only(port, inject_mismatch=False)

        assert result.port_calls == 0
        assert len(port.calls) == 0
        assert result.passed is True

    def test_stage_b_with_mismatch_still_zero_calls(self) -> None:
        """Stage B with injected mismatch still makes zero port calls."""
        from scripts.smoke_enablement_ceremony import FakePort, run_stage_b_plan_only

        port = FakePort()
        result = run_stage_b_plan_only(port, inject_mismatch=True)

        assert result.port_calls == 0
        assert len(port.calls) == 0
        assert result.passed is True

    def test_stage_b_returns_valid_result(self) -> None:
        """Stage B returns valid StageResult."""
        from scripts.smoke_enablement_ceremony import FakePort, run_stage_b_plan_only

        port = FakePort()
        result = run_stage_b_plan_only(port)

        assert result.name == "B: Plan-only"
        assert result.executed_count == 0


class TestStageCBlocked:
    """Tests for Stage C: Blocked mode."""

    def test_stage_c_zero_port_calls(self) -> None:
        """Stage C makes zero port calls (blocked by armed=False)."""
        from scripts.smoke_enablement_ceremony import FakePort, run_stage_c_blocked

        port = FakePort()
        result = run_stage_c_blocked(port, inject_mismatch=False)

        assert result.port_calls == 0
        assert len(port.calls) == 0
        assert result.passed is True

    def test_stage_c_with_mismatch_still_zero_calls(self) -> None:
        """Stage C with injected mismatch still makes zero port calls."""
        from scripts.smoke_enablement_ceremony import FakePort, run_stage_c_blocked

        port = FakePort()
        result = run_stage_c_blocked(port, inject_mismatch=True)

        assert result.port_calls == 0
        assert len(port.calls) == 0
        assert result.passed is True

    def test_stage_c_returns_valid_result(self) -> None:
        """Stage C returns valid StageResult."""
        from scripts.smoke_enablement_ceremony import FakePort, run_stage_c_blocked

        port = FakePort()
        result = run_stage_c_blocked(port)

        assert result.name == "C: Blocked"
        assert result.executed_count == 0


class TestCeremonyContract:
    """Integration tests for ceremony contract."""

    def test_all_stages_pass_default_mode(self) -> None:
        """All stages pass in default mode (no mismatch)."""
        from scripts.smoke_enablement_ceremony import (
            FakePort,
            run_stage_a_detect_only,
            run_stage_b_plan_only,
            run_stage_c_blocked,
        )

        port = FakePort()

        result_a = run_stage_a_detect_only(port, inject_mismatch=False)
        result_b = run_stage_b_plan_only(port, inject_mismatch=False)
        result_c = run_stage_c_blocked(port, inject_mismatch=False)

        assert result_a.passed is True
        assert result_b.passed is True
        assert result_c.passed is True
        assert len(port.calls) == 0

    def test_all_stages_pass_with_mismatch(self) -> None:
        """All stages pass even with injected mismatch (zero execution)."""
        from scripts.smoke_enablement_ceremony import (
            FakePort,
            run_stage_a_detect_only,
            run_stage_b_plan_only,
            run_stage_c_blocked,
        )

        port = FakePort()

        result_a = run_stage_a_detect_only(port, inject_mismatch=True)
        result_b = run_stage_b_plan_only(port, inject_mismatch=True)
        result_c = run_stage_c_blocked(port, inject_mismatch=True)

        assert result_a.passed is True
        assert result_b.passed is True
        assert result_c.passed is True
        # The key contract: zero port calls in ALL modes
        assert len(port.calls) == 0

    def test_total_port_calls_zero_across_all_stages(self) -> None:
        """Total port calls remain zero across all stages."""
        from scripts.smoke_enablement_ceremony import (
            FakePort,
            run_stage_a_detect_only,
            run_stage_b_plan_only,
            run_stage_c_blocked,
        )

        port = FakePort()

        run_stage_a_detect_only(port, inject_mismatch=True)
        run_stage_b_plan_only(port, inject_mismatch=True)
        run_stage_c_blocked(port, inject_mismatch=True)

        # This is the primary contract assertion
        assert len(port.calls) == 0, "Ceremony stages must never execute port calls"
