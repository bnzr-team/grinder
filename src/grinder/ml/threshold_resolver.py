"""Threshold resolver: auto-threshold from eval report (Track C, PR-C9).

Reads ``eval_report.json`` from the evaluation directory, validates
sha256 integrity, schema version, and model provenance, then extracts
``recommended_threshold_bps``.

Two modes (determined by caller):
- **Recommend-only** (default): log resolved threshold, do not override.
- **Auto-apply** (opt-in): override configured threshold.

Design:
- **Fail-open**: any error -> None + warning log.  Caller falls back to
  configured ``GRINDER_FILL_PROB_MIN_BPS``.
- **3-layer validation**: sha256 -> schema_version -> model provenance.
- **No caching**: load once at startup (same pattern as model loader).
- **Evidence artifact**: gated on ``GRINDER_ARTIFACT_DIR`` being set
  (boot-time config artifact, NOT gated on ``GRINDER_FILL_PROB_EVIDENCE``).

SSOT: this module.  ADR-074 in docs/DECISIONS.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ARTIFACT_VERSION = "threshold_resolution_v1"
EXPECTED_EVAL_SCHEMA = "fill_model_eval_v0"
ENV_ARTIFACT_DIR = "GRINDER_ARTIFACT_DIR"


@dataclass(frozen=True)
class ThresholdResolution:
    """Result of threshold resolution from eval report.

    Attributes:
        threshold_bps: Recommended threshold from eval report (0..10000).
        eval_sha256: SHA256 of eval_report.json content.
        model_manifest_sha256: SHA256 of model manifest.json (provenance).
        eval_dir: Path to eval directory used.
    """

    threshold_bps: int
    eval_sha256: str
    model_manifest_sha256: str
    eval_dir: str


# --- Internal helpers ---------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file (binary read, chunked)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically: tmp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# --- Core resolver ------------------------------------------------------------


def resolve_threshold(  # noqa: PLR0911
    eval_dir: str | Path,
    model_dir: str | Path,
) -> ThresholdResolution | None:
    """Resolve recommended threshold from eval report.

    3-layer validation:
    1. SHA256 integrity of eval_report.json against manifest.
    2. Schema version must be ``fill_model_eval_v0``.
    3. Model provenance: ``model_manifest_sha256`` in eval report must
       match the SHA256 of ``model_dir/manifest.json``.

    Args:
        eval_dir: Directory containing eval_report.json + manifest.json.
        model_dir: Directory containing the model's manifest.json.

    Returns:
        ThresholdResolution on success, None on any error (fail-open).
    """
    try:
        eval_path = Path(eval_dir)
        model_path = Path(model_dir)

        # --- Layer 1: load eval report + sha256 verification ---
        manifest_file = eval_path / "manifest.json"
        report_file = eval_path / "eval_report.json"

        if not manifest_file.exists():
            logger.warning(
                "THRESHOLD_RESOLVE_FAILED reason=eval_manifest_missing path=%s",
                manifest_file,
            )
            return None

        if not report_file.exists():
            logger.warning(
                "THRESHOLD_RESOLVE_FAILED reason=eval_report_missing path=%s",
                report_file,
            )
            return None

        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        expected_sha = manifest["sha256"]["eval_report.json"]

        report_content = report_file.read_text(encoding="utf-8")
        actual_sha = hashlib.sha256(report_content.encode("utf-8")).hexdigest()

        if actual_sha != expected_sha:
            logger.warning(
                "THRESHOLD_RESOLVE_FAILED reason=sha256_mismatch expected=%s actual=%s path=%s",
                expected_sha[:16],
                actual_sha[:16],
                report_file,
            )
            return None

        report: dict[str, Any] = json.loads(report_content)

        # --- Layer 2: schema version validation ---
        schema_version = report.get("schema_version", "")
        if schema_version != EXPECTED_EVAL_SCHEMA:
            logger.warning(
                "THRESHOLD_RESOLVE_FAILED reason=schema_mismatch expected=%s got=%s",
                EXPECTED_EVAL_SCHEMA,
                schema_version,
            )
            return None

        # --- Layer 3: model provenance ---
        eval_model_sha = report.get("model_manifest_sha256", "")
        if not eval_model_sha:
            logger.warning(
                "THRESHOLD_RESOLVE_FAILED reason=missing_model_manifest_sha256",
            )
            return None

        model_manifest_file = model_path / "manifest.json"
        if not model_manifest_file.exists():
            logger.warning(
                "THRESHOLD_RESOLVE_FAILED reason=model_manifest_missing path=%s",
                model_manifest_file,
            )
            return None

        model_manifest_sha = _sha256_file(model_manifest_file)

        if model_manifest_sha != eval_model_sha:
            logger.warning(
                "THRESHOLD_RESOLVE_FAILED reason=model_version_mismatch "
                "eval_model_sha=%s current_model_sha=%s",
                eval_model_sha[:16],
                model_manifest_sha[:16],
            )
            return None

        # --- Extract threshold ---
        recommended = report.get("recommended_threshold_bps")
        if not isinstance(recommended, int):
            logger.warning(
                "THRESHOLD_RESOLVE_FAILED reason=missing_recommended_threshold",
            )
            return None

        if recommended < 0 or recommended > 10000:
            logger.warning(
                "THRESHOLD_RESOLVE_FAILED reason=threshold_out_of_range value=%d",
                recommended,
            )
            return None

        resolution = ThresholdResolution(
            threshold_bps=recommended,
            eval_sha256=actual_sha,
            model_manifest_sha256=model_manifest_sha,
            eval_dir=str(eval_path),
        )

        logger.info(
            "THRESHOLD_RESOLVED threshold_bps=%d eval_sha256=%s "
            "model_manifest_sha256=%s eval_dir=%s",
            resolution.threshold_bps,
            resolution.eval_sha256[:16],
            resolution.model_manifest_sha256[:16],
            resolution.eval_dir,
        )

        return resolution

    except Exception:
        logger.warning(
            "THRESHOLD_RESOLVE_FAILED reason=unexpected_error",
            exc_info=True,
        )
        return None


# --- Evidence artifact --------------------------------------------------------


def write_threshold_resolution_evidence(
    *,
    resolution: ThresholdResolution,
    configured_bps: int,
    mode: str,
    effective_bps: int,
    out_dir: Path | None = None,
) -> tuple[Path, Path] | None:
    """Write threshold resolution evidence artifact.

    Gated on ``GRINDER_ARTIFACT_DIR`` being set (NOT ``GRINDER_FILL_PROB_EVIDENCE``).
    This is a boot-time config artifact, not a runtime event artifact.

    Args:
        resolution: ThresholdResolution from resolve_threshold().
        configured_bps: Originally configured GRINDER_FILL_PROB_MIN_BPS.
        mode: ``"recommend_only"`` or ``"auto_apply"``.
        effective_bps: The threshold that will actually be used.
        out_dir: Override output directory (for testing).

    Returns:
        (json_path, sha_path) on success, None if GRINDER_ARTIFACT_DIR
        unset or on write error.
    """
    if out_dir is None:
        artifact_dir = os.environ.get(ENV_ARTIFACT_DIR, "").strip()
        if not artifact_dir:
            return None
        out_dir = Path(artifact_dir) / "fill_prob"

    try:
        ts_ms = int(time.time() * 1000)
        evidence: dict[str, Any] = {
            "artifact_version": ARTIFACT_VERSION,
            "ts_ms": ts_ms,
            "mode": mode,
            "recommended_bps": resolution.threshold_bps,
            "configured_bps": configured_bps,
            "effective_bps": effective_bps,
            "eval_sha256": resolution.eval_sha256,
            "model_manifest_sha256": resolution.model_manifest_sha256,
            "eval_dir": resolution.eval_dir,
        }

        stem = f"threshold_resolution_{ts_ms}"
        json_path = out_dir / f"{stem}.json"
        sha_path = out_dir / f"{stem}.sha256"

        content = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

        _atomic_write_text(json_path, content)
        _atomic_write_text(sha_path, f"{digest}  {json_path.name}\n")

        return json_path, sha_path

    except OSError:
        logger.warning(
            "Failed to write threshold resolution evidence artifact",
            exc_info=True,
        )
        return None
