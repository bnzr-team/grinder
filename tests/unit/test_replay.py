"""Tests for replay module.

Tests:
- ReplayEngine initialization
- Snapshot processing
- End-to-end replay
- Output serialization
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from grinder.contracts import Snapshot
from grinder.replay import ReplayEngine, ReplayOutput, ReplayResult


class TestReplayOutput:
    """Test ReplayOutput data class."""

    def test_to_dict(self) -> None:
        """Test ReplayOutput serialization."""
        output = ReplayOutput(
            ts=1000,
            symbol="BTCUSDT",
            prefilter_result={"allowed": True, "reason": "PASS"},
            plan={"mode": "BILATERAL", "center_price": "50000"},
            actions=[{"action_type": "PLACE", "symbol": "BTCUSDT"}],
            events=[{"ts": 1000, "event_type": "RECONCILE"}],
        )
        d = output.to_dict()

        assert d["ts"] == 1000
        assert d["symbol"] == "BTCUSDT"
        assert d["prefilter_result"]["allowed"] is True
        assert d["plan"]["mode"] == "BILATERAL"
        assert len(d["actions"]) == 1
        assert len(d["events"]) == 1


class TestReplayResult:
    """Test ReplayResult data class."""

    def test_to_dict(self) -> None:
        """Test ReplayResult serialization."""
        output = ReplayOutput(
            ts=1000,
            symbol="BTCUSDT",
            prefilter_result={"allowed": True, "reason": "PASS"},
            plan=None,
            actions=[],
            events=[],
        )
        result = ReplayResult(
            fixture_path="/test/fixture",
            outputs=[output],
            digest="abc123",
            events_processed=1,
            errors=[],
        )
        d = result.to_dict()

        assert d["fixture_path"] == "/test/fixture"
        assert len(d["outputs"]) == 1
        assert d["digest"] == "abc123"
        assert d["events_processed"] == 1
        assert d["errors"] == []

    def test_to_json(self) -> None:
        """Test ReplayResult JSON serialization is deterministic."""
        result = ReplayResult(
            fixture_path="/test/fixture",
            outputs=[],
            digest="abc123",
            events_processed=0,
            errors=[],
        )
        json1 = result.to_json()
        json2 = result.to_json()

        assert json1 == json2
        assert '"digest":"abc123"' in json1


class TestReplayEngineInit:
    """Test ReplayEngine initialization."""

    def test_default_params(self) -> None:
        """Test ReplayEngine initializes with default parameters."""
        engine = ReplayEngine()
        assert engine._policy is not None
        assert engine._port is not None
        assert engine._engine is not None

    def test_custom_params(self) -> None:
        """Test ReplayEngine initializes with custom parameters."""
        engine = ReplayEngine(
            spacing_bps=20.0,
            levels=3,
            size_per_level=Decimal("50"),
            price_precision=4,
            quantity_precision=2,
        )
        assert engine._policy.spacing_bps == 20.0
        assert engine._policy.levels == 3


class TestReplayEngineProcessSnapshot:
    """Test ReplayEngine.process_snapshot."""

    def test_process_snapshot_allowed(self) -> None:
        """Test processing a snapshot that passes prefilter."""
        engine = ReplayEngine()
        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000.00"),
            ask_price=Decimal("50001.00"),
            bid_qty=Decimal("10.0"),
            ask_qty=Decimal("10.0"),
            last_price=Decimal("50000.50"),
            last_qty=Decimal("0.5"),
        )

        output = engine.process_snapshot(snapshot)

        assert output.ts == 1000
        assert output.symbol == "BTCUSDT"
        assert output.prefilter_result["allowed"] is True
        assert output.plan is not None
        assert output.plan["mode"] == "BILATERAL"

    def test_process_multiple_snapshots_same_symbol(self) -> None:
        """Test processing multiple snapshots for same symbol maintains state."""
        engine = ReplayEngine()

        # First snapshot
        snapshot1 = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000.00"),
            ask_price=Decimal("50001.00"),
            bid_qty=Decimal("10.0"),
            ask_qty=Decimal("10.0"),
            last_price=Decimal("50000.50"),
            last_qty=Decimal("0.5"),
        )
        output1 = engine.process_snapshot(snapshot1)

        # First snapshot should place orders
        assert len(output1.actions) > 0

        # Second snapshot at same price - should reconcile (no new orders)
        snapshot2 = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000.00"),
            ask_price=Decimal("50001.00"),
            bid_qty=Decimal("10.0"),
            ask_qty=Decimal("10.0"),
            last_price=Decimal("50000.50"),
            last_qty=Decimal("0.5"),
        )
        output2 = engine.process_snapshot(snapshot2)

        # Second snapshot should have fewer actions (orders already placed)
        # Only reconcile event, no new placements needed
        place_actions = [a for a in output2.actions if a.get("action_type") == "PLACE"]
        assert len(place_actions) == 0


class TestReplayEngineDigest:
    """Test ReplayEngine digest computation."""

    def test_digest_is_deterministic(self) -> None:
        """Test that digest is deterministic for same inputs."""
        fixture_dir = Path(__file__).parent.parent / "fixtures" / "sample_day"

        engine1 = ReplayEngine()
        result1 = engine1.run(fixture_dir)

        engine2 = ReplayEngine()
        result2 = engine2.run(fixture_dir)

        assert result1.digest == result2.digest

    def test_digest_changes_with_different_params(self) -> None:
        """Test that digest changes with different engine parameters."""
        fixture_dir = Path(__file__).parent.parent / "fixtures" / "sample_day"

        engine1 = ReplayEngine(spacing_bps=10.0, levels=5)
        result1 = engine1.run(fixture_dir)

        engine2 = ReplayEngine(spacing_bps=20.0, levels=3)
        result2 = engine2.run(fixture_dir)

        # Different parameters should produce different digests
        assert result1.digest != result2.digest
