"""Tests for CycleEngine: fill → TP + replenishment.

Tests:
- TP generation for BUY fills (SELL TP above)
- TP generation for SELL fills (BUY TP below)
- Replenishment when adds_allowed=True
- No replenishment when adds_allowed=False
- Determinism: same inputs → same intents
- Intent ID uniqueness and stability
- Price/quantity rounding
"""

from __future__ import annotations

from decimal import Decimal

from grinder.paper.cycle_engine import CycleEngine, CycleIntent, CycleResult
from grinder.paper.fills import Fill


class TestTPGeneration:
    """Test take-profit order generation."""

    def test_buy_fill_generates_sell_tp(self) -> None:
        """BUY fill generates SELL TP at p_fill * (1 + step_pct)."""
        engine = CycleEngine(step_pct=Decimal("0.001"))  # 0.1% = 10 bps
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=False)

        assert result.fills_processed == 1
        assert result.tps_generated == 1
        assert result.replenishments_generated == 0
        assert len(result.intents) == 1

        tp_intent = result.intents[0]
        assert tp_intent.intent_type == "TP"
        assert tp_intent.side == "SELL"
        # 50000 * 1.001 = 50050
        assert tp_intent.price == Decimal("50050.00")
        assert tp_intent.quantity == Decimal("0.100")
        assert tp_intent.symbol == "BTCUSDT"
        assert tp_intent.source_fill_id == "fill_1"

    def test_sell_fill_generates_buy_tp(self) -> None:
        """SELL fill generates BUY TP at p_fill * (1 - step_pct)."""
        engine = CycleEngine(step_pct=Decimal("0.001"))  # 0.1% = 10 bps
        fill = Fill(
            ts=1000,
            symbol="ETHUSDT",
            side="SELL",
            price=Decimal("3000"),
            quantity=Decimal("1.5"),
            order_id="fill_2",
        )

        result = engine.process_fills([fill], adds_allowed=False)

        assert result.tps_generated == 1
        assert len(result.intents) == 1

        tp_intent = result.intents[0]
        assert tp_intent.intent_type == "TP"
        assert tp_intent.side == "BUY"
        # 3000 * 0.999 = 2997
        assert tp_intent.price == Decimal("2997.00")
        assert tp_intent.quantity == Decimal("1.500")

    def test_tp_price_with_larger_step(self) -> None:
        """TP price calculation with larger step percentage."""
        engine = CycleEngine(step_pct=Decimal("0.01"))  # 1% = 100 bps
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("10000"),
            quantity=Decimal("1"),
            order_id="fill_3",
        )

        result = engine.process_fills([fill], adds_allowed=False)
        tp_intent = result.intents[0]

        # 10000 * 1.01 = 10100
        assert tp_intent.price == Decimal("10100.00")


class TestReplenishment:
    """Test replenishment order generation."""

    def test_replenishment_when_adds_allowed(self) -> None:
        """Replenishment is generated when adds_allowed=True."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=True)

        assert result.fills_processed == 1
        assert result.tps_generated == 1
        assert result.replenishments_generated == 1
        assert len(result.intents) == 2

        # First intent is TP
        tp_intent = result.intents[0]
        assert tp_intent.intent_type == "TP"
        assert tp_intent.side == "SELL"

        # Second intent is replenishment
        replenish_intent = result.intents[1]
        assert replenish_intent.intent_type == "REPLENISH"
        assert replenish_intent.side == "BUY"  # Same side as fill
        # 50000 * 0.999 = 49950 (below fill price)
        assert replenish_intent.price == Decimal("49950.00")
        assert replenish_intent.quantity == Decimal("0.100")

    def test_no_replenishment_when_adds_disallowed(self) -> None:
        """No replenishment when adds_allowed=False."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=False)

        assert result.replenishments_generated == 0
        assert len(result.intents) == 1
        assert result.intents[0].intent_type == "TP"

    def test_sell_fill_replenishment_above(self) -> None:
        """SELL fill replenishment is placed above fill price."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=True)
        replenish_intent = result.intents[1]

        assert replenish_intent.intent_type == "REPLENISH"
        assert replenish_intent.side == "SELL"  # Same side as fill
        # 50000 * 1.001 = 50050 (above fill price)
        assert replenish_intent.price == Decimal("50050.00")

    def test_custom_replenish_offset(self) -> None:
        """Replenishment with custom offset percentage."""
        engine = CycleEngine(
            step_pct=Decimal("0.001"),  # TP offset
            replenish_offset_pct=Decimal("0.002"),  # Replenish offset (different)
        )
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=True)

        tp_intent = result.intents[0]
        replenish_intent = result.intents[1]

        # TP uses step_pct: 50000 * 1.001 = 50050
        assert tp_intent.price == Decimal("50050.00")

        # Replenish uses replenish_offset_pct: 50000 * 0.998 = 49900
        assert replenish_intent.price == Decimal("49900.00")


class TestMultipleFills:
    """Test processing multiple fills."""

    def test_multiple_fills_order_preserved(self) -> None:
        """Fills are processed in order, intents generated sequentially."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fills = [
            Fill(
                ts=1000,
                symbol="BTCUSDT",
                side="BUY",
                price=Decimal("50000"),
                quantity=Decimal("0.1"),
                order_id="fill_1",
            ),
            Fill(
                ts=1001,
                symbol="BTCUSDT",
                side="SELL",
                price=Decimal("50100"),
                quantity=Decimal("0.2"),
                order_id="fill_2",
            ),
        ]

        result = engine.process_fills(fills, adds_allowed=True)

        assert result.fills_processed == 2
        assert result.tps_generated == 2
        assert result.replenishments_generated == 2
        assert len(result.intents) == 4

        # Order: fill_1 TP, fill_1 replenish, fill_2 TP, fill_2 replenish
        assert result.intents[0].source_fill_id == "fill_1"
        assert result.intents[0].intent_type == "TP"
        assert result.intents[1].source_fill_id == "fill_1"
        assert result.intents[1].intent_type == "REPLENISH"
        assert result.intents[2].source_fill_id == "fill_2"
        assert result.intents[2].intent_type == "TP"
        assert result.intents[3].source_fill_id == "fill_2"
        assert result.intents[3].intent_type == "REPLENISH"

    def test_mixed_symbols(self) -> None:
        """Process fills for multiple symbols correctly."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fills = [
            Fill(
                ts=1000,
                symbol="BTCUSDT",
                side="BUY",
                price=Decimal("50000"),
                quantity=Decimal("0.1"),
                order_id="btc_fill",
            ),
            Fill(
                ts=1001,
                symbol="ETHUSDT",
                side="SELL",
                price=Decimal("3000"),
                quantity=Decimal("1.0"),
                order_id="eth_fill",
            ),
        ]

        result = engine.process_fills(fills, adds_allowed=False)

        assert len(result.intents) == 2
        assert result.intents[0].symbol == "BTCUSDT"
        assert result.intents[1].symbol == "ETHUSDT"


class TestDeterminism:
    """Test deterministic behavior."""

    def test_same_inputs_same_intents(self) -> None:
        """Identical inputs produce identical intents."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result1 = engine.process_fills([fill], adds_allowed=True)
        result2 = engine.process_fills([fill], adds_allowed=True)

        assert len(result1.intents) == len(result2.intents)
        for i1, i2 in zip(result1.intents, result2.intents, strict=True):
            assert i1 == i2

    def test_intent_ids_deterministic(self) -> None:
        """Intent IDs are deterministic based on fill data."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result1 = engine.process_fills([fill], adds_allowed=True)
        result2 = engine.process_fills([fill], adds_allowed=True)

        assert result1.intents[0].intent_id == result2.intents[0].intent_id
        assert result1.intents[1].intent_id == result2.intents[1].intent_id

    def test_intent_id_format(self) -> None:
        """Intent IDs follow expected format."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=True)

        tp_intent = result.intents[0]
        replenish_intent = result.intents[1]

        # Format: cycle_{type}_{source_id}_{side}_{price}
        assert tp_intent.intent_id == "cycle_TP_fill_1_SELL_50050.00"
        assert replenish_intent.intent_id == "cycle_REPLENISH_fill_1_BUY_49950.00"


class TestPrecision:
    """Test price and quantity precision."""

    def test_price_rounding(self) -> None:
        """Prices are rounded to configured precision."""
        engine = CycleEngine(step_pct=Decimal("0.001"), price_precision=2)
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50123.456789"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=True)

        # 50123.456789 * 1.001 = 50173.580245789 → rounded to 50173.58
        tp_intent = result.intents[0]
        assert tp_intent.price == Decimal("50173.58")

    def test_quantity_rounding(self) -> None:
        """Quantities are rounded to configured precision."""
        engine = CycleEngine(step_pct=Decimal("0.001"), quantity_precision=3)
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.123456789"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=True)

        tp_intent = result.intents[0]
        assert tp_intent.quantity == Decimal("0.123")

    def test_custom_precision(self) -> None:
        """Custom precision settings are respected."""
        engine = CycleEngine(
            step_pct=Decimal("0.001"),
            price_precision=4,
            quantity_precision=6,
        )
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000.12345"),
            quantity=Decimal("0.123456789"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=True)

        tp_intent = result.intents[0]
        # 50000.12345 * 1.001 = 50050.12357345 → 50050.1235 (4 decimals, ROUND_DOWN)
        assert tp_intent.price == Decimal("50050.1235")
        assert tp_intent.quantity == Decimal("0.123456")


class TestSerialization:
    """Test serialization of intents and results."""

    def test_intent_to_dict(self) -> None:
        """CycleIntent serializes correctly."""
        intent = CycleIntent(
            intent_type="TP",
            side="SELL",
            price=Decimal("50050"),
            quantity=Decimal("0.1"),
            symbol="BTCUSDT",
            source_fill_id="fill_1",
            intent_id="cycle_TP_fill_1_SELL_50050",
        )

        d = intent.to_dict()

        assert d["intent_type"] == "TP"
        assert d["side"] == "SELL"
        assert d["price"] == "50050"
        assert d["quantity"] == "0.1"
        assert d["symbol"] == "BTCUSDT"
        assert d["source_fill_id"] == "fill_1"
        assert d["intent_id"] == "cycle_TP_fill_1_SELL_50050"

    def test_intent_from_dict(self) -> None:
        """CycleIntent deserializes correctly."""
        d = {
            "intent_type": "REPLENISH",
            "side": "BUY",
            "price": "49950.50",
            "quantity": "1.5",
            "symbol": "ETHUSDT",
            "source_fill_id": "order_xyz",
            "intent_id": "cycle_REPLENISH_order_xyz_BUY_49950.50",
        }

        intent = CycleIntent.from_dict(d)

        assert intent.intent_type == "REPLENISH"
        assert intent.side == "BUY"
        assert intent.price == Decimal("49950.50")
        assert intent.quantity == Decimal("1.5")
        assert intent.symbol == "ETHUSDT"
        assert intent.source_fill_id == "order_xyz"

    def test_intent_roundtrip(self) -> None:
        """CycleIntent serialization roundtrip."""
        intent = CycleIntent(
            intent_type="TP",
            side="SELL",
            price=Decimal("50050.123"),
            quantity=Decimal("0.123"),
            symbol="BTCUSDT",
            source_fill_id="fill_1",
            intent_id="cycle_TP_fill_1_SELL_50050.123",
        )

        intent2 = CycleIntent.from_dict(intent.to_dict())
        assert intent == intent2

    def test_result_to_dict(self) -> None:
        """CycleResult serializes correctly."""
        intents = [
            CycleIntent(
                intent_type="TP",
                side="SELL",
                price=Decimal("50050"),
                quantity=Decimal("0.1"),
                symbol="BTCUSDT",
                source_fill_id="fill_1",
                intent_id="id_1",
            ),
        ]
        result = CycleResult(
            intents=intents,
            fills_processed=1,
            tps_generated=1,
            replenishments_generated=0,
        )

        d = result.to_dict()

        assert d["fills_processed"] == 1
        assert d["tps_generated"] == 1
        assert d["replenishments_generated"] == 0
        assert len(d["intents"]) == 1
        assert d["intents"][0]["intent_type"] == "TP"


class TestSingleFillConvenience:
    """Test process_single_fill convenience method."""

    def test_single_fill_with_adds(self) -> None:
        """Process single fill with adds_allowed=True."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        intents = engine.process_single_fill(fill, adds_allowed=True)

        assert len(intents) == 2
        assert intents[0].intent_type == "TP"
        assert intents[1].intent_type == "REPLENISH"

    def test_single_fill_without_adds(self) -> None:
        """Process single fill with adds_allowed=False."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        intents = engine.process_single_fill(fill, adds_allowed=False)

        assert len(intents) == 1
        assert intents[0].intent_type == "TP"


class TestEdgeCases:
    """Test edge cases."""

    def test_empty_fills_list(self) -> None:
        """Process empty fills list returns empty result."""
        engine = CycleEngine(step_pct=Decimal("0.001"))

        result = engine.process_fills([], adds_allowed=True)

        assert result.fills_processed == 0
        assert result.tps_generated == 0
        assert result.replenishments_generated == 0
        assert len(result.intents) == 0

    def test_zero_quantity_fill(self) -> None:
        """Zero quantity fill produces zero quantity intents."""
        engine = CycleEngine(step_pct=Decimal("0.001"))
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0"),
            order_id="fill_zero",
        )

        result = engine.process_fills([fill], adds_allowed=True)

        assert result.tps_generated == 1
        assert result.intents[0].quantity == Decimal("0.000")

    def test_very_small_step(self) -> None:
        """Very small step percentage (1 bp)."""
        engine = CycleEngine(step_pct=Decimal("0.0001"))  # 1 bp
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=False)

        # 50000 * 1.0001 = 50005
        assert result.intents[0].price == Decimal("50005.00")

    def test_large_step(self) -> None:
        """Large step percentage (5%)."""
        engine = CycleEngine(step_pct=Decimal("0.05"))  # 5%
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="fill_1",
        )

        result = engine.process_fills([fill], adds_allowed=False)

        # 50000 * 1.05 = 52500
        assert result.intents[0].price == Decimal("52500.00")
