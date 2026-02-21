"""Tests for account sync evidence artifact writer (Launch-15 PR2).

Validates:
- Env-gated writing (safe-by-default)
- Artifact file creation
- sha256sums integrity
- Deterministic content

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.6)
"""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from grinder.account.contracts import (
    AccountSnapshot,
    OpenOrderSnap,
    PositionSnap,
)
from grinder.account.evidence import (
    ENV_ENABLE,
    should_write_evidence,
    write_evidence_bundle,
)
from grinder.account.syncer import Mismatch

# -- Helpers --


def _pos(symbol: str = "BTCUSDT", side: str = "LONG") -> PositionSnap:
    return PositionSnap(
        symbol=symbol,
        side=side,
        qty=Decimal("1.5"),
        entry_price=Decimal("50000.00"),
        mark_price=Decimal("50100.00"),
        unrealized_pnl=Decimal("150.00"),
        leverage=10,
        ts=1000,
    )


def _order(order_id: str = "ord_1") -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=order_id,
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        price=Decimal("49000.00"),
        qty=Decimal("0.01"),
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=1000,
    )


def _snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        positions=(_pos(),),
        open_orders=(_order(),),
        ts=1000,
        source="test",
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- Tests: Env gate --


class TestEnvGate:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_ENABLE, raising=False)
        assert should_write_evidence() is False

    def test_enabled_truthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("1", "true", "yes", "on", "TRUE", "Yes", " 1 "):
            monkeypatch.setenv(ENV_ENABLE, val)
            assert should_write_evidence() is True, f"Expected True for {val!r}"

    def test_disabled_falsy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("0", "false", "no", "off", "", "random"):
            monkeypatch.setenv(ENV_ENABLE, val)
            assert should_write_evidence() is False, f"Expected False for {val!r}"


# -- Tests: write_evidence_bundle --


class TestWriteBundle:
    def test_returns_none_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_ENABLE, raising=False)
        result = write_evidence_bundle(_snapshot(), [])
        assert result is None

    def test_writes_all_files(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        snap = _snapshot()
        mismatches = [Mismatch(rule="test_rule", detail="test detail")]

        result = write_evidence_bundle(snap, mismatches)

        assert result is not None
        assert result.exists()

        expected_files = [
            "account_snapshot.json",
            "positions.json",
            "open_orders.json",
            "mismatches.json",
            "summary.txt",
            "sha256sums.txt",
        ]
        for f in expected_files:
            assert (result / f).exists(), f"Missing file: {f}"

    def test_snapshot_json_valid(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        snap = _snapshot()
        result = write_evidence_bundle(snap, [])
        assert result is not None

        content = (result / "account_snapshot.json").read_text()
        parsed = json.loads(content)
        assert parsed["ts"] == 1000
        assert parsed["source"] == "test"
        assert len(parsed["positions"]) == 1
        assert len(parsed["open_orders"]) == 1

    def test_positions_json_valid(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        snap = _snapshot()
        result = write_evidence_bundle(snap, [])
        assert result is not None

        content = (result / "positions.json").read_text()
        parsed = json.loads(content)
        assert len(parsed) == 1
        assert parsed[0]["symbol"] == "BTCUSDT"

    def test_mismatches_json_valid(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        mismatches = [
            Mismatch(rule="duplicate_key", detail="dup"),
            Mismatch(rule="negative_qty", detail="neg"),
        ]
        result = write_evidence_bundle(_snapshot(), mismatches)
        assert result is not None

        content = (result / "mismatches.json").read_text()
        parsed = json.loads(content)
        assert len(parsed) == 2
        assert parsed[0]["rule"] == "duplicate_key"

    def test_summary_contains_key_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        mismatches = [Mismatch(rule="orphan_order", detail="ord_x")]
        result = write_evidence_bundle(_snapshot(), mismatches)
        assert result is not None

        summary = (result / "summary.txt").read_text()
        assert "ts: 1000" in summary
        assert "source: test" in summary
        assert "positions: 1" in summary
        assert "open_orders: 1" in summary
        assert "mismatches: 1" in summary
        assert "[orphan_order]" in summary
        assert "snapshot_sha256:" in summary

    def test_sha256sums_valid(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        result = write_evidence_bundle(_snapshot(), [])
        assert result is not None

        sha_content = (result / "sha256sums.txt").read_text()
        for line in sha_content.strip().split("\n"):
            digest, name = line.split("  ", 1)
            file_content = (result / name).read_text()
            expected = _sha256(file_content)
            assert digest == expected, f"sha256 mismatch for {name}"

    def test_empty_mismatches_produces_empty_array(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        result = write_evidence_bundle(_snapshot(), [])
        assert result is not None

        content = (result / "mismatches.json").read_text()
        parsed = json.loads(content)
        assert parsed == []

    def test_evidence_dir_under_account_sync(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        result = write_evidence_bundle(_snapshot(), [])
        assert result is not None

        # Path should be: tmp_path/account_sync/<timestamp>/
        assert result.parent.name == "account_sync"
