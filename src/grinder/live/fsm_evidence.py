"""Deterministic FSM transition evidence artifacts.

Launch-13 PR5: Side-effect layer that writes evidence bundles on FSM transitions.
SSOT: docs/08_STATE_MACHINE.md (Sec 8.13).

Design:
- Deterministic text format (stable field order, sorted signals, trailing newline).
- Atomic file writes (tmp + os.replace).
- Safe-by-default: no files written unless GRINDER_FSM_EVIDENCE is truthy.
- Zero changes to FSM transition logic (caller-side only).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from grinder.env_parse import parse_bool

if TYPE_CHECKING:
    from grinder.live.fsm_orchestrator import FsmConfig, OrchestratorInputs, TransitionEvent

logger = logging.getLogger(__name__)

ARTIFACT_VERSION = "fsm_evidence_v2"
ENV_ENABLE = "GRINDER_FSM_EVIDENCE"
ENV_ARTIFACT_DIR = "GRINDER_ARTIFACT_DIR"


def should_write_fsm_evidence() -> bool:
    """Check if evidence writing is enabled via env var.

    Safe-by-default: returns False if env var is unset, empty, whitespace,
    or any non-truthy value. Only returns True for explicit truthy values.

    Uses :func:`grinder.env_parse.parse_bool` (SSOT for truthy/falsey).
    """
    return parse_bool(ENV_ENABLE, default=False, strict=False)


def _fmt_value(v: object) -> str:
    """Format a signal value deterministically (Python-style)."""
    if v is None:
        return "None"
    if isinstance(v, bool):
        return "True" if v else "False"
    return str(v)


def render_evidence_text(
    event: TransitionEvent,
    inputs: OrchestratorInputs,
    config: FsmConfig | None = None,  # noqa: ARG001 — kept for caller API compat
) -> str:
    """Render canonical evidence text for a transition event.

    Format is deterministic: fixed field order, sorted signal keys,
    trailing newline. Same event+inputs always produces identical output.

    v2 (PR-A2b): native numeric fields replace v1 bool/str surrogates.
    Breaking changes: feed_stale/toxicity_level removed (PR-A2b),
    drawdown_breached replaced by drawdown_pct (PR-A3).

    The config parameter is accepted for caller API compat but unused in v2
    (signals are emitted as-is, no threshold interpretation needed).
    """
    signals: dict[str, object] = {
        "kill_switch_active": inputs.kill_switch_active,
        "drawdown_pct": inputs.drawdown_pct,
        "feed_gap_ms": inputs.feed_gap_ms,
        "spread_bps": inputs.spread_bps,
        "toxicity_score_bps": inputs.toxicity_score_bps,
        "position_reduced": inputs.position_reduced,
        "operator_override": inputs.operator_override,
    }

    lines: list[str] = [
        f"artifact_version={ARTIFACT_VERSION}",
        f"ts_ms={event.ts_ms}",
        f"from_state={event.from_state.name}",
        f"to_state={event.to_state.name}",
        f"reason={event.reason.value}",
        "signals:",
    ]
    for k in sorted(signals):
        lines.append(f"  {k}={_fmt_value(signals[k])}")

    return "\n".join(lines) + "\n"


def compute_sha256_hex(text: str) -> str:
    """Compute SHA256 hex digest of text encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically: tmp file + os.replace (POSIX atomic on same fs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _sanitize_stem(s: str) -> str:
    """Keep only [A-Za-z0-9_] in stem component; replace others with '_'."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in s)


def write_fsm_evidence_atomic(
    *,
    out_dir: Path,
    event: TransitionEvent,
    inputs: OrchestratorInputs,
    config: FsmConfig | None = None,
) -> tuple[Path, Path]:
    """Write evidence txt + sha256 files atomically.

    Returns:
        (txt_path, sha_path) of the written files.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    from_s = _sanitize_stem(event.from_state.name)
    to_s = _sanitize_stem(event.to_state.name)
    stem = f"fsm_transition_{event.ts_ms}_{from_s}_{to_s}"

    txt_path = out_dir / f"{stem}.txt"
    sha_path = out_dir / f"{stem}.sha256"

    text = render_evidence_text(event, inputs, config=config)
    digest = compute_sha256_hex(text)

    _atomic_write_text(txt_path, text)
    _atomic_write_text(sha_path, f"{digest}  {txt_path.name}\n")

    return txt_path, sha_path


def maybe_emit_transition_evidence(
    event: TransitionEvent,
    inputs: OrchestratorInputs,
    *,
    config: FsmConfig | None = None,
) -> tuple[Path, Path] | None:
    """Emit evidence if enabled, otherwise return None.

    Safe-by-default: if env var is unset/falsey, returns None silently.
    If env is set but write fails, logs warning and returns None.
    """
    if not should_write_fsm_evidence():
        return None

    raw_dir = os.environ.get(ENV_ARTIFACT_DIR, "artifacts")
    out_dir = Path(raw_dir) / "fsm"

    try:
        return write_fsm_evidence_atomic(out_dir=out_dir, event=event, inputs=inputs, config=config)
    except OSError:
        logger.warning(
            "Failed to write FSM evidence artifact",
            extra={"out_dir": str(out_dir), "ts_ms": event.ts_ms},
            exc_info=True,
        )
        return None
