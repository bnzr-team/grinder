"""Integration test: verify_dataset against committed fixture.

M8-04a: End-to-end test using the tiny_valid fixture under tests/testdata/.
"""

from __future__ import annotations

from pathlib import Path

from scripts.verify_dataset import verify_dataset

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "testdata" / "datasets" / "tiny_valid"
FIXTURE_MANIFEST = FIXTURE_DIR / "manifest.json"


class TestFixtureRoundtrip:
    """Verify the committed tiny_valid fixture passes all checks."""

    def test_fixture_exists(self) -> None:
        assert FIXTURE_DIR.exists(), f"Fixture dir missing: {FIXTURE_DIR}"
        assert FIXTURE_MANIFEST.exists(), f"Manifest missing: {FIXTURE_MANIFEST}"
        assert (FIXTURE_DIR / "data.bin").exists(), "data.bin missing"

    def test_verify_passes(self) -> None:
        errors = verify_dataset(FIXTURE_MANIFEST, base_dir=FIXTURE_DIR.parent)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_verify_verbose_passes(self) -> None:
        errors = verify_dataset(FIXTURE_MANIFEST, base_dir=FIXTURE_DIR.parent, verbose=True)
        assert errors == []
