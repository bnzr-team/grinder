"""Tests for domain contracts."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from grinder.contracts import (
    Decision,
    DecisionReason,
    OrderIntent,
    PolicyContext,
    Position,
    Snapshot,
)
from grinder.core import GridMode, OrderSide

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "contracts"


class TestSnapshot:
    """Tests for Snapshot contract."""

    def test_roundtrip_dict(self) -> None:
        """Test dict serialization roundtrip."""
        snap = Snapshot(
            ts=1706000000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000.00"),
            ask_price=Decimal("50001.00"),
            bid_qty=Decimal("1.5"),
            ask_qty=Decimal("2.0"),
            last_price=Decimal("50000.50"),
            last_qty=Decimal("0.1"),
        )
        d = snap.to_dict()
        restored = Snapshot.from_dict(d)
        assert restored == snap

    def test_mid_price(self) -> None:
        """Test mid price calculation."""
        snap = Snapshot(
            ts=0,
            symbol="TEST",
            bid_price=Decimal("100"),
            ask_price=Decimal("102"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("101"),
            last_qty=Decimal("1"),
        )
        assert snap.mid_price == Decimal("101")

    def test_spread_bps(self) -> None:
        """Test spread calculation in bps."""
        snap = Snapshot(
            ts=0,
            symbol="TEST",
            bid_price=Decimal("100"),
            ask_price=Decimal("101"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("100.5"),
            last_qty=Decimal("1"),
        )
        # Spread = 1, mid = 100.5, bps = 1/100.5 * 10000 ≈ 99.5
        assert abs(snap.spread_bps - 99.5) < 1

    def test_frozen(self) -> None:
        """Test immutability."""
        snap = Snapshot(
            ts=0,
            symbol="TEST",
            bid_price=Decimal("100"),
            ask_price=Decimal("101"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("100"),
            last_qty=Decimal("1"),
        )
        with pytest.raises(AttributeError):
            snap.ts = 1  # type: ignore[misc]


class TestPosition:
    """Tests for Position contract."""

    def test_roundtrip_dict(self) -> None:
        """Test dict serialization roundtrip."""
        pos = Position(
            symbol="ETHUSDT",
            size=Decimal("10.5"),
            entry_price=Decimal("3000.00"),
            unrealized_pnl=Decimal("50.25"),
        )
        d = pos.to_dict()
        restored = Position.from_dict(d)
        assert restored == pos


class TestPolicyContext:
    """Tests for PolicyContext contract."""

    def test_roundtrip_dict(self) -> None:
        """Test dict serialization roundtrip."""
        snap = Snapshot(
            ts=1706000000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.1"),
        )
        pos = Position(
            symbol="BTCUSDT",
            size=Decimal("0.5"),
            entry_price=Decimal("49500"),
            unrealized_pnl=Decimal("250"),
        )
        ctx = PolicyContext(
            snapshot=snap,
            position=pos,
            features={"volatility": 0.02, "trend": 0.5},
            daily_pnl=Decimal("100"),
            max_position_size=Decimal("5"),
        )
        d = ctx.to_dict()
        restored = PolicyContext.from_dict(d)
        assert restored.snapshot == ctx.snapshot
        assert restored.position == ctx.position
        assert restored.features == ctx.features
        assert restored.daily_pnl == ctx.daily_pnl

    def test_no_position(self) -> None:
        """Test context without position."""
        snap = Snapshot(
            ts=0,
            symbol="TEST",
            bid_price=Decimal("100"),
            ask_price=Decimal("101"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("100"),
            last_qty=Decimal("1"),
        )
        ctx = PolicyContext(snapshot=snap, position=None)
        d = ctx.to_dict()
        restored = PolicyContext.from_dict(d)
        assert restored.position is None


class TestOrderIntent:
    """Tests for OrderIntent contract."""

    def test_roundtrip_dict(self) -> None:
        """Test dict serialization roundtrip."""
        intent = OrderIntent(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49900"),
            quantity=Decimal("0.01"),
            reason=DecisionReason.POLICY_GRID_NORMAL,
            level_id=2,
        )
        d = intent.to_dict()
        restored = OrderIntent.from_dict(d)
        assert restored == intent


class TestDecision:
    """Tests for Decision contract."""

    def test_roundtrip_dict(self) -> None:
        """Test dict serialization roundtrip."""
        intents = (
            OrderIntent(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("49900"),
                quantity=Decimal("0.01"),
                reason=DecisionReason.POLICY_GRID_NORMAL,
                level_id=1,
            ),
            OrderIntent(
                symbol="BTCUSDT",
                side=OrderSide.SELL,
                price=Decimal("50100"),
                quantity=Decimal("0.01"),
                reason=DecisionReason.POLICY_GRID_NORMAL,
                level_id=1,
            ),
        )
        decision = Decision(
            ts=1706000000000,
            symbol="BTCUSDT",
            mode=GridMode.BILATERAL,
            reason=DecisionReason.POLICY_GRID_NORMAL,
            order_intents=intents,
            cancel_order_ids=("order-123", "order-456"),
            policy_name="RangeMaster",
            context_hash="abc123",
        )
        d = decision.to_dict()
        restored = Decision.from_dict(d)
        assert restored == decision

    def test_json_roundtrip(self) -> None:
        """Test JSON serialization roundtrip."""
        decision = Decision(
            ts=1706000000000,
            symbol="BTCUSDT",
            mode=GridMode.PAUSE,
            reason=DecisionReason.RISK_KILL_SWITCH,
            policy_name="EmergencyStop",
        )
        json_str = decision.to_json()
        restored = Decision.from_json(json_str)
        assert restored == decision

    def test_json_deterministic(self) -> None:
        """Test that JSON serialization is deterministic."""
        decision = Decision(
            ts=1706000000000,
            symbol="BTCUSDT",
            mode=GridMode.BILATERAL,
            reason=DecisionReason.POLICY_GRID_NORMAL,
            order_intents=(
                OrderIntent(
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("50000"),
                    quantity=Decimal("0.01"),
                    reason=DecisionReason.POLICY_GRID_NORMAL,
                ),
            ),
            policy_name="Test",
        )
        json1 = decision.to_json()
        json2 = decision.to_json()
        assert json1 == json2


class TestFixtures:
    """Tests for golden fixture files."""

    def test_snapshot_fixture(self) -> None:
        """Test parsing snapshot fixture."""
        fixture_path = FIXTURES_DIR / "snapshot_btc.json"
        if not fixture_path.exists():
            pytest.skip("Fixture not found")
        with fixture_path.open() as f:
            data = json.load(f)
        snap = Snapshot.from_dict(data)
        assert snap.symbol == "BTCUSDT"
        assert snap.ts > 0

    def test_decision_fixture(self) -> None:
        """Test parsing decision fixture."""
        fixture_path = FIXTURES_DIR / "decision_bilateral.json"
        if not fixture_path.exists():
            pytest.skip("Fixture not found")
        with fixture_path.open() as f:
            data = json.load(f)
        decision = Decision.from_dict(data)
        assert decision.symbol == "BTCUSDT"
        assert decision.mode == GridMode.BILATERAL

    def test_decision_emergency_fixture(self) -> None:
        """Test parsing emergency decision fixture."""
        fixture_path = FIXTURES_DIR / "decision_emergency.json"
        if not fixture_path.exists():
            pytest.skip("Fixture not found")
        with fixture_path.open() as f:
            data = json.load(f)
        decision = Decision.from_dict(data)
        assert decision.mode == GridMode.EMERGENCY
        assert decision.reason == DecisionReason.RISK_KILL_SWITCH
