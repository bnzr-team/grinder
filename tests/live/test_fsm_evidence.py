"""Tests for FSM evidence artifacts (Launch-13 PR5).

Validates:
- render_evidence_text() determinism (sorted signals, stable format, trailing newline)
- compute_sha256_hex() matches known golden value
- should_write_fsm_evidence() env gate (truthy/falsey/unset)
- write_fsm_evidence_atomic() creates txt + sha256, no tmp leftovers
- Idempotent overwrite produces same content
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from grinder.core import SystemState
from grinder.live.fsm_evidence import (
    ARTIFACT_VERSION,
    compute_sha256_hex,
    render_evidence_text,
    should_write_fsm_evidence,
    write_fsm_evidence_atomic,
)
from grinder.live.fsm_orchestrator import OrchestratorInputs, TransitionEvent, TransitionReason

# Canonical golden text for regression testing.
# v2 (PR-A2b): native numeric fields replace v1 bool/str surrogates.
# Breaking change: feed_stale/toxicity_level removed, replaced by
# feed_gap_ms/spread_bps/toxicity_score_bps.
CANON_TEXT = (
    "artifact_version=fsm_evidence_v2\n"
    "ts_ms=2000\n"
    "from_state=ACTIVE\n"
    "to_state=EMERGENCY\n"
    "reason=KILL_SWITCH\n"
    "signals:\n"
    "  drawdown_breached=False\n"
    "  feed_gap_ms=0\n"
    "  kill_switch_active=True\n"
    "  operator_override=None\n"
    "  position_reduced=False\n"
    "  spread_bps=0.0\n"
    "  toxicity_score_bps=0.0\n"
)

CANON_SHA256 = "f733bb65b6cf2bdc0a7dc8864bd87c38c847529a8dcd49d5d9a9be0cc1777de7"


def _mk_event(
    ts_ms: int = 2000,
    from_state: SystemState = SystemState.ACTIVE,
    to_state: SystemState = SystemState.EMERGENCY,
    reason: TransitionReason = TransitionReason.KILL_SWITCH,
) -> TransitionEvent:
    return TransitionEvent(
        ts_ms=ts_ms,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
    )


def _mk_inputs(
    ts_ms: int = 2000,
    kill_switch_active: bool = True,
    operator_override: str | None = None,
) -> OrchestratorInputs:
    return OrchestratorInputs(
        ts_ms=ts_ms,
        kill_switch_active=kill_switch_active,
        drawdown_breached=False,
        feed_gap_ms=0,
        spread_bps=0.0,
        toxicity_score_bps=0.0,
        position_reduced=False,
        operator_override=operator_override,
    )


# ===========================================================================
# 1. render_evidence_text() determinism
# ===========================================================================


class TestRenderEvidenceText:
    """Evidence text is canonical, sorted, and has trailing newline."""

    def test_canonical_output_matches_golden(self) -> None:
        event = _mk_event()
        inputs = _mk_inputs()
        text = render_evidence_text(event, inputs)
        assert text == CANON_TEXT

    def test_trailing_newline(self) -> None:
        event = _mk_event()
        inputs = _mk_inputs()
        text = render_evidence_text(event, inputs)
        assert text.endswith("\n")

    def test_signals_sorted_regardless_of_input_order(self) -> None:
        """OrchestratorInputs fields have fixed order, but signals extraction
        is sorted by key name — verify by checking line ordering."""
        event = _mk_event()
        inputs = _mk_inputs()
        text = render_evidence_text(event, inputs)
        signal_lines = [ln for ln in text.splitlines() if ln.startswith("  ")]
        assert signal_lines == sorted(signal_lines)

    def test_none_operator_override_renders_as_none(self) -> None:
        event = _mk_event()
        inputs = _mk_inputs(operator_override=None)
        text = render_evidence_text(event, inputs)
        assert "  operator_override=None\n" in text

    def test_string_operator_override_renders_as_string(self) -> None:
        event = _mk_event(
            from_state=SystemState.ACTIVE,
            to_state=SystemState.PAUSED,
            reason=TransitionReason.OPERATOR_PAUSE,
        )
        inputs = _mk_inputs(kill_switch_active=False, operator_override="PAUSE")
        text = render_evidence_text(event, inputs)
        assert "  operator_override=PAUSE\n" in text

    def test_artifact_version_present(self) -> None:
        event = _mk_event()
        inputs = _mk_inputs()
        text = render_evidence_text(event, inputs)
        assert text.startswith(f"artifact_version={ARTIFACT_VERSION}\n")


# ===========================================================================
# 2. compute_sha256_hex()
# ===========================================================================


class TestComputeSha256:
    """SHA256 hex digest matches known golden value."""

    def test_canonical_sha256_matches(self) -> None:
        assert compute_sha256_hex(CANON_TEXT) == CANON_SHA256

    def test_different_input_different_hash(self) -> None:
        assert compute_sha256_hex("different text\n") != CANON_SHA256


# ===========================================================================
# 3. should_write_fsm_evidence() env gate
# ===========================================================================


class TestShouldWriteEvidence:
    """Env-based evidence gate: truthy/falsey/unset."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, False),
            ("", False),
            ("   ", False),
            ("0", False),
            ("false", False),
            ("FALSE", False),
            ("no", False),
            ("off", False),
            ("1", True),
            ("true", True),
            ("TRUE", True),
            (" yes ", True),
            ("on", True),
            ("ON", True),
            ("weird", False),  # unknown -> safe default
            ("maybe", False),
        ],
    )
    def test_env_gate(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
    ) -> None:
        monkeypatch.delenv("GRINDER_FSM_EVIDENCE", raising=False)
        if raw is not None:
            monkeypatch.setenv("GRINDER_FSM_EVIDENCE", raw)
        assert should_write_fsm_evidence() is expected


# ===========================================================================
# 4. write_fsm_evidence_atomic()
# ===========================================================================


class TestWriteEvidenceAtomic:
    """Atomic write creates txt + sha256 files correctly."""

    def test_creates_txt_and_sha256(self, tmp_path: Path) -> None:
        event = _mk_event()
        inputs = _mk_inputs()
        txt_path, sha_path = write_fsm_evidence_atomic(out_dir=tmp_path, event=event, inputs=inputs)

        assert txt_path.exists()
        assert sha_path.exists()
        assert txt_path.suffix == ".txt"
        assert sha_path.suffix == ".sha256"

    def test_txt_content_matches_render(self, tmp_path: Path) -> None:
        event = _mk_event()
        inputs = _mk_inputs()
        txt_path, _ = write_fsm_evidence_atomic(out_dir=tmp_path, event=event, inputs=inputs)

        txt = txt_path.read_text(encoding="utf-8")
        assert txt == CANON_TEXT

    def test_sha256_content_matches(self, tmp_path: Path) -> None:
        event = _mk_event()
        inputs = _mk_inputs()
        txt_path, sha_path = write_fsm_evidence_atomic(out_dir=tmp_path, event=event, inputs=inputs)

        txt = txt_path.read_text(encoding="utf-8")
        sha = sha_path.read_text(encoding="utf-8")
        digest = compute_sha256_hex(txt)
        assert sha == f"{digest}  {txt_path.name}\n"

    def test_filename_contains_ts_and_states(self, tmp_path: Path) -> None:
        event = _mk_event(ts_ms=12345)
        inputs = _mk_inputs(ts_ms=12345)
        txt_path, _sha_path = write_fsm_evidence_atomic(
            out_dir=tmp_path, event=event, inputs=inputs
        )

        assert "12345" in txt_path.name
        assert "ACTIVE" in txt_path.name
        assert "EMERGENCY" in txt_path.name

    def test_no_tmp_leftovers(self, tmp_path: Path) -> None:
        event = _mk_event()
        inputs = _mk_inputs()
        write_fsm_evidence_atomic(out_dir=tmp_path, event=event, inputs=inputs)

        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_idempotent_overwrite(self, tmp_path: Path) -> None:
        event = _mk_event()
        inputs = _mk_inputs()

        p1_txt, p1_sha = write_fsm_evidence_atomic(out_dir=tmp_path, event=event, inputs=inputs)
        p2_txt, p2_sha = write_fsm_evidence_atomic(out_dir=tmp_path, event=event, inputs=inputs)

        assert p1_txt == p2_txt
        assert p1_sha == p2_sha
        assert p1_txt.read_text(encoding="utf-8") == CANON_TEXT

    def test_creates_out_dir_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "fsm"
        event = _mk_event()
        inputs = _mk_inputs()
        txt_path, sha_path = write_fsm_evidence_atomic(out_dir=nested, event=event, inputs=inputs)

        assert txt_path.exists()
        assert sha_path.exists()
