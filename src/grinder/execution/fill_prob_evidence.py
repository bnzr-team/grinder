"""Fill probability gate evidence artifacts (Track C, PR-C6).

Env-gated evidence writer for BLOCK/SHADOW events from the fill
probability gate.  Produces JSON + sha256 sidecar for post-hoc triage.

Design:
- Safe-by-default: no files written unless GRINDER_FILL_PROB_EVIDENCE is truthy.
- Atomic file writes (tmp + os.replace).
- Deterministic JSON (sort_keys=True, indent=2, trailing newline).
- Zero changes to gate logic (caller-side only).
- Structured log always emitted on BLOCK (not env-gated).

SSOT: this module.  ADR-071 in docs/DECISIONS.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from grinder.env_parse import parse_bool

if TYPE_CHECKING:
    from grinder.execution.fill_prob_gate import FillProbResult
    from grinder.ml.fill_model_v0 import FillModelFeaturesV0, FillModelV0

logger = logging.getLogger(__name__)

ARTIFACT_VERSION = "fill_prob_evidence_v1"
ENV_ENABLE = "GRINDER_FILL_PROB_EVIDENCE"
ENV_ARTIFACT_DIR = "GRINDER_ARTIFACT_DIR"


def should_write_evidence() -> bool:
    """Check if evidence writing is enabled via env var.

    Safe-by-default: returns False if env var is unset, empty, or falsey.
    Uses :func:`grinder.env_parse.parse_bool` (SSOT for truthy/falsey).
    """
    return parse_bool(ENV_ENABLE, default=False, strict=False)


def render_fill_prob_evidence(
    *,
    result: FillProbResult,
    features: FillModelFeaturesV0,
    model: FillModelV0 | None,
    action_meta: dict[str, Any],
    ts_ms: int,
) -> dict[str, Any]:
    """Render evidence payload as a JSON-serializable dict.

    Pure function — no I/O, no side effects.

    Args:
        result: FillProbResult from check_fill_prob().
        features: FillModelFeaturesV0 used for prediction.
        model: FillModelV0 instance, or None if unavailable.
        action_meta: Action metadata (symbol, side, price, qty, action_type).
        ts_ms: Timestamp in milliseconds.

    Returns:
        Deterministic dict ready for JSON serialization.
    """
    model_meta: dict[str, Any]
    if model is not None:
        model_meta = {
            "global_prior_bps": model.global_prior_bps,
            "n_bins": len(model.bins),
            "n_train_rows": model.n_train_rows,
        }
    else:
        model_meta = {
            "global_prior_bps": None,
            "n_bins": None,
            "n_train_rows": None,
        }

    return {
        "artifact_version": ARTIFACT_VERSION,
        "ts_ms": ts_ms,
        "verdict": result.verdict.value,
        "prob_bps": result.prob_bps,
        "threshold_bps": result.threshold_bps,
        "enforce": result.enforce,
        "features": dict(features),
        "action": action_meta,
        "model": model_meta,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically: tmp file + os.replace (POSIX atomic on same fs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def write_fill_prob_evidence(
    *,
    evidence: dict[str, Any],
    out_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Write evidence JSON + sha256 sidecar atomically.

    Args:
        evidence: Rendered evidence dict from render_fill_prob_evidence().
        out_dir: Output directory. Defaults to {GRINDER_ARTIFACT_DIR}/fill_prob.

    Returns:
        (json_path, sha_path) of the written files.
    """
    if out_dir is None:
        raw_dir = os.environ.get(ENV_ARTIFACT_DIR, "artifacts")
        out_dir = Path(raw_dir) / "fill_prob"

    out_dir.mkdir(parents=True, exist_ok=True)

    ts_ms = evidence["ts_ms"]
    verdict = evidence["verdict"]
    symbol = evidence.get("action", {}).get("symbol", "UNKNOWN")
    stem = f"{ts_ms}_{verdict}_{symbol}"

    json_path = out_dir / f"{stem}.json"
    sha_path = out_dir / f"{stem}.sha256"

    content = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

    _atomic_write_text(json_path, content)
    _atomic_write_text(sha_path, f"{digest}  {json_path.name}\n")

    return json_path, sha_path


def log_fill_prob_evidence(evidence: dict[str, Any]) -> None:
    """Emit structured log for a fill probability gate event.

    Always called on BLOCK (not env-gated).
    Called on SHADOW only when evidence writing is enabled.
    """
    logger.info(
        "FILL_PROB_EVIDENCE verdict=%s prob_bps=%d threshold_bps=%d enforce=%s symbol=%s action=%s",
        evidence["verdict"],
        evidence["prob_bps"],
        evidence["threshold_bps"],
        evidence["enforce"],
        evidence.get("action", {}).get("symbol", "UNKNOWN"),
        evidence.get("action", {}).get("action_type", "UNKNOWN"),
        extra={"fill_prob_evidence": evidence},
    )


def maybe_emit_fill_prob_evidence(
    *,
    result: FillProbResult,
    features: FillModelFeaturesV0,
    model: FillModelV0 | None,
    action_meta: dict[str, Any],
) -> tuple[Path, Path] | None:
    """Emit evidence if enabled, otherwise return None.

    Behavior:
    - BLOCK: always logs (structured), writes artifact only if env-gated ON.
    - SHADOW: logs + writes artifact only if env-gated ON.
    - ALLOW: never called (caller filters).

    Safe-by-default: if env var is unset/falsey, only BLOCK log is emitted.
    If write fails, logs warning and returns None.
    """
    ts_ms = int(time.time() * 1000)

    evidence = render_fill_prob_evidence(
        result=result,
        features=features,
        model=model,
        action_meta=action_meta,
        ts_ms=ts_ms,
    )

    is_block = result.verdict.value == "BLOCK"
    write_enabled = should_write_evidence()

    # Structured log: always on BLOCK, only on SHADOW if env-gated ON
    if is_block or write_enabled:
        log_fill_prob_evidence(evidence)

    # Artifact write: only if env-gated ON
    if not write_enabled:
        return None

    try:
        return write_fill_prob_evidence(evidence=evidence)
    except OSError:
        logger.warning(
            "Failed to write fill prob evidence artifact",
            extra={"ts_ms": ts_ms, "verdict": result.verdict.value},
            exc_info=True,
        )
        return None
