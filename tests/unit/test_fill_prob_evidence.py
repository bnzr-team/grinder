"""Tests for fill probability gate evidence artifacts (Track C, PR-C6).

Covers:
- Evidence rendering: payload contract (verdict, features, model, action).
- Writer env-gated OFF by default: no files written.
- Writer env-gated ON: JSON + sha256 sidecar written atomically.
- BLOCK always logs, SHADOW logs only when env ON.
- ALLOW never triggers evidence.
- Engine integration: evidence emitted on BLOCK/SHADOW.
- Deterministic format: sort_keys=True, indent=2, trailing newline.
"""

from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide
from grinder.execution.fill_prob_evidence import (
    ARTIFACT_VERSION,
    log_fill_prob_evidence,
    maybe_emit_fill_prob_evidence,
    render_fill_prob_evidence,
    should_write_evidence,
    write_fill_prob_evidence,
)
from grinder.execution.fill_prob_gate import (
    FillProbResult,
    FillProbVerdict,
)
from grinder.execution.sor_metrics import reset_sor_metrics
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import (
    BlockReason,
    LiveActionStatus,
    LiveEngineConfig,
    LiveEngineV0,
)
from grinder.ml.fill_model_loader import extract_online_features
from grinder.ml.fill_model_v0 import FillModelFeaturesV0, FillModelV0

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# --- Helpers ---------------------------------------------------------------


def _make_model(tmp_path: Path, *, bins: dict[str, int] | None = None) -> FillModelV0:
    """Create a FillModelV0 with given bins."""
    if bins is None:
        bins = {"long|0|1|0": 1000, "short|1|2|1": 8000}
    model = FillModelV0(bins=bins, global_prior_bps=5000, n_train_rows=100)
    model_dir = tmp_path / "fill_model_v0"
    model.save(model_dir, force=True, created_at_utc="2025-01-01T00:00:00Z")
    return model


def _make_features(direction: str = "long", notional: float = 50.0) -> FillModelFeaturesV0:
    """Create FillModelFeaturesV0 via extract_online_features."""
    return extract_online_features(direction=direction, notional=notional)


def _block_result() -> FillProbResult:
    """Create a BLOCK FillProbResult."""
    return FillProbResult(
        verdict=FillProbVerdict.BLOCK,
        prob_bps=1000,
        threshold_bps=2500,
        enforce=True,
    )


def _shadow_result() -> FillProbResult:
    """Create a SHADOW FillProbResult."""
    return FillProbResult(
        verdict=FillProbVerdict.SHADOW,
        prob_bps=3000,
        threshold_bps=2500,
        enforce=False,
    )


def _allow_result() -> FillProbResult:
    """Create an ALLOW FillProbResult."""
    return FillProbResult(
        verdict=FillProbVerdict.ALLOW,
        prob_bps=8000,
        threshold_bps=2500,
        enforce=True,
    )


def _action_meta() -> dict[str, str]:
    """Create action metadata dict."""
    return {
        "action_type": "PLACE",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "price": "49000.00",
        "qty": "0.01",
    }


def _make_snapshot(bid: str = "50000.00", ask: str = "50001.00", ts: int = 1000000) -> Snapshot:
    """Create a Snapshot with given bid/ask."""
    return Snapshot(
        ts=ts,
        symbol="BTCUSDT",
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        last_price=Decimal(bid),
        last_qty=Decimal("0.5"),
    )


def _make_paper_engine(actions: list[ExecutionAction]) -> MagicMock:
    """Create a mock PaperEngine returning given actions."""
    engine = MagicMock()
    engine.process_snapshot.return_value = MagicMock(actions=actions)
    return engine


def _place_action(
    price: str = "49000.00", qty: str = "0.01", side: OrderSide = OrderSide.BUY
) -> ExecutionAction:
    """Create a PLACE action."""
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="BTCUSDT",
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        level_id=1,
        reason="GRID_ENTRY",
    )


def _live_config() -> LiveEngineConfig:
    """Create a LiveEngineConfig with all gates open."""
    return LiveEngineConfig(
        armed=True,
        mode=SafeMode.LIVE_TRADE,
        kill_switch_active=False,
        symbol_whitelist=[],
    )


# --- Tests: Render (pure function) ----------------------------------------


class TestRenderEvidence:
    """R-001..R-004: evidence rendering tests."""

    def test_contains_gate_fields(self, tmp_path: Path) -> None:
        """R-001: evidence dict contains all FillProbResult fields."""
        model = _make_model(tmp_path)
        result = _block_result()
        features = _make_features()

        evidence = render_fill_prob_evidence(
            result=result,
            features=features,
            model=model,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        assert evidence["verdict"] == "BLOCK"
        assert evidence["prob_bps"] == 1000
        assert evidence["threshold_bps"] == 2500
        assert evidence["enforce"] is True
        assert evidence["artifact_version"] == ARTIFACT_VERSION
        assert evidence["ts_ms"] == 1234567890

    def test_contains_features(self, tmp_path: Path) -> None:
        """R-002: evidence dict contains features."""
        model = _make_model(tmp_path)
        result = _block_result()
        features = _make_features(direction="long", notional=50.0)

        evidence = render_fill_prob_evidence(
            result=result,
            features=features,
            model=model,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        assert evidence["features"]["direction"] == "long"
        assert isinstance(evidence["features"]["notional_bucket"], int)
        assert isinstance(evidence["features"]["entry_fill_count"], int)
        assert isinstance(evidence["features"]["holding_ms_bucket"], int)

    def test_contains_model_metadata(self, tmp_path: Path) -> None:
        """R-003: evidence dict contains model metadata."""
        model = _make_model(tmp_path)
        result = _block_result()
        features = _make_features()

        evidence = render_fill_prob_evidence(
            result=result,
            features=features,
            model=model,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        assert evidence["model"]["n_bins"] == len(model.bins)
        assert evidence["model"]["n_train_rows"] == 100
        assert evidence["model"]["global_prior_bps"] == 5000

    def test_model_none_uses_null_metadata(self) -> None:
        """R-004: model=None → model metadata fields are None."""
        result = _block_result()
        features = _make_features()

        evidence = render_fill_prob_evidence(
            result=result,
            features=features,
            model=None,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        assert evidence["model"]["n_bins"] is None
        assert evidence["model"]["n_train_rows"] is None
        assert evidence["model"]["global_prior_bps"] is None

    def test_contains_action_metadata(self, tmp_path: Path) -> None:
        """R-005: evidence dict contains action metadata."""
        model = _make_model(tmp_path)
        result = _block_result()
        features = _make_features()

        evidence = render_fill_prob_evidence(
            result=result,
            features=features,
            model=model,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        assert evidence["action"]["symbol"] == "BTCUSDT"
        assert evidence["action"]["side"] == "BUY"
        assert evidence["action"]["price"] == "49000.00"
        assert evidence["action"]["qty"] == "0.01"
        assert evidence["action"]["action_type"] == "PLACE"


# --- Tests: Writer ---------------------------------------------------------


class TestWriter:
    """W-001..W-005: evidence writer tests."""

    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """W-001: no files when GRINDER_FILL_PROB_EVIDENCE unset."""
        monkeypatch.delenv("GRINDER_FILL_PROB_EVIDENCE", raising=False)
        assert should_write_evidence() is False

    def test_enabled_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """W-002: returns True when env var is truthy."""
        monkeypatch.setenv("GRINDER_FILL_PROB_EVIDENCE", "1")
        assert should_write_evidence() is True

    def test_writes_json_and_sha256(self, tmp_path: Path) -> None:
        """W-003: writes JSON + sha256 sidecar, sha matches content."""
        evidence = render_fill_prob_evidence(
            result=_block_result(),
            features=_make_features(),
            model=None,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        json_path, sha_path = write_fill_prob_evidence(
            evidence=evidence,
            out_dir=tmp_path / "fill_prob",
        )

        assert json_path.exists()
        assert sha_path.exists()

        # Verify sha256 matches content
        content = json_path.read_text(encoding="utf-8")
        expected_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        sha_line = sha_path.read_text(encoding="utf-8")
        assert sha_line.startswith(expected_sha)

    def test_deterministic_format(self, tmp_path: Path) -> None:
        """W-004: JSON is sort_keys=True, indent=2, trailing newline."""
        evidence = render_fill_prob_evidence(
            result=_block_result(),
            features=_make_features(),
            model=None,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        json_path, _ = write_fill_prob_evidence(
            evidence=evidence,
            out_dir=tmp_path / "fill_prob",
        )

        content = json_path.read_text(encoding="utf-8")
        assert content.endswith("\n")

        # Re-serialize to verify sort_keys + indent=2
        parsed = json.loads(content)
        expected = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
        assert content == expected

    def test_filename_includes_verdict_and_symbol(self, tmp_path: Path) -> None:
        """W-005: filename is {ts_ms}_{verdict}_{symbol}.json."""
        evidence = render_fill_prob_evidence(
            result=_block_result(),
            features=_make_features(),
            model=None,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        json_path, sha_path = write_fill_prob_evidence(
            evidence=evidence,
            out_dir=tmp_path / "fill_prob",
        )

        assert json_path.name == "1234567890_BLOCK_BTCUSDT.json"
        assert sha_path.name == "1234567890_BLOCK_BTCUSDT.sha256"

    def test_shadow_writes_artifact(self, tmp_path: Path) -> None:
        """W-006: SHADOW verdict also produces artifact."""
        evidence = render_fill_prob_evidence(
            result=_shadow_result(),
            features=_make_features(),
            model=None,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        json_path, _ = write_fill_prob_evidence(
            evidence=evidence,
            out_dir=tmp_path / "fill_prob",
        )

        content = json.loads(json_path.read_text(encoding="utf-8"))
        assert content["verdict"] == "SHADOW"


# --- Tests: Logging --------------------------------------------------------


class TestLogging:
    """L-001..L-003: structured logging tests."""

    def test_log_on_block_always(self, caplog: pytest.LogCaptureFixture) -> None:
        """L-001: BLOCK always logs FILL_PROB_EVIDENCE."""
        evidence = render_fill_prob_evidence(
            result=_block_result(),
            features=_make_features(),
            model=None,
            action_meta=_action_meta(),
            ts_ms=1234567890,
        )

        with caplog.at_level(logging.INFO):
            log_fill_prob_evidence(evidence)

        assert any("FILL_PROB_EVIDENCE" in rec.message for rec in caplog.records)
        assert any("BLOCK" in rec.message for rec in caplog.records)
        assert any("BTCUSDT" in rec.message for rec in caplog.records)

    def test_maybe_emit_block_logs_without_env(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """L-002: BLOCK logs even when evidence env is OFF."""
        monkeypatch.delenv("GRINDER_FILL_PROB_EVIDENCE", raising=False)

        with caplog.at_level(logging.INFO):
            result = maybe_emit_fill_prob_evidence(
                result=_block_result(),
                features=_make_features(),
                model=None,
                action_meta=_action_meta(),
            )

        # No artifact written
        assert result is None
        # But log was emitted
        assert any("FILL_PROB_EVIDENCE" in rec.message for rec in caplog.records)

    def test_maybe_emit_shadow_silent_without_env(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """L-003: SHADOW does NOT log when evidence env is OFF."""
        monkeypatch.delenv("GRINDER_FILL_PROB_EVIDENCE", raising=False)

        with caplog.at_level(logging.INFO):
            result = maybe_emit_fill_prob_evidence(
                result=_shadow_result(),
                features=_make_features(),
                model=None,
                action_meta=_action_meta(),
            )

        assert result is None
        assert not any("FILL_PROB_EVIDENCE" in rec.message for rec in caplog.records)


# --- Tests: maybe_emit integration ----------------------------------------


class TestMaybeEmit:
    """M-001..M-003: maybe_emit integration tests."""

    def test_block_writes_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-001: BLOCK writes artifact when env is ON."""
        monkeypatch.setenv("GRINDER_FILL_PROB_EVIDENCE", "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        result = maybe_emit_fill_prob_evidence(
            result=_block_result(),
            features=_make_features(),
            model=None,
            action_meta=_action_meta(),
        )

        assert result is not None
        json_path, sha_path = result
        assert json_path.exists()
        assert sha_path.exists()
        assert "BLOCK" in json_path.name

    def test_shadow_writes_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-002: SHADOW writes artifact when env is ON."""
        monkeypatch.setenv("GRINDER_FILL_PROB_EVIDENCE", "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        result = maybe_emit_fill_prob_evidence(
            result=_shadow_result(),
            features=_make_features(),
            model=None,
            action_meta=_action_meta(),
        )

        assert result is not None
        json_path, _ = result
        assert json_path.exists()
        assert "SHADOW" in json_path.name

    def test_oserror_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """M-003: OSError on write → returns None, logs warning."""
        monkeypatch.setenv("GRINDER_FILL_PROB_EVIDENCE", "1")
        # Point to a path that can't be written
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", "/proc/nonexistent")

        with caplog.at_level(logging.WARNING):
            result = maybe_emit_fill_prob_evidence(
                result=_block_result(),
                features=_make_features(),
                model=None,
                action_meta=_action_meta(),
            )

        assert result is None
        assert any("Failed to write" in rec.message for rec in caplog.records)


# --- Tests: Engine integration ---------------------------------------------


class TestEngineEvidence:
    """E-001..E-003: evidence wiring in LiveEngineV0."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()

    def test_block_emits_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """E-001: BLOCK via engine emits evidence log."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_ENFORCE", "1")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "5000")
        monkeypatch.delenv("GRINDER_FILL_PROB_EVIDENCE", raising=False)

        # price=49000*qty=0.01 → notional=490 → bucket 1 → key long|1|1|0
        model = _make_model(tmp_path, bins={"long|1|1|0": 1000})
        action = _place_action(price="49000.00", qty="0.01")

        port = MagicMock()
        engine = LiveEngineV0(
            paper_engine=_make_paper_engine([action]),
            exchange_port=port,
            config=_live_config(),
            fill_model=model,
        )

        with caplog.at_level(logging.INFO):
            output = engine.process_snapshot(_make_snapshot())

        assert output.live_actions[0].status == LiveActionStatus.BLOCKED
        assert output.live_actions[0].block_reason == BlockReason.FILL_PROB_LOW
        # Evidence log was emitted
        assert any("FILL_PROB_EVIDENCE" in rec.message for rec in caplog.records)

    def test_block_writes_artifact_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E-002: BLOCK via engine writes artifact when env ON."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_ENFORCE", "1")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "5000")
        monkeypatch.setenv("GRINDER_FILL_PROB_EVIDENCE", "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        model = _make_model(tmp_path, bins={"long|1|1|0": 1000})
        action = _place_action(price="49000.00", qty="0.01")

        port = MagicMock()
        engine = LiveEngineV0(
            paper_engine=_make_paper_engine([action]),
            exchange_port=port,
            config=_live_config(),
            fill_model=model,
        )

        engine.process_snapshot(_make_snapshot())

        # Check that artifact was written
        evidence_dir = tmp_path / "fill_prob"
        json_files = list(evidence_dir.glob("*_BLOCK_*.json"))
        assert len(json_files) == 1

        content = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert content["verdict"] == "BLOCK"
        assert content["action"]["symbol"] == "BTCUSDT"
        assert content["prob_bps"] == 1000

    def test_shadow_no_artifact_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E-003: SHADOW via engine writes no artifact when env OFF."""
        monkeypatch.delenv("GRINDER_FILL_MODEL_ENFORCE", raising=False)
        monkeypatch.delenv("GRINDER_FILL_PROB_EVIDENCE", raising=False)
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))

        model = _make_model(tmp_path, bins={"long|1|1|0": 1000})
        action = _place_action(price="49000.00", qty="0.01")

        port = MagicMock()
        engine = LiveEngineV0(
            paper_engine=_make_paper_engine([action]),
            exchange_port=port,
            config=_live_config(),
            fill_model=model,
        )

        engine.process_snapshot(_make_snapshot())

        # No evidence artifact should exist
        evidence_dir = tmp_path / "fill_prob"
        if evidence_dir.exists():
            assert len(list(evidence_dir.glob("*.json"))) == 0
