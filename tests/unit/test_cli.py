"""Tests for CLI and replay functionality.

Tests:
- CLI --help works (exit code 0)
- CLI replay command works with valid fixture
- CLI replay produces deterministic digest
- CLI replay validation errors
- ReplayEngine determinism
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from grinder.replay import ReplayEngine

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "sample_day"
EXPECTED_DIGEST = "453ebd0f655e4920"


class TestCLIHelp:
    """Test CLI help functionality."""

    def test_cli_help_exits_zero(self) -> None:
        """Test that grinder --help exits with code 0."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.argv=['grinder', '--help']; from grinder.cli import main; main()",
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
            env={"PYTHONPATH": "src"},
            check=False,
        )
        assert result.returncode == 0
        assert "GRINDER CLI" in result.stdout

    def test_cli_replay_help_exits_zero(self) -> None:
        """Test that grinder replay --help exits with code 0."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.argv=['grinder', 'replay', '--help']; from grinder.cli import main; main()",
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
            env={"PYTHONPATH": "src"},
            check=False,
        )
        assert result.returncode == 0
        assert "--fixture" in result.stdout


class TestCLIReplay:
    """Test CLI replay command."""

    def test_replay_valid_fixture(self) -> None:
        """Test replay with valid fixture produces expected digest."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.run_replay", "--fixture", str(FIXTURE_DIR)],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
            env={"PYTHONPATH": "src"},
            check=False,
        )
        assert result.returncode == 0
        assert f"Output digest: {EXPECTED_DIGEST}" in result.stdout

    def test_replay_missing_fixture_exits_nonzero(self) -> None:
        """Test replay with missing fixture exits with error."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.run_replay", "--fixture", "/nonexistent/fixture"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
            env={"PYTHONPATH": "src"},
            check=False,
        )
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_replay_determinism(self) -> None:
        """Test replay produces same digest on multiple runs."""
        digests = []
        for _ in range(3):
            result = subprocess.run(
                [sys.executable, "-m", "scripts.run_replay", "--fixture", str(FIXTURE_DIR)],
                capture_output=True,
                text=True,
                cwd=Path(__file__).parent.parent.parent,
                env={"PYTHONPATH": "src"},
                check=False,
            )
            assert result.returncode == 0
            # Extract digest from output
            for line in result.stdout.splitlines():
                if "Output digest:" in line:
                    digests.append(line.split()[-1])
                    break

        assert len(digests) == 3
        assert all(d == digests[0] for d in digests), f"Digests differ: {digests}"


class TestReplayEngine:
    """Test ReplayEngine directly."""

    def test_engine_run_fixture(self) -> None:
        """Test ReplayEngine runs fixture successfully."""
        engine = ReplayEngine()
        result = engine.run(FIXTURE_DIR)

        assert result.events_processed == 5
        assert len(result.outputs) == 5
        assert not result.errors
        assert result.digest == EXPECTED_DIGEST

    def test_engine_determinism(self) -> None:
        """Test ReplayEngine produces same digest on multiple runs."""
        digests = []
        for _ in range(3):
            engine = ReplayEngine()
            result = engine.run(FIXTURE_DIR)
            digests.append(result.digest)

        assert all(d == digests[0] for d in digests), f"Digests differ: {digests}"

    def test_engine_outputs_structure(self) -> None:
        """Test ReplayEngine outputs have correct structure."""
        engine = ReplayEngine()
        result = engine.run(FIXTURE_DIR)

        for output in result.outputs:
            assert output.ts > 0
            assert output.symbol in ("BTCUSDT", "ETHUSDT")
            assert "allowed" in output.prefilter_result
            assert "reason" in output.prefilter_result
            # All should pass prefilter with sufficient volume
            assert output.prefilter_result["allowed"] is True
            # Should have plan when allowed
            assert output.plan is not None
            assert "mode" in output.plan
            assert "center_price" in output.plan

    def test_engine_missing_fixture(self) -> None:
        """Test ReplayEngine handles missing fixture gracefully."""
        engine = ReplayEngine()
        result = engine.run(Path("/nonexistent/fixture"))

        assert result.events_processed == 0
        assert len(result.outputs) == 0
        assert len(result.errors) >= 1
        assert "No events found" in result.errors[0]


class TestVerifyReplayDeterminism:
    """Test verify_replay_determinism script."""

    def test_verify_replay_passes(self) -> None:
        """Test verify_replay_determinism passes for valid fixture."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.verify_replay_determinism",
                "--fixture",
                str(FIXTURE_DIR),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
            env={"PYTHONPATH": "src"},
            check=False,
        )
        assert result.returncode == 0
        assert "DETERMINISM CHECK PASSED" in result.stdout
