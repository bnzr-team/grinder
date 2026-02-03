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

from grinder.contracts import Snapshot
from grinder.paper import PaperEngine, PaperOutput, PaperResult

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
