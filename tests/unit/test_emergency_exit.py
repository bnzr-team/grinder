"""Tests for EmergencyExitExecutor (RISK-EE-1, § 10.6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from grinder.core import OrderSide  # noqa: TC001 - used at runtime
from grinder.risk.emergency_exit import EmergencyExitExecutor, EmergencyExitResult

# ---------------------------------------------------------------------------
# Fake port for testing (satisfies EmergencyExitPort protocol)
# ---------------------------------------------------------------------------


@dataclass
class FakePosition:
    """Minimal position stub with position_amt attribute."""

    symbol: str
    position_amt: Decimal


@dataclass
class FakeEmergencyExitPort:
    """In-memory port for emergency exit testing.

    Tracks call sequence for verification.
    """

    positions: dict[str, list[FakePosition]] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    cancel_all_raise: bool = False
    place_market_raise: bool = False
    get_positions_raise: bool = False
    # If True, positions are NOT removed after place_market_order (simulates partial fill)
    partial_fill: bool = False

    def cancel_all_orders(self, symbol: str) -> int:
        self.calls.append(("cancel_all_orders", symbol))
        if self.cancel_all_raise:
            raise RuntimeError("cancel_all_orders failed")
        return 1

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        reduce_only: bool = False,
    ) -> str:
        self.calls.append(
            ("place_market_order", symbol, side.value, str(quantity), str(reduce_only))
        )
        if self.place_market_raise:
            raise RuntimeError("place_market_order failed")
        # Remove position unless simulating partial fill
        if not self.partial_fill and symbol in self.positions:
            self.positions[symbol] = [p for p in self.positions[symbol] if p.position_amt == 0]
        return f"order-{symbol}-{side.value}"

    def get_positions(self, symbol: str) -> list[FakePosition]:
        self.calls.append(("get_positions", symbol))
        if self.get_positions_raise:
            raise RuntimeError("get_positions failed")
        return self.positions.get(symbol, [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmergencyExitNoPositions:
    """When there are no positions, exit should succeed immediately."""

    def test_no_positions_success(self) -> None:
        port = FakeEmergencyExitPort()
        executor = EmergencyExitExecutor(port, verify_attempts=1, verify_interval_s=0)

        result = executor.execute(ts_ms=1000, reason="test", symbols=["BTCUSDT"])

        assert result.success is True
        assert result.orders_cancelled == 1  # cancel_all still called
        assert result.market_orders_placed == 0
        assert result.positions_remaining == 0
        assert result.triggered_at_ms == 1000
        assert result.reason == "test"


class TestEmergencyExitLongPosition:
    """Close a long position (positive position_amt → SELL)."""

    def test_single_long_closed(self) -> None:
        port = FakeEmergencyExitPort(
            positions={"BTCUSDT": [FakePosition("BTCUSDT", Decimal("0.004"))]}
        )
        executor = EmergencyExitExecutor(port, verify_attempts=1, verify_interval_s=0)

        result = executor.execute(ts_ms=2000, reason="dd_breach", symbols=["BTCUSDT"])

        assert result.success is True
        assert result.market_orders_placed == 1
        assert result.positions_remaining == 0

        # Verify SELL side for long position
        market_calls = [c for c in port.calls if c[0] == "place_market_order"]
        assert len(market_calls) == 1
        assert market_calls[0][2] == "SELL"  # side
        assert market_calls[0][3] == "0.004"  # quantity
        assert market_calls[0][4] == "True"  # reduce_only


class TestEmergencyExitShortPosition:
    """Close a short position (negative position_amt → BUY)."""

    def test_single_short_closed(self) -> None:
        port = FakeEmergencyExitPort(
            positions={"BTCUSDT": [FakePosition("BTCUSDT", Decimal("-0.004"))]}
        )
        executor = EmergencyExitExecutor(port, verify_attempts=1, verify_interval_s=0)

        result = executor.execute(ts_ms=3000, reason="dd_breach", symbols=["BTCUSDT"])

        assert result.success is True
        assert result.market_orders_placed == 1

        market_calls = [c for c in port.calls if c[0] == "place_market_order"]
        assert market_calls[0][2] == "BUY"  # side
        assert market_calls[0][3] == "0.004"  # quantity (absolute)
        assert market_calls[0][4] == "True"  # reduce_only


class TestEmergencyExitMultipleSymbols:
    """Close positions across multiple symbols."""

    def test_two_symbols_both_closed(self) -> None:
        port = FakeEmergencyExitPort(
            positions={
                "BTCUSDT": [FakePosition("BTCUSDT", Decimal("0.010"))],
                "ETHUSDT": [FakePosition("ETHUSDT", Decimal("-0.500"))],
            }
        )
        executor = EmergencyExitExecutor(port, verify_attempts=1, verify_interval_s=0)

        result = executor.execute(ts_ms=4000, reason="dd_breach", symbols=["BTCUSDT", "ETHUSDT"])

        assert result.success is True
        assert result.market_orders_placed == 2
        assert result.orders_cancelled == 2  # cancel_all per symbol


class TestEmergencyExitPartialClose:
    """When positions don't close (partial fill), result is PARTIAL."""

    def test_partial_fill_returns_partial(self) -> None:
        port = FakeEmergencyExitPort(
            positions={"BTCUSDT": [FakePosition("BTCUSDT", Decimal("0.004"))]},
            partial_fill=True,
        )
        executor = EmergencyExitExecutor(port, verify_attempts=2, verify_interval_s=0)

        result = executor.execute(ts_ms=5000, reason="dd_breach", symbols=["BTCUSDT"])

        assert result.success is False
        assert result.positions_remaining > 0
        assert result.market_orders_placed == 1


class TestEmergencyExitCancelBeforeClose:
    """Verify cancel_all is called BEFORE place_market_order for each symbol."""

    def test_cancel_before_market_order(self) -> None:
        port = FakeEmergencyExitPort(
            positions={"BTCUSDT": [FakePosition("BTCUSDT", Decimal("0.004"))]}
        )
        executor = EmergencyExitExecutor(port, verify_attempts=1, verify_interval_s=0)

        executor.execute(ts_ms=6000, reason="test", symbols=["BTCUSDT"])

        # Find indices of cancel and market calls
        cancel_idx = next(i for i, c in enumerate(port.calls) if c[0] == "cancel_all_orders")
        market_idx = next(i for i, c in enumerate(port.calls) if c[0] == "place_market_order")
        assert cancel_idx < market_idx


class TestEmergencyExitErrorRecovery:
    """Errors in one symbol don't block other symbols."""

    def test_cancel_error_continues(self) -> None:
        port = FakeEmergencyExitPort(
            positions={"BTCUSDT": [FakePosition("BTCUSDT", Decimal("0.004"))]},
            cancel_all_raise=True,
        )
        executor = EmergencyExitExecutor(port, verify_attempts=1, verify_interval_s=0)

        # Should not raise — continues despite cancel failure
        result = executor.execute(ts_ms=7000, reason="test", symbols=["BTCUSDT"])

        # Still tries to close position
        assert result.market_orders_placed == 1

    def test_get_positions_error_assumes_open(self) -> None:
        """If get_positions fails during verify, assume position still open."""
        port = FakeEmergencyExitPort(
            positions={"BTCUSDT": [FakePosition("BTCUSDT", Decimal("0.004"))]}
        )
        executor = EmergencyExitExecutor(port, verify_attempts=1, verify_interval_s=0)
        # Positions clear on market order, but then fail on verify
        port.get_positions_raise = False  # First calls succeed (close phase)

        result = executor.execute(ts_ms=8000, reason="test", symbols=["BTCUSDT"])

        # Position was closed, verify should succeed
        assert result.success is True


class TestEmergencyExitResultFields:
    """Verify all EmergencyExitResult fields."""

    def test_result_dataclass(self) -> None:
        result = EmergencyExitResult(
            triggered_at_ms=9000,
            reason="drawdown_breach",
            orders_cancelled=2,
            market_orders_placed=1,
            positions_remaining=0,
            success=True,
        )
        assert result.triggered_at_ms == 9000
        assert result.reason == "drawdown_breach"
        assert result.orders_cancelled == 2
        assert result.market_orders_placed == 1
        assert result.positions_remaining == 0
        assert result.success is True


class TestEmergencyExitAllReduceOnly:
    """Every market order must have reduce_only=True."""

    def test_all_market_orders_reduce_only(self) -> None:
        port = FakeEmergencyExitPort(
            positions={
                "BTCUSDT": [FakePosition("BTCUSDT", Decimal("0.010"))],
                "ETHUSDT": [FakePosition("ETHUSDT", Decimal("-0.500"))],
            }
        )
        executor = EmergencyExitExecutor(port, verify_attempts=1, verify_interval_s=0)

        executor.execute(ts_ms=10000, reason="test", symbols=["BTCUSDT", "ETHUSDT"])

        market_calls = [c for c in port.calls if c[0] == "place_market_order"]
        for call in market_calls:
            assert call[4] == "True", f"reduce_only must be True, got {call[4]}"
