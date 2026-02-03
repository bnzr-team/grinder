"""Tests for paper trading engine.

Tests:
- PaperEngine: end-to-end fixture processing with gating
- PaperOutput: serialization
- PaperResult: metrics and digest
- CLI integration
- Determinism
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from grinder.contracts import Snapshot
from grinder.paper import PaperEngine, PaperOutput, PaperResult
from grinder.policies.grid.static import StaticGridPolicy

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "sample_day"
FIXTURE_ALLOWED_DIR = Path(__file__).parent.parent / "fixtures" / "sample_day_allowed"

# Canonical digests (locked for determinism)
# v1 schema: includes fills and pnl_snapshot in output
# v1.1: crossing/touch fill model (PR-ASM-P0-01)
EXPECTED_PAPER_DIGEST_SAMPLE_DAY = "66b29a4e92192f8f"  # 0 fills (blocked by gating)
EXPECTED_PAPER_DIGEST_ALLOWED = "3ecf49cd03db1b07"  # 10 fills with crossing/touch model


class TestPaperOutput:
    """Test PaperOutput data class."""

    def test_to_dict(self) -> None:
        """Test serialization to dict."""
        output = PaperOutput(
            ts=1000,
            symbol="BTCUSDT",
            prefilter_result={"allowed": True, "reason": "PASS"},
            gating_result={"allowed": True, "reason": "PASS"},
            plan={"mode": "BI_LATERAL", "center_price": "50000"},
            actions=[{"type": "PLACE", "price": "49990"}],
            events=[{"type": "ORDER_PLACED"}],
            blocked_by_gating=False,
        )
        d = output.to_dict()
        assert d["ts"] == 1000
        assert d["symbol"] == "BTCUSDT"
        assert d["prefilter_result"]["allowed"] is True
        assert d["gating_result"]["allowed"] is True
        assert d["plan"]["mode"] == "BI_LATERAL"
        assert d["blocked_by_gating"] is False


class TestPaperResult:
    """Test PaperResult data class."""

    def test_to_dict(self) -> None:
        """Test serialization to dict."""
        result = PaperResult(
            fixture_path="/test/fixture",
            events_processed=10,
            events_gated=2,
            orders_placed=8,
            orders_blocked=2,
            digest="abc123",
        )
        d = result.to_dict()
        assert d["fixture_path"] == "/test/fixture"
        assert d["events_processed"] == 10
        assert d["events_gated"] == 2
        assert d["orders_placed"] == 8
        assert d["orders_blocked"] == 2
        assert d["digest"] == "abc123"

    def test_to_json_deterministic(self) -> None:
        """Test JSON serialization is deterministic."""
        result = PaperResult(
            fixture_path="/test",
            digest="xyz",
            events_processed=5,
        )
        json1 = result.to_json()
        json2 = result.to_json()
        assert json1 == json2


class TestPaperEngine:
    """Test PaperEngine."""

    def test_run_fixture(self) -> None:
        """Test running paper engine on fixture."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)

        assert result.events_processed == 5
        assert len(result.outputs) == 5
        assert not result.errors
        assert result.digest != ""

    def test_determinism(self) -> None:
        """Test that paper engine produces deterministic digest."""
        digests = []
        for _ in range(3):
            engine = PaperEngine()
            result = engine.run(FIXTURE_DIR)
            digests.append(result.digest)

        assert all(d == digests[0] for d in digests), f"Digests differ: {digests}"

    def test_outputs_have_gating_result(self) -> None:
        """Test that outputs include gating results."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)

        for output in result.outputs:
            assert "allowed" in output.gating_result
            assert "reason" in output.gating_result

    def test_gating_blocks_rapid_orders(self) -> None:
        """Test that gating blocks orders with strict rate limit."""
        # Very strict rate limiter - only 1 order per minute
        engine = PaperEngine(
            max_orders_per_minute=1,
            cooldown_ms=60000,  # 1 minute cooldown
        )
        result = engine.run(FIXTURE_DIR)

        # First order should go through, rest should be gated
        assert result.events_gated > 0
        assert result.orders_blocked > 0

    def test_gating_blocks_excessive_notional(self) -> None:
        """Test that gating blocks orders exceeding notional limit."""
        # Very low notional limit
        engine = PaperEngine(
            max_notional_per_symbol=Decimal("1"),
            max_notional_total=Decimal("1"),
        )
        result = engine.run(FIXTURE_DIR)

        # Should hit notional limit quickly
        assert result.events_gated > 0

    def test_process_snapshot_with_gating(self) -> None:
        """Test processing single snapshot with gating."""
        # Use small size to stay within notional limits for BTC prices
        engine = PaperEngine(
            size_per_level=Decimal("0.01"),  # 0.01 BTC = ~$500 at $50k
            max_notional_per_symbol=Decimal("10000"),
        )
        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("10"),
            ask_qty=Decimal("10"),
            last_price=Decimal("50000.50"),
            last_qty=Decimal("0.5"),
        )

        output = engine.process_snapshot(snapshot)

        assert output.ts == 1000
        assert output.symbol == "BTCUSDT"
        assert output.prefilter_result["allowed"] is True
        assert output.gating_result["allowed"] is True
        assert output.plan is not None
        assert output.blocked_by_gating is False

    def test_missing_fixture_returns_error(self) -> None:
        """Test that missing fixture returns error."""
        engine = PaperEngine()
        result = engine.run(Path("/nonexistent/fixture"))

        assert result.events_processed == 0
        assert len(result.errors) >= 1
        assert "No events found" in result.errors[0]

    def test_reset(self) -> None:
        """Test reset clears engine state."""
        engine = PaperEngine(max_orders_per_minute=1)

        # Run once
        result1 = engine.run(FIXTURE_DIR)

        # Reset and run again - should get same result
        engine.reset()
        result2 = engine.run(FIXTURE_DIR)

        assert result1.digest == result2.digest

    def test_different_params_different_digest(self) -> None:
        """Test that different parameters produce different digests."""
        engine1 = PaperEngine(spacing_bps=10.0, levels=5)
        result1 = engine1.run(FIXTURE_DIR)

        engine2 = PaperEngine(spacing_bps=20.0, levels=3)
        result2 = engine2.run(FIXTURE_DIR)

        assert result1.digest != result2.digest


class TestPaperCLI:
    """Test CLI integration for paper trading."""

    def test_cli_paper_help(self) -> None:
        """Test grinder paper --help works."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.argv=['grinder', 'paper', '--help']; from grinder.cli import main; main()",
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
            env={"PYTHONPATH": "src"},
            check=False,
        )
        assert result.returncode == 0
        assert "--fixture" in result.stdout
        assert "paper" in result.stdout.lower()

    def test_cli_paper_valid_fixture(self) -> None:
        """Test paper trading with valid fixture."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys; sys.argv=['grinder', 'paper', '--fixture', '{FIXTURE_DIR}']; "
                f"from grinder.cli import main; main()",
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
            env={"PYTHONPATH": "src"},
            check=False,
        )
        assert result.returncode == 0
        assert "Paper trading completed" in result.stdout
        assert "Output digest:" in result.stdout

    def test_cli_paper_missing_fixture(self) -> None:
        """Test paper trading with missing fixture exits with error."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.argv=['grinder', 'paper', '--fixture', '/nonexistent']; "
                "from grinder.cli import main; main()",
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
            env={"PYTHONPATH": "src"},
            check=False,
        )
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_cli_paper_determinism(self) -> None:
        """Test CLI paper produces deterministic digest."""
        digests = []
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.argv=['grinder', 'paper', '--fixture', '{FIXTURE_DIR}']; "
                    f"from grinder.cli import main; main()",
                ],
                capture_output=True,
                text=True,
                cwd=Path(__file__).parent.parent.parent,
                env={"PYTHONPATH": "src"},
                check=False,
            )
            assert result.returncode == 0
            for line in result.stdout.splitlines():
                if "Output digest:" in line:
                    digests.append(line.split()[-1])
                    break

        assert len(digests) == 2
        assert digests[0] == digests[1], f"Digests differ: {digests}"


class TestAllowedOrdersFixture:
    """Tests for sample_day_allowed fixture (orders pass gating)."""

    def test_fixture_exists(self) -> None:
        """Test that the allowed-orders fixture directory exists."""
        assert FIXTURE_ALLOWED_DIR.exists(), f"Fixture not found: {FIXTURE_ALLOWED_DIR}"
        assert (FIXTURE_ALLOWED_DIR / "events.jsonl").exists()
        assert (FIXTURE_ALLOWED_DIR / "config.json").exists()

    def test_orders_are_placed(self) -> None:
        """Test that orders are placed (not blocked by gating)."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_ALLOWED_DIR)

        assert result.events_processed == 5
        assert result.orders_placed > 0, "Expected orders to be placed"
        assert result.events_gated == 0, "Expected no events blocked by gating"
        assert not result.errors

    def test_canonical_digest(self) -> None:
        """Test that digest matches expected canonical value."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_ALLOWED_DIR)

        assert result.digest == EXPECTED_PAPER_DIGEST_ALLOWED, (
            f"Digest mismatch: got {result.digest}, expected {EXPECTED_PAPER_DIGEST_ALLOWED}"
        )

    def test_determinism(self) -> None:
        """Test deterministic output across multiple runs."""
        digests = []
        for _ in range(3):
            engine = PaperEngine()
            result = engine.run(FIXTURE_ALLOWED_DIR)
            digests.append(result.digest)

        assert all(d == digests[0] for d in digests), f"Digests differ: {digests}"

    def test_prefilter_passes(self) -> None:
        """Test that prefilter allows all events (tight spread)."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_ALLOWED_DIR)

        for output in result.outputs:
            assert output.prefilter_result["allowed"] is True, (
                f"Prefilter blocked: {output.prefilter_result}"
            )

    def test_gating_passes(self) -> None:
        """Test that gating allows all events (low notional)."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_ALLOWED_DIR)

        for output in result.outputs:
            assert output.gating_result["allowed"] is True, (
                f"Gating blocked: {output.gating_result}"
            )
            assert output.blocked_by_gating is False


class TestSampleDayFixture:
    """Tests for sample_day fixture (orders blocked by gating)."""

    def test_canonical_digest(self) -> None:
        """Test that digest matches expected canonical value."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)

        assert result.digest == EXPECTED_PAPER_DIGEST_SAMPLE_DAY, (
            f"Digest mismatch: got {result.digest}, expected {EXPECTED_PAPER_DIGEST_SAMPLE_DAY}"
        )

    def test_orders_blocked(self) -> None:
        """Test that orders are blocked (notional too high for BTC prices)."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)

        assert result.events_gated == 5, "Expected all events blocked by gating"
        assert result.orders_placed == 0, "Expected no orders placed"
        assert result.orders_blocked == 5, "Expected all orders blocked"


class TestFeatureEngineIntegration:
    """Tests for FeatureEngine integration (ADR-019)."""

    def test_features_disabled_by_default(self) -> None:
        """Test that features are None when feature_engine disabled (default)."""
        engine = PaperEngine()
        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50010"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )

        output = engine.process_snapshot(snapshot)

        assert output.features is None
        assert engine._feature_engine_enabled is False

    def test_features_enabled_returns_features(self) -> None:
        """Test that features are computed when feature_engine enabled."""
        engine = PaperEngine(feature_engine_enabled=True)
        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50010"),
            bid_qty=Decimal("2"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )

        output = engine.process_snapshot(snapshot)

        assert output.features is not None
        assert output.features["ts"] == 1000
        assert output.features["symbol"] == "BTCUSDT"
        assert output.features["mid_price"] == "50005"
        assert output.features["spread_bps"] == 1  # ~2 bps truncated to int
        assert output.features["imbalance_l1_bps"] == 3333  # (2-1)/(2+1) ≈ 0.333
        assert "natr_bps" in output.features
        assert "warmup_bars" in output.features

    def test_features_not_in_digest(self) -> None:
        """Test that features are NOT included in digest (backward compat)."""
        engine_no_features = PaperEngine()
        engine_with_features = PaperEngine(feature_engine_enabled=True)

        result1 = engine_no_features.run(FIXTURE_DIR)
        result2 = engine_with_features.run(FIXTURE_DIR)

        # Digests must match even though features are computed
        assert result1.digest == result2.digest, (
            f"Digest mismatch: no_features={result1.digest}, with_features={result2.digest}"
        )

    def test_features_in_to_dict_not_in_to_digest_dict(self) -> None:
        """Test features field in to_dict but not in to_digest_dict."""
        output = PaperOutput(
            ts=1000,
            symbol="BTCUSDT",
            prefilter_result={"allowed": True, "reason": "PASS"},
            gating_result={"allowed": True, "reason": "PASS"},
            plan=None,
            actions=[],
            events=[],
            blocked_by_gating=False,
            features={"mid_price": "50000", "natr_bps": 100},
        )

        full_dict = output.to_dict()
        digest_dict = output.to_digest_dict()

        assert "features" in full_dict
        assert full_dict["features"] == {"mid_price": "50000", "natr_bps": 100}
        assert "features" not in digest_dict

    def test_digest_unchanged_sample_day(self) -> None:
        """Test canonical digest unchanged when features enabled."""
        engine = PaperEngine(feature_engine_enabled=True)
        result = engine.run(FIXTURE_DIR)

        assert result.digest == EXPECTED_PAPER_DIGEST_SAMPLE_DAY, (
            f"Digest changed with features enabled: got {result.digest}, "
            f"expected {EXPECTED_PAPER_DIGEST_SAMPLE_DAY}"
        )

    def test_digest_unchanged_allowed(self) -> None:
        """Test canonical digest unchanged when features enabled (allowed fixture)."""
        engine = PaperEngine(feature_engine_enabled=True)
        result = engine.run(FIXTURE_ALLOWED_DIR)

        assert result.digest == EXPECTED_PAPER_DIGEST_ALLOWED, (
            f"Digest changed with features enabled: got {result.digest}, "
            f"expected {EXPECTED_PAPER_DIGEST_ALLOWED}"
        )

    def test_feature_engine_enabled_in_result(self) -> None:
        """Test that feature_engine_enabled is tracked in PaperResult."""
        engine1 = PaperEngine(feature_engine_enabled=False)
        engine2 = PaperEngine(feature_engine_enabled=True)

        result1 = engine1.run(FIXTURE_DIR)
        result2 = engine2.run(FIXTURE_DIR)

        assert result1.feature_engine_enabled is False
        assert result2.feature_engine_enabled is True

    def test_features_computed_even_when_blocked(self) -> None:
        """Test that features are computed even when gating blocks the event."""
        engine = PaperEngine(
            feature_engine_enabled=True,
            max_notional_per_symbol=Decimal("1"),  # Will block
        )
        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50010"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )

        output = engine.process_snapshot(snapshot)

        # Event is blocked, but features should still be computed
        assert output.blocked_by_gating is True
        assert output.features is not None
        assert output.features["symbol"] == "BTCUSDT"

    def test_feature_engine_reset(self) -> None:
        """Test that reset clears feature engine state."""
        engine = PaperEngine(feature_engine_enabled=True)

        # Process some snapshots
        for i in range(5):
            snapshot = Snapshot(
                ts=i * 1000,
                symbol="BTCUSDT",
                bid_price=Decimal("50000"),
                ask_price=Decimal("50010"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50005"),
                last_qty=Decimal("0.1"),
            )
            engine.process_snapshot(snapshot)

        # Verify feature engine has state
        assert engine._feature_engine is not None
        assert len(engine._feature_engine.get_all_symbols()) > 0

        # Reset
        engine.reset()

        # Verify feature engine state is cleared
        assert engine._feature_engine.get_all_symbols() == []


class TestPolicyFeaturesPlumbing:
    """Tests for feature plumbing to policy (ADR-020)."""

    def test_policy_receives_features_when_enabled(self) -> None:
        """Test that policy receives full features dict when feature_engine enabled."""
        engine = PaperEngine(
            feature_engine_enabled=True,
            size_per_level=Decimal("0.01"),  # Small size to avoid notional limits
        )
        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50010"),
            bid_qty=Decimal("2"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )

        # Capture the features dict passed to policy.evaluate()
        captured_features: list[dict[str, Any]] = []
        original_evaluate = engine._policy.evaluate

        def capture_evaluate(features: dict[str, Any]) -> Any:
            captured_features.append(features.copy())
            return original_evaluate(features)

        with patch.object(engine._policy, "evaluate", side_effect=capture_evaluate):
            engine.process_snapshot(snapshot)

        # Verify features were captured
        assert len(captured_features) == 1
        features = captured_features[0]

        # Check that FeatureSnapshot fields are present
        assert "mid_price" in features
        assert "spread_bps" in features
        assert "imbalance_l1_bps" in features
        assert "thin_l1" in features
        assert "natr_bps" in features
        assert "warmup_bars" in features

        # Verify values
        assert features["mid_price"] == Decimal("50005")
        assert features["spread_bps"] == 1  # ~2 bps truncated
        assert features["imbalance_l1_bps"] == 3333  # (2-1)/(2+1)

    def test_policy_receives_only_mid_price_when_disabled(self) -> None:
        """Test that policy only receives mid_price when feature_engine disabled."""
        engine = PaperEngine(feature_engine_enabled=False)
        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50010"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )

        captured_features: list[dict[str, Any]] = []
        original_evaluate = engine._policy.evaluate

        def capture_evaluate(features: dict[str, Any]) -> Any:
            captured_features.append(features.copy())
            return original_evaluate(features)

        with patch.object(engine._policy, "evaluate", side_effect=capture_evaluate):
            engine.process_snapshot(snapshot)

        # Verify features were captured
        assert len(captured_features) == 1
        features = captured_features[0]

        # Should only have mid_price (no FeatureEngine features)
        assert "mid_price" in features
        assert features["mid_price"] == Decimal("50005")

        # FeatureSnapshot fields should NOT be present
        assert "spread_bps" not in features
        assert "imbalance_l1_bps" not in features
        assert "natr_bps" not in features

    def test_static_grid_policy_ignores_extra_features(self) -> None:
        """Test that StaticGridPolicy works with extra feature keys."""
        policy = StaticGridPolicy(spacing_bps=10.0, levels=5, size_per_level=Decimal("1"))

        # Pass features dict with extra keys (simulating FeatureSnapshot)
        features = {
            "mid_price": Decimal("50000"),
            "spread_bps": 5,
            "imbalance_l1_bps": 1000,
            "thin_l1": Decimal("10"),
            "natr_bps": 200,
            "warmup_bars": 15,
            "range_score": 50,
        }

        # Should work without error
        plan = policy.evaluate(features)

        # Verify correct behavior
        assert plan.center_price == Decimal("50000")
        assert plan.spacing_bps == 10.0
        assert plan.levels_up == 5

    def test_digests_unchanged_with_policy_features(self) -> None:
        """Test canonical digests unchanged when features passed to policy."""
        # This is a regression test for backward compatibility
        engine = PaperEngine(feature_engine_enabled=True)
        result = engine.run(FIXTURE_DIR)

        # Digest should match expected (features in policy_features don't affect digest)
        assert result.digest == EXPECTED_PAPER_DIGEST_SAMPLE_DAY, (
            f"Digest changed with policy features: got {result.digest}, "
            f"expected {EXPECTED_PAPER_DIGEST_SAMPLE_DAY}"
        )

    def test_mid_price_not_silently_overridden(self) -> None:
        """Test that mid_price from FeatureSnapshot matches base mid_price (no silent override).

        ADR-020: When feature_engine_enabled=True, policy_features.update() merges
        FeatureSnapshot.to_policy_features() which includes mid_price. This test verifies
        that both sources compute the same mid_price (from snapshot.mid_price), so the
        override is intentional and consistent.
        """
        engine = PaperEngine(
            feature_engine_enabled=True,
            size_per_level=Decimal("0.01"),
        )
        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50010"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )

        # Capture policy features to verify mid_price
        captured_features: list[dict[str, Any]] = []
        original_evaluate = engine._policy.evaluate

        def capture_evaluate(features: dict[str, Any]) -> Any:
            captured_features.append(features.copy())
            return original_evaluate(features)

        with patch.object(engine._policy, "evaluate", side_effect=capture_evaluate):
            engine.process_snapshot(snapshot)

        # Verify mid_price matches snapshot.mid_price
        assert len(captured_features) == 1
        features = captured_features[0]

        # Both base and FeatureSnapshot compute mid_price from snapshot
        # They should be identical, so no "silent" override occurs
        assert features["mid_price"] == snapshot.mid_price
        assert features["mid_price"] == Decimal("50005")
