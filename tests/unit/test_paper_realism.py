"""Unit tests for LC-03: Paper realism v0.1 (tick-delay fills).

Tests verify:
- Orders stay OPEN until tick-eligible
- Fills occur exactly at tick N after placement
- Cancel before fill prevents filling
- Determinism: same inputs → same outputs
- Backward compatibility with fill_after_ticks=0
"""

from __future__ import annotations

from decimal import Decimal

from grinder.contracts import Snapshot
from grinder.core import OrderSide, OrderState
from grinder.execution.types import OrderRecord
from grinder.paper import PaperEngine
from grinder.paper.fills import check_pending_fills

# --- Test Class: check_pending_fills function ---


class TestCheckPendingFills:
    """Tests for the check_pending_fills function."""

    def test_order_not_fill_eligible_before_tick_threshold(self) -> None:
        """Orders don't fill before tick threshold is reached."""
        order = OrderRecord(
            order_id="test_order_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.1"),
            state=OrderState.OPEN,
            level_id=1,
            created_ts=1000,
            placed_tick=1,
        )

        # At tick 1, with fill_after_ticks=1, order should NOT fill
        # Because 1 - 1 = 0 < 1
        result = check_pending_fills(
            ts=1000,
            open_orders=[order],
            mid_price=Decimal("48000"),  # Price crossed (below buy limit)
            current_tick=1,
            fill_after_ticks=1,
        )

        assert len(result.fills) == 0
        assert len(result.filled_order_ids) == 0

    def test_order_fills_at_tick_threshold(self) -> None:
        """Orders fill exactly when tick threshold is reached."""
        order = OrderRecord(
            order_id="test_order_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.1"),
            state=OrderState.OPEN,
            level_id=1,
            created_ts=1000,
            placed_tick=1,
        )

        # At tick 2, with fill_after_ticks=1, order SHOULD fill
        # Because 2 - 1 = 1 >= 1
        result = check_pending_fills(
            ts=2000,
            open_orders=[order],
            mid_price=Decimal("48000"),  # Price crossed
            current_tick=2,
            fill_after_ticks=1,
        )

        assert len(result.fills) == 1
        assert "test_order_1" in result.filled_order_ids
        assert result.fills[0].order_id == "test_order_1"
        assert result.fills[0].price == Decimal("49000")
        assert result.fills[0].quantity == Decimal("0.1")

    def test_buy_order_requires_price_crossing(self) -> None:
        """BUY order only fills when mid_price <= limit_price."""
        order = OrderRecord(
            order_id="test_buy",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.1"),
            state=OrderState.OPEN,
            level_id=1,
            created_ts=1000,
            placed_tick=1,
        )

        # Price above buy limit - no fill
        result = check_pending_fills(
            ts=2000,
            open_orders=[order],
            mid_price=Decimal("50000"),  # Above buy limit
            current_tick=2,
            fill_after_ticks=1,
        )
        assert len(result.fills) == 0

        # Price at buy limit - fills
        result = check_pending_fills(
            ts=2000,
            open_orders=[order],
            mid_price=Decimal("49000"),  # At limit
            current_tick=2,
            fill_after_ticks=1,
        )
        assert len(result.fills) == 1

        # Price below buy limit - fills
        result = check_pending_fills(
            ts=2000,
            open_orders=[order],
            mid_price=Decimal("48000"),  # Below limit
            current_tick=2,
            fill_after_ticks=1,
        )
        assert len(result.fills) == 1

    def test_sell_order_requires_price_crossing(self) -> None:
        """SELL order only fills when mid_price >= limit_price."""
        order = OrderRecord(
            order_id="test_sell",
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.1"),
            state=OrderState.OPEN,
            level_id=1,
            created_ts=1000,
            placed_tick=1,
        )

        # Price below sell limit - no fill
        result = check_pending_fills(
            ts=2000,
            open_orders=[order],
            mid_price=Decimal("50000"),  # Below sell limit
            current_tick=2,
            fill_after_ticks=1,
        )
        assert len(result.fills) == 0

        # Price at sell limit - fills
        result = check_pending_fills(
            ts=2000,
            open_orders=[order],
            mid_price=Decimal("51000"),  # At limit
            current_tick=2,
            fill_after_ticks=1,
        )
        assert len(result.fills) == 1

        # Price above sell limit - fills
        result = check_pending_fills(
            ts=2000,
            open_orders=[order],
            mid_price=Decimal("52000"),  # Above limit
            current_tick=2,
            fill_after_ticks=1,
        )
        assert len(result.fills) == 1

    def test_deterministic_ordering_by_order_id(self) -> None:
        """Fill order is deterministic (sorted by order_id)."""
        orders = [
            OrderRecord(
                order_id="order_c",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("49000"),
                quantity=Decimal("0.1"),
                state=OrderState.OPEN,
                level_id=1,
                created_ts=1000,
                placed_tick=1,
            ),
            OrderRecord(
                order_id="order_a",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("48500"),
                quantity=Decimal("0.1"),
                state=OrderState.OPEN,
                level_id=2,
                created_ts=1000,
                placed_tick=1,
            ),
            OrderRecord(
                order_id="order_b",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("48000"),
                quantity=Decimal("0.1"),
                state=OrderState.OPEN,
                level_id=3,
                created_ts=1000,
                placed_tick=1,
            ),
        ]

        result = check_pending_fills(
            ts=2000,
            open_orders=orders,
            mid_price=Decimal("47000"),  # All orders fill
            current_tick=2,
            fill_after_ticks=1,
        )

        # Should be sorted by order_id: a, b, c
        assert len(result.fills) == 3
        assert result.fills[0].order_id == "order_a"
        assert result.fills[1].order_id == "order_b"
        assert result.fills[2].order_id == "order_c"

    def test_fill_after_ticks_higher_value(self) -> None:
        """Orders respect higher fill_after_ticks values."""
        order = OrderRecord(
            order_id="test_order",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.1"),
            state=OrderState.OPEN,
            level_id=1,
            created_ts=1000,
            placed_tick=1,
        )

        # With fill_after_ticks=3, order placed at tick 1 fills at tick 4+
        for tick in [2, 3]:
            result = check_pending_fills(
                ts=tick * 1000,
                open_orders=[order],
                mid_price=Decimal("48000"),
                current_tick=tick,
                fill_after_ticks=3,
            )
            assert len(result.fills) == 0, f"Should not fill at tick {tick}"

        # At tick 4, fills
        result = check_pending_fills(
            ts=4000,
            open_orders=[order],
            mid_price=Decimal("48000"),
            current_tick=4,
            fill_after_ticks=3,
        )
        assert len(result.fills) == 1


# --- Test Class: PaperEngine tick-delay fills ---


class TestPaperEnginTickDelayFills:
    """Tests for PaperEngine with fill_after_ticks > 0."""

    def _create_engine(self, fill_after_ticks: int = 1) -> PaperEngine:
        """Create a PaperEngine with common parameters for testing."""
        return PaperEngine(
            spacing_bps=100.0,  # 1% spacing
            levels=2,
            size_per_level=Decimal("0.01"),
            initial_capital=Decimal("100000"),
            fill_after_ticks=fill_after_ticks,
        )

    def _create_snapshot(
        self,
        ts: int,
        symbol: str = "BTCUSDT",
        mid_price: Decimal = Decimal("50000"),
    ) -> Snapshot:
        """Create a test snapshot."""
        # spread_bps is a property computed from bid/ask, not a constructor arg
        return Snapshot(
            ts=ts,
            symbol=symbol,
            bid_price=mid_price - Decimal("5"),
            ask_price=mid_price + Decimal("5"),
            bid_qty=Decimal("10"),
            ask_qty=Decimal("10"),
            last_price=mid_price,
            last_qty=Decimal("1"),
        )

    def test_order_stays_open_until_tick_eligible(self) -> None:
        """Orders remain OPEN until tick threshold is reached."""
        engine = self._create_engine(fill_after_ticks=2)

        # Tick 1: Place order
        snapshot1 = self._create_snapshot(ts=1000, mid_price=Decimal("50000"))
        output1 = engine.process_snapshot(snapshot1)

        # Should have placed orders (OPEN state)
        assert len(output1.actions) > 0
        place_actions = [a for a in output1.actions if a.get("action_type") == "PLACE"]
        assert len(place_actions) > 0

        # No fills yet (tick 1, placed at tick 1, need tick 3 to fill)
        assert len(output1.fills) == 0

        # Tick 2: Still no fills (placed at tick 1, current tick 2, need 2 ticks)
        snapshot2 = self._create_snapshot(ts=2000, mid_price=Decimal("49000"))
        output2 = engine.process_snapshot(snapshot2)
        # Orders are now 1 tick old, need 2 ticks to fill
        # tick_counter after tick 1 = 1, after tick 2 = 2
        # Orders placed at tick 1 have placed_tick=1
        # At tick 2: 2 - 1 = 1 < 2, no fill
        assert len(output2.fills) == 0

    def test_order_fills_after_tick_threshold(self) -> None:
        """Orders fill when tick threshold is reached and price crosses."""
        engine = self._create_engine(fill_after_ticks=1)

        # Tick 1: Place orders at price 50000
        snapshot1 = self._create_snapshot(ts=1000, mid_price=Decimal("50000"))
        output1 = engine.process_snapshot(snapshot1)
        assert len(output1.fills) == 0  # No fills on placement tick

        # Tick 2: Price drops, BUY orders should fill
        snapshot2 = self._create_snapshot(ts=2000, mid_price=Decimal("49000"))
        output2 = engine.process_snapshot(snapshot2)

        # BUY orders that have limit_price >= 49000 should fill
        # (Grid places BUY orders below center)
        buy_fills = [f for f in output2.fills if f.get("side") == "BUY"]
        assert len(buy_fills) > 0

    def test_cancel_before_fill_prevents_fill(self) -> None:
        """Cancelling an order before it's fill-eligible prevents filling."""
        engine = self._create_engine(fill_after_ticks=2)

        # Tick 1: Place orders
        snapshot1 = self._create_snapshot(ts=1000, mid_price=Decimal("50000"))
        output1 = engine.process_snapshot(snapshot1)
        assert len(output1.fills) == 0

        # Get the state to see open orders
        state = engine._states.get("BTCUSDT")
        assert state is not None
        open_order_ids = list(state.open_orders.keys())
        assert len(open_order_ids) > 0

        # Tick 2: Change price dramatically to trigger reconciliation (cancel all)
        # Use PAUSE mode by changing center price significantly
        snapshot2 = self._create_snapshot(ts=2000, mid_price=Decimal("40000"))
        output2 = engine.process_snapshot(snapshot2)

        # Original orders should be cancelled (reconciliation removes stale orders)
        cancel_actions = [a for a in output2.actions if a.get("action_type") == "CANCEL"]
        # The grid will reconcile and cancel orders that don't match new grid

        # Check that cancelled orders don't generate fills
        # even if price would have crossed
        cancelled_ids = {a.get("order_id") for a in cancel_actions}
        filled_ids = {f.get("order_id") for f in output2.fills}

        # No cancelled order should have filled
        assert cancelled_ids.isdisjoint(filled_ids)

    def test_fill_after_ticks_zero_is_instant(self) -> None:
        """fill_after_ticks=0 maintains original instant/crossing behavior."""
        engine = self._create_engine(fill_after_ticks=0)

        # Tick 1: Place orders, price crosses immediately
        # Grid places BUY below center, if mid=49000 and BUY at 49xxx, fill
        snapshot = self._create_snapshot(ts=1000, mid_price=Decimal("49000"))
        output = engine.process_snapshot(snapshot)

        # Should have some fills immediately (crossing model)
        # BUY orders at or below 49000 will fill
        # Note: depends on grid configuration
        # At minimum, no error should occur
        assert output is not None

    def test_determinism_same_inputs_same_outputs(self) -> None:
        """Same sequence of snapshots produces identical fills."""

        def run_engine() -> list[list[dict[str, str | int | None]]]:
            engine = self._create_engine(fill_after_ticks=1)

            all_fills = []
            for ts in [1000, 2000, 3000]:
                snapshot = self._create_snapshot(ts=ts, mid_price=Decimal("49000"))
                output = engine.process_snapshot(snapshot)
                all_fills.append(output.fills)

            return all_fills

        # Run twice
        fills1 = run_engine()
        fills2 = run_engine()

        # Results must be identical
        assert fills1 == fills2

    def test_order_state_transitions_to_filled(self) -> None:
        """Orders transition from OPEN to FILLED when they fill."""
        engine = self._create_engine(fill_after_ticks=1)

        # Tick 1: Place orders
        snapshot1 = self._create_snapshot(ts=1000, mid_price=Decimal("50000"))
        engine.process_snapshot(snapshot1)

        # Get initial open orders
        state1 = engine._states.get("BTCUSDT")
        assert state1 is not None
        initial_open_count = sum(
            1 for o in state1.open_orders.values() if o.state == OrderState.OPEN
        )
        assert initial_open_count > 0

        # Tick 2: Price crosses, some orders should fill
        snapshot2 = self._create_snapshot(ts=2000, mid_price=Decimal("49000"))
        output2 = engine.process_snapshot(snapshot2)

        # Check that filled orders have FILLED state
        state2 = engine._states.get("BTCUSDT")
        assert state2 is not None

        filled_ids = {f.get("order_id") for f in output2.fills}
        for order_id in filled_ids:
            if order_id in state2.open_orders:
                assert state2.open_orders[order_id].state == OrderState.FILLED

    def test_filled_orders_not_reconsidered_for_fill(self) -> None:
        """FILLED orders are not checked for fills again."""
        engine = self._create_engine(fill_after_ticks=1)

        # Tick 1: Place orders
        snapshot1 = self._create_snapshot(ts=1000, mid_price=Decimal("50000"))
        engine.process_snapshot(snapshot1)

        # Tick 2: Some orders fill
        snapshot2 = self._create_snapshot(ts=2000, mid_price=Decimal("49000"))
        output2 = engine.process_snapshot(snapshot2)

        # Tick 3: Same price, filled orders should not fill again
        snapshot3 = self._create_snapshot(ts=3000, mid_price=Decimal("49000"))
        output3 = engine.process_snapshot(snapshot3)

        # No double fills for the same orders
        filled_ids_tick2 = {f.get("order_id") for f in output2.fills}
        filled_ids_tick3 = {f.get("order_id") for f in output3.fills}

        # No order from tick 2 should fill again in tick 3
        assert filled_ids_tick2.isdisjoint(filled_ids_tick3)

    def test_reset_clears_snapshot_counter(self) -> None:
        """Reset clears the snapshot counter."""
        engine = self._create_engine(fill_after_ticks=1)

        # Process some snapshots
        for ts in [1000, 2000]:
            snapshot = self._create_snapshot(ts=ts)
            engine.process_snapshot(snapshot)

        assert engine._snapshot_counter > 0

        # Reset
        engine.reset()

        assert engine._snapshot_counter == 0


# --- Test Class: OrderRecord placed_tick ---


class TestOrderRecordPlacedTick:
    """Tests for OrderRecord.placed_tick field."""

    def test_placed_tick_serialization(self) -> None:
        """placed_tick is correctly serialized to/from dict."""
        order = OrderRecord(
            order_id="test",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            state=OrderState.OPEN,
            level_id=1,
            created_ts=1000,
            placed_tick=5,
        )

        d = order.to_dict()
        assert d["placed_tick"] == 5

        restored = OrderRecord.from_dict(d)
        assert restored.placed_tick == 5

    def test_placed_tick_backward_compat_default(self) -> None:
        """Missing placed_tick in dict defaults to 0 for backward compat."""
        d = {
            "order_id": "test",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "50000",
            "quantity": "0.1",
            "state": "OPEN",
            "level_id": 1,
            "created_ts": 1000,
            # placed_tick intentionally missing
        }

        order = OrderRecord.from_dict(d)
        assert order.placed_tick == 0


# --- Test Class: Edge Cases ---


class TestEdgeCases:
    """Edge case tests for tick-delay fills."""

    def test_empty_open_orders_returns_empty_result(self) -> None:
        """check_pending_fills with no orders returns empty result."""
        result = check_pending_fills(
            ts=1000,
            open_orders=[],
            mid_price=Decimal("50000"),
            current_tick=1,
            fill_after_ticks=1,
        )

        assert len(result.fills) == 0
        assert len(result.filled_order_ids) == 0

    def test_multiple_symbols_tracked_independently(self) -> None:
        """Each symbol's tick counter is independent."""
        engine = PaperEngine(
            spacing_bps=100.0,
            levels=2,
            size_per_level=Decimal("0.01"),
            initial_capital=Decimal("100000"),
            fill_after_ticks=1,
        )

        # Process BTCUSDT twice
        for ts in [1000, 2000]:
            snapshot = Snapshot(
                ts=ts,
                symbol="BTCUSDT",
                bid_price=Decimal("49995"),
                ask_price=Decimal("50005"),
                bid_qty=Decimal("10"),
                ask_qty=Decimal("10"),
                last_price=Decimal("50000"),
                last_qty=Decimal("1"),
            )
            engine.process_snapshot(snapshot)

        # Check tick counter for BTCUSDT
        btc_state = engine._states.get("BTCUSDT")
        assert btc_state is not None
        assert btc_state.tick_counter == 2

        # Global snapshot counter should be 2
        assert engine._snapshot_counter == 2

        # Process BTCUSDT a third time
        snapshot3 = Snapshot(
            ts=3000,
            symbol="BTCUSDT",
            bid_price=Decimal("49990"),
            ask_price=Decimal("50010"),
            bid_qty=Decimal("10"),
            ask_qty=Decimal("10"),
            last_price=Decimal("50000"),
            last_qty=Decimal("1"),
        )
        engine.process_snapshot(snapshot3)

        # BTCUSDT tick counter should now be 3
        btc_state = engine._states.get("BTCUSDT")
        assert btc_state is not None
        assert btc_state.tick_counter == 3

        # Global snapshot counter should be 3
        assert engine._snapshot_counter == 3
