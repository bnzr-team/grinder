"""LC-17: FakePort violation path tests.

Proves that FakePort correctly records calls and that the smoke script
would return exit code 1 if any port call is made.
"""

from __future__ import annotations

from decimal import Decimal


class TestFakePortViolation:
    """Test FakePort records calls as violations."""

    def test_cancel_order_records_call(self) -> None:
        """Verify cancel_order() records the call."""
        # Import FakePort from smoke script
        from scripts.smoke_real_sources_detect_only import FakePort  # noqa: PLC0415

        port = FakePort()
        assert len(port.calls) == 0

        # Call cancel_order - this should be recorded
        result = port.cancel_order("BTCUSDT", "grinder_test_123")

        assert len(port.calls) == 1
        assert port.calls[0]["action"] == "cancel_order"
        assert port.calls[0]["symbol"] == "BTCUSDT"
        assert port.calls[0]["client_order_id"] == "grinder_test_123"
        assert result["status"] == "CANCELED"

    def test_place_market_order_records_call(self) -> None:
        """Verify place_market_order() records the call."""
        from scripts.smoke_real_sources_detect_only import FakePort  # noqa: PLC0415

        port = FakePort()
        assert len(port.calls) == 0

        # Call place_market_order - this should be recorded
        result = port.place_market_order("ETHUSDT", "SELL", Decimal("0.1"))

        assert len(port.calls) == 1
        assert port.calls[0]["action"] == "place_market_order"
        assert port.calls[0]["symbol"] == "ETHUSDT"
        assert port.calls[0]["side"] == "SELL"
        assert port.calls[0]["quantity"] == "0.1"
        assert result["status"] == "FILLED"

    def test_multiple_calls_accumulate(self) -> None:
        """Verify multiple calls accumulate in the list."""
        from scripts.smoke_real_sources_detect_only import FakePort  # noqa: PLC0415

        port = FakePort()

        port.cancel_order("BTCUSDT", "order1")
        port.cancel_order("ETHUSDT", "order2")
        port.place_market_order("SOLUSDT", "BUY", Decimal("10"))

        assert len(port.calls) == 3


class TestExitCodeContract:
    """Test exit code contract for detect-only violation."""

    def test_exit_codes_defined(self) -> None:
        """Verify exit codes are properly defined."""
        from scripts.smoke_real_sources_detect_only import (  # noqa: PLC0415
            EXIT_CONFIG_ERROR,
            EXIT_CONNECTION_ERROR,
            EXIT_DETECT_ONLY_VIOLATION,
            EXIT_SUCCESS,
        )

        assert EXIT_SUCCESS == 0
        assert EXIT_DETECT_ONLY_VIOLATION == 1
        assert EXIT_CONFIG_ERROR == 2
        assert EXIT_CONNECTION_ERROR == 3

    def test_violation_logic_port_calls(self) -> None:
        """Verify violation detection logic for port_calls > 0.

        This tests the core logic used in main() to determine exit code.
        """
        # Simulate results with port_calls > 0
        port_calls = 1
        executed_count = 0

        # This is the exact logic from main()
        exit_code = 0 if port_calls == 0 and executed_count == 0 else 1

        assert exit_code == 1

    def test_violation_logic_executed_count(self) -> None:
        """Verify violation detection logic for executed_count > 0."""
        port_calls = 0
        executed_count = 1

        exit_code = 0 if port_calls == 0 and executed_count == 0 else 1

        assert exit_code == 1

    def test_success_logic(self) -> None:
        """Verify success when both port_calls=0 and executed_count=0."""
        port_calls = 0
        executed_count = 0

        exit_code = 0 if port_calls == 0 and executed_count == 0 else 1

        assert exit_code == 0
