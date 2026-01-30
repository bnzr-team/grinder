#!/usr/bin/env python3
"""
Verify replay determinism by running backtest twice and comparing digests.

Usage:
    python -m scripts.verify_replay_determinism
    python -m scripts.verify_replay_determinism --fixture tests/fixtures/sample_day/
"""

import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


def run_replay(fixture_dir: Path, run_id: int) -> str:
    """Run replay and return output digest."""
    print(f"\n--- Replay run #{run_id} ---")

    result = subprocess.run(
        [sys.executable, "-m", "scripts.run_replay", "--fixture", str(fixture_dir), "-v"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
        check=False,
    )

    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"Replay run #{run_id} failed with exit code {result.returncode}")

    # Extract digest from output
    for line in result.stdout.split("\n"):
        if "Output digest:" in line:
            return line.split()[-1]

    # Fallback: compute digest of stdout
    return hashlib.sha256(result.stdout.encode()).hexdigest()[:16]


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify replay determinism")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/sample_day"),
        help="Fixture directory to replay",
    )
    args = parser.parse_args()

    fixture_dir = args.fixture

    if not fixture_dir.exists():
        print(f"Fixture directory not found: {fixture_dir}")
        print("Generating synthetic fixture...")

        fixture_dir = Path(tempfile.mkdtemp())
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.generate_fixture",
                "--symbols",
                "BTCUSDT",
                "--duration-s",
                "2",
                "--out-dir",
                str(fixture_dir),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,
            check=False,
        )

        if result.returncode != 0:
            print(f"Failed to generate fixture: {result.stderr}")
            sys.exit(1)

    print(f"Using fixture: {fixture_dir}")

    # Run replay twice
    try:
        digest1 = run_replay(fixture_dir, 1)
        digest2 = run_replay(fixture_dir, 2)
    except RuntimeError as e:
        print(f"\nERROR: {e}")
        sys.exit(1)

    # Compare digests
    print("\n--- Digest verification ---")
    print(f"Run #1 digest: {digest1}")
    print(f"Run #2 digest: {digest2}")

    if digest1 == digest2:
        print("\nALL DIGESTS MATCH")
        print("DETERMINISM CHECK PASSED")
        sys.exit(0)
    else:
        print("\nDIGEST MISMATCH")
        print("DETERMINISM CHECK FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
