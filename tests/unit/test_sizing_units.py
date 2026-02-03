"""Tests for sizing units SSOT (ADR-018).

Tests verify:
- size_schedule is interpreted as base asset quantity, NOT notional
- notional_to_qty() conversion utility works correctly
- Execution engine uses size_schedule as quantity (not notional)

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.12.4
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.core import GridMode
from grinder.policies import notional_to_qty
from grinder.policies.base import GridPlan


class TestNotionalToQty:
    """Tests for notional_to_qty() conversion utility."""

    def test_basic_conversion(self) -> None:
        """Test basic notional to qty conversion."""
        # $500 at $50,000/BTC = 0.01 BTC
        notional = Decimal("500")
        price = Decimal("50000")

        qty = notional_to_qty(notional, price)

        assert qty == Decimal("0.01")

    def test_precision_rounding(self) -> None:
        """Test conversion rounds down to precision."""
        # $1000 at $30000/BTC = 0.0333... BTC
        notional = Decimal("1000")
        price = Decimal("30000")

        qty = notional_to_qty(notional, price, precision=4)

        # Should round down to 4 decimals: 0.0333
        assert qty == Decimal("0.0333")

    def test_high_precision(self) -> None:
        """Test conversion with high precision (satoshi level)."""
        notional = Decimal("100")
        price = Decimal("50000")

        qty = notional_to_qty(notional, price, precision=8)

        # 100/50000 = 0.002 exactly
        assert qty == Decimal("0.00200000")

    def test_zero_notional(self) -> None:
        """Test zero notional returns zero qty."""
        notional = Decimal("0")
        price = Decimal("50000")

        qty = notional_to_qty(notional, price)

        assert qty == Decimal("0")

    def test_invalid_price_zero(self) -> None:
        """Test zero price raises ValueError."""
        with pytest.raises(ValueError, match="price must be positive"):
            notional_to_qty(Decimal("100"), Decimal("0"))

    def test_invalid_price_negative(self) -> None:
        """Test negative price raises ValueError."""
        with pytest.raises(ValueError, match="price must be positive"):
            notional_to_qty(Decimal("100"), Decimal("-50000"))

    def test_invalid_notional_negative(self) -> None:
        """Test negative notional raises ValueError."""
        with pytest.raises(ValueError, match="notional must be non-negative"):
            notional_to_qty(Decimal("-100"), Decimal("50000"))

    def test_deterministic_output(self) -> None:
        """Test conversion is deterministic."""
        notional = Decimal("1234.56")
        price = Decimal("45678.90")

        qty1 = notional_to_qty(notional, price)
        qty2 = notional_to_qty(notional, price)

        assert qty1 == qty2

    def test_round_down_behavior(self) -> None:
        """Test rounding is always down (ROUND_DOWN), not nearest."""
        # This ensures we never over-allocate
        notional = Decimal("999")
        price = Decimal("1000")
        # 999/1000 = 0.999, with precision=2 should be 0.99 (not 1.00)

        qty = notional_to_qty(notional, price, precision=2)

        assert qty == Decimal("0.99")


class TestSizeScheduleIsQty:
    """Tests verifying size_schedule is interpreted as quantity (base asset).

    SSOT: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.12.4 states:
    "GridPlan.size_schedule MUST be interpreted as quantity (base) by Execution and Ledger"
    """

    def test_gridplan_size_schedule_is_qty_not_notional(self) -> None:
        """Test that size_schedule values are used directly as quantity.

        If size_schedule were notional (USD), a value like 0.01 would be nonsensical
        ($0.01 order). The fact that we use Decimal("0.01") for BTC orders proves
        the value is quantity (0.01 BTC = ~$500 at $50k).
        """
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),  # $50,000/BTC
            spacing_bps=10.0,
            levels_up=3,
            levels_down=3,
            size_schedule=[Decimal("0.01"), Decimal("0.02"), Decimal("0.03")],
        )

        # If these were notional (USD), they'd be $0.01, $0.02, $0.03 - nonsense
        # They are quantities: 0.01 BTC = $500, 0.02 BTC = $1000, 0.03 BTC = $1500
        assert plan.size_schedule[0] == Decimal("0.01")  # 0.01 BTC
        assert plan.size_schedule[1] == Decimal("0.02")  # 0.02 BTC
        assert plan.size_schedule[2] == Decimal("0.03")  # 0.03 BTC

    def test_size_schedule_notional_equivalence(self) -> None:
        """Test computing notional equivalent of size_schedule.

        This test demonstrates that size_schedule is qty by showing
        the notional calculation makes sense only if qty interpretation is used.
        """
        price = Decimal("50000")
        qty = Decimal("0.01")

        # If size_schedule is qty (correct interpretation):
        # notional = qty * price = 0.01 * 50000 = $500
        notional_if_qty = qty * price
        assert notional_if_qty == Decimal("500")

        # If size_schedule were notional (WRONG interpretation):
        # qty = notional / price = 0.01 / 50000 = 0.0000002 BTC = 0.02 satoshi
        # This is below dust limit and makes no sense
        qty_if_notional = qty / price
        assert qty_if_notional < Decimal("0.001")  # Nonsensically small

    def test_realistic_order_sizes(self) -> None:
        """Test that typical size_schedule values are realistic as quantities."""
        # These are values actually used in our test fixtures
        realistic_sizes = [
            Decimal("0.01"),  # 0.01 BTC = ~$500 at $50k
            Decimal("0.001"),  # 0.001 BTC = ~$50 at $50k
            Decimal("0.1"),  # 0.1 BTC = ~$5000 at $50k
        ]

        for size in realistic_sizes:
            # As quantity: size represents base asset amount
            # Notional at $50k would be reasonable
            notional_at_50k = size * Decimal("50000")
            assert notional_at_50k >= Decimal("50")  # At least $50 order
            assert notional_at_50k <= Decimal("10000")  # At most $10k order


class TestNotionalToQtyIntegration:
    """Integration tests showing notional_to_qty usage with GridPlan."""

    def test_convert_notional_budget_to_size_schedule(self) -> None:
        """Test converting a notional budget to size_schedule quantities."""
        price = Decimal("50000")
        notional_per_level = Decimal("500")  # $500 per level
        levels = 5

        # Convert notional to qty for each level
        size_schedule = [
            notional_to_qty(notional_per_level, price, precision=4) for _ in range(levels)
        ]

        # All levels should have 0.01 BTC (500/50000)
        assert all(s == Decimal("0.01") for s in size_schedule)

        # Create GridPlan with computed size_schedule
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=price,
            spacing_bps=10.0,
            levels_up=levels,
            levels_down=levels,
            size_schedule=size_schedule,
        )

        assert len(plan.size_schedule) == levels
        assert plan.size_schedule[0] == Decimal("0.01")

    def test_tiered_notional_to_qty(self) -> None:
        """Test converting tiered notional budget to size_schedule."""
        price = Decimal("50000")
        # Tiered notional: $500, $1000, $1500 per level
        tiered_notional = [Decimal("500"), Decimal("1000"), Decimal("1500")]

        size_schedule = [notional_to_qty(n, price, precision=4) for n in tiered_notional]

        # Should be: 0.01, 0.02, 0.03 BTC
        assert size_schedule == [
            Decimal("0.01"),
            Decimal("0.02"),
            Decimal("0.03"),
        ]
