"""Threshold resolver: auto-threshold from eval report (Track C, PR-C9).

Reads ``eval_report.json`` from the evaluation directory, validates
sha256 integrity, schema version, and model provenance, then extracts
``recommended_threshold_bps``.

Two modes (determined by caller):
- **Recommend-only** (default): log resolved threshold, do not override.
- **Auto-apply** (opt-in): override configured threshold.

Design:
- **Fail-open**: any error -> ResolveResult with resolution=None.
  Caller falls back to configured ``GRINDER_FILL_PROB_MIN_BPS``.
- **3-layer validation**: sha256 -> schema_version -> model provenance.
- **Optional freshness check**: opt-in via ``GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS``.
- **No caching**: load once at startup (same pattern as model loader).
- **Pure resolver**: ``resolve_threshold_result()`` does NOT log —
  callers (engine, preflight) own all logging.
- **Evidence artifact**: gated on ``GRINDER_ARTIFACT_DIR`` being set
  (boot-time config artifact, NOT gated on ``GRINDER_FILL_PROB_EVIDENCE``).

SSOT: this module.  ADR-074 / ADR-074a in docs/DECISIONS.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ARTIFACT_VERSION = "threshold_resolution_v1"
EXPECTED_EVAL_SCHEMA = "fill_model_eval_v0"
ENV_ARTIFACT_DIR = "GRINDER_ARTIFACT_DIR"
ENV_EVAL_MAX_AGE_HOURS = "GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS"
TIMESTAMP_FUTURE_TOLERANCE_S = 300  # 5 minutes
KNOWN_TIMESTAMP_KEYS = ("ts_ms", "created_at", "generated_at")


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


@dataclass(frozen=True)
class ResolveResult:
    """Result of threshold resolution (success or fail-open).

    Attributes:
        resolution: ThresholdResolution on success, None on failure.
        reason_code: Stable reason code. ``"ok"`` on success.
        detail: Human-readable detail for triage.
    """

    resolution: ThresholdResolution | None
    reason_code: str
    detail: str


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


def _extract_report_timestamp_s(report: dict[str, Any]) -> float | str | None:
    """Extract timestamp from eval report (if present).

    Checks keys in order: ts_ms, created_at, generated_at.

    Returns:
        float — seconds since epoch (success).
        str — error message (timestamp key found but unparseable).
        None — no timestamp key found.
    """
    for key in KNOWN_TIMESTAMP_KEYS:
        val = report.get(key)
        if val is None:
            continue
        if key == "ts_ms" and isinstance(val, (int, float)):
            return float(val) / 1000.0
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.timestamp()
            except (ValueError, TypeError):
                return f"timestamp key '{key}' unparseable: {val!r}"
        # Key found but wrong type
        return f"timestamp key '{key}' unexpected type: {type(val).__name__}"
    return None


def _fail(reason_code: str, detail: str) -> ResolveResult:
    """Shorthand for a failed ResolveResult."""
    return ResolveResult(resolution=None, reason_code=reason_code, detail=detail)


# --- Core resolver ------------------------------------------------------------


def resolve_threshold_result(  # noqa: PLR0911, PLR0912
    eval_dir: str | Path,
    model_dir: str | Path,
) -> ResolveResult:
    """Resolve recommended threshold from eval report (structured result).

    Pure validation function — does NOT log.  Callers (engine, preflight)
    own all logging using the returned ``ResolveResult``.

    3-layer validation + optional freshness check:
    1. SHA256 integrity of eval_report.json against manifest.
    2. Schema version must be ``fill_model_eval_v0``.
    3. Model provenance: ``model_manifest_sha256`` in eval report must
       match the SHA256 of ``model_dir/manifest.json``.
    4. Freshness (opt-in via ``GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS``).

    Args:
        eval_dir: Directory containing eval_report.json + manifest.json.
        model_dir: Directory containing the model's manifest.json.

    Returns:
        ResolveResult with resolution on success, or reason_code on failure.
    """
    try:
        eval_path = Path(eval_dir)
        model_path = Path(model_dir)

        # --- Layer 1: load eval report + sha256 verification ---
        manifest_file = eval_path / "manifest.json"
        report_file = eval_path / "eval_report.json"

        if not manifest_file.exists():
            return _fail("missing_field", f"eval manifest not found: {manifest_file}")

        if not report_file.exists():
            return _fail("missing_field", f"eval report not found: {report_file}")

        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

        # Explicit manifest field checks
        sha256_block = manifest.get("sha256")
        if not isinstance(sha256_block, dict):
            return _fail("missing_field", "eval manifest missing 'sha256' block")
        expected_sha = sha256_block.get("eval_report.json")
        if not isinstance(expected_sha, str):
            return _fail(
                "missing_field",
                "eval manifest sha256 missing 'eval_report.json' entry",
            )

        report_content = report_file.read_text(encoding="utf-8")
        actual_sha = hashlib.sha256(report_content.encode("utf-8")).hexdigest()

        if actual_sha != expected_sha:
            return _fail(
                "sha256_mismatch",
                f"expected {expected_sha[:16]}..., got {actual_sha[:16]}...",
            )

        report: dict[str, Any] = json.loads(report_content)

        # --- Explicit required field checks ---
        for field_name in (
            "schema_version",
            "recommended_threshold_bps",
            "model_manifest_sha256",
        ):
            if field_name not in report:
                return _fail("missing_field", f"{field_name} not in eval report")

        # --- Layer 2: schema version validation ---
        schema_version = report["schema_version"]
        if schema_version != EXPECTED_EVAL_SCHEMA:
            return _fail(
                "schema_unsupported",
                f"expected {EXPECTED_EVAL_SCHEMA}, got {schema_version}",
            )

        # --- Layer 3: model provenance ---
        eval_model_sha = report["model_manifest_sha256"]
        if not eval_model_sha:
            return _fail(
                "missing_field",
                "model_manifest_sha256 empty in eval report",
            )

        model_manifest_file = model_path / "manifest.json"
        if not model_manifest_file.exists():
            return _fail(
                "missing_field",
                f"model manifest not found: {model_manifest_file}",
            )

        model_manifest_sha = _sha256_file(model_manifest_file)

        if model_manifest_sha != eval_model_sha:
            return _fail(
                "model_provenance_mismatch",
                f"eval_model_sha={eval_model_sha[:16]}..., current={model_manifest_sha[:16]}...",
            )

        # --- Layer 4: freshness check (opt-in) ---
        max_age_str = os.environ.get(ENV_EVAL_MAX_AGE_HOURS, "").strip()
        if max_age_str:
            try:
                max_age_hours = float(max_age_str)
            except ValueError:
                return _fail(
                    "env_invalid",
                    f"GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS={max_age_str!r} is not a valid number",
                )

            if max_age_hours > 0:
                report_ts = _extract_report_timestamp_s(report)
                if isinstance(report_ts, str):
                    # Timestamp key found but unparseable
                    return _fail("parse_error", report_ts)
                if report_ts is not None:
                    now = time.time()
                    if report_ts > now + TIMESTAMP_FUTURE_TOLERANCE_S:
                        delta_h = (report_ts - now) / 3600
                        return _fail(
                            "timestamp_future",
                            f"eval report timestamp is {delta_h:.1f}h in the future",
                        )
                    age_hours = (now - report_ts) / 3600
                    if age_hours > max_age_hours:
                        return _fail(
                            "timestamp_too_old",
                            f"eval report age {age_hours:.1f}h exceeds max {max_age_hours}h",
                        )

        # --- Extract threshold ---
        recommended = report["recommended_threshold_bps"]
        if not isinstance(recommended, int):
            return _fail(
                "bad_type",
                f"recommended_threshold_bps must be int, got {type(recommended).__name__}",
            )

        if recommended < 0 or recommended > 10000:
            return _fail(
                "out_of_range",
                f"recommended_threshold_bps={recommended} not in [0, 10000]",
            )

        resolution = ThresholdResolution(
            threshold_bps=recommended,
            eval_sha256=actual_sha,
            model_manifest_sha256=model_manifest_sha,
            eval_dir=str(eval_path),
        )

        return ResolveResult(
            resolution=resolution,
            reason_code="ok",
            detail=f"threshold_bps={recommended}",
        )

    except Exception as exc:
        return _fail("parse_error", str(exc)[:200])


def resolve_threshold(
    eval_dir: str | Path,
    model_dir: str | Path,
) -> ThresholdResolution | None:
    """Resolve recommended threshold from eval report (backward-compatible).

    Thin wrapper around ``resolve_threshold_result()`` that returns only
    the ``ThresholdResolution`` (or ``None`` on any error).

    Use ``resolve_threshold_result()`` when you need the ``reason_code``.
    """
    return resolve_threshold_result(eval_dir, model_dir).resolution


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
