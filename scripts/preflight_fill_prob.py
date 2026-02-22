#!/usr/bin/env python3
"""Pre-flight checks for fill probability enforcement rollout (Track C, PR-C8).

Validates all prerequisites BEFORE an operator flips GRINDER_FILL_MODEL_ENFORCE=1.
Pure offline — reads files only, no live connection required.

Checks:
  1. Model directory exists and loads successfully (sha256 verification).
  2. Eval report exists and loads successfully (sha256 verification).
  3. Calibration is well-calibrated (max_error < 500 bps).
  4. Recommended threshold from eval matches GRINDER_FILL_PROB_MIN_BPS (or warns).
  5. Evidence artifacts directory exists with at least one artifact.
  6. Auto-threshold resolution succeeds (optional, --auto-threshold flag, PR-C9).

Usage:
    python3 -m scripts.preflight_fill_prob \\
        --model ml/models/fill_model_v0 \\
        --eval ml/eval/fill_model_v0 \\
        --evidence-dir ml/evidence/fill_prob

    # With explicit threshold override (default: 2500 bps)
    python3 -m scripts.preflight_fill_prob \\
        --model <dir> --eval <dir> --evidence-dir <dir> \\
        --threshold-bps 3000

Exit codes:
    0 - All checks pass, safe to enable enforcement.
    1 - At least one check failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from grinder.ml.threshold_resolver import resolve_threshold


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_model(model_dir: Path) -> tuple[bool, str]:
    """Check 1: model directory exists and loads (sha256 verified)."""
    if not model_dir.is_dir():
        return False, f"Model directory not found: {model_dir}"

    manifest_path = model_dir / "manifest.json"
    if not manifest_path.exists():
        return False, f"Model manifest not found: {manifest_path}"

    model_path = model_dir / "model.json"
    if not model_path.exists():
        return False, f"Model file not found: {model_path}"

    try:
        manifest = json.loads(manifest_path.read_text())
        expected_sha = manifest["sha256"]["model.json"]
        actual_sha = _sha256_file(model_path)
        if actual_sha != expected_sha:
            return (
                False,
                f"Model sha256 mismatch: expected {expected_sha[:16]}..., got {actual_sha[:16]}...",
            )

        model_data = json.loads(model_path.read_text())
        n_bins = len(model_data.get("bins", {}))
        prior = model_data.get("global_prior_bps", "?")
        return True, f"Loaded: {n_bins} bins, global_prior={prior} bps"
    except (KeyError, json.JSONDecodeError) as e:
        return False, f"Model load error: {e}"


def _check_eval(eval_dir: Path) -> tuple[bool, str, dict[str, Any] | None]:
    """Check 2: eval report exists and loads (sha256 verified)."""
    if not eval_dir.is_dir():
        return False, f"Eval directory not found: {eval_dir}", None

    manifest_path = eval_dir / "manifest.json"
    if not manifest_path.exists():
        return False, f"Eval manifest not found: {manifest_path}", None

    report_path = eval_dir / "eval_report.json"
    if not report_path.exists():
        return False, f"Eval report not found: {report_path}", None

    try:
        manifest = json.loads(manifest_path.read_text())
        expected_sha = manifest["sha256"]["eval_report.json"]
        report_content = report_path.read_text(encoding="utf-8")
        actual_sha = hashlib.sha256(report_content.encode("utf-8")).hexdigest()
        if actual_sha != expected_sha:
            return (
                False,
                f"Eval report sha256 mismatch: expected {expected_sha[:16]}..., got {actual_sha[:16]}...",
                None,
            )

        report = json.loads(report_content)
        n_rows = report.get("n_rows", "?")
        return True, f"Loaded: {n_rows} rows evaluated", report
    except (KeyError, json.JSONDecodeError) as e:
        return False, f"Eval report load error: {e}", None


def _check_calibration(report: dict[str, Any]) -> tuple[bool, str]:
    """Check 3: calibration is well-calibrated (max_error < 500 bps)."""
    well_cal = report.get("calibration_well_calibrated")
    max_err = report.get("calibration_max_error_bps", "?")

    if well_cal is True:
        return True, f"Well-calibrated (max_error={max_err} bps < 500 bps)"
    return False, f"NOT well-calibrated (max_error={max_err} bps >= 500 bps)"


def _check_threshold(report: dict[str, Any], configured_bps: int) -> tuple[bool, str]:
    """Check 4: recommended threshold matches configured threshold."""
    recommended = report.get("recommended_threshold_bps")
    if recommended is None:
        return False, "Eval report missing recommended_threshold_bps"

    if recommended == configured_bps:
        return True, f"Recommended={recommended} bps matches configured={configured_bps} bps"

    return False, (
        f"MISMATCH: recommended={recommended} bps, configured={configured_bps} bps. "
        f"Consider updating GRINDER_FILL_PROB_MIN_BPS or re-running evaluation."
    )


def _check_auto_threshold(model_dir: Path, eval_dir: Path) -> tuple[bool, str]:
    """Check 6: auto-threshold resolution succeeds (PR-C9).

    Validates that resolve_threshold() can extract recommended_threshold_bps
    from the eval report with full 3-layer validation (sha256, schema, provenance).
    Only run when --auto-threshold flag is passed.
    """
    resolution = resolve_threshold(eval_dir, model_dir)
    if resolution is None:
        return False, "Auto-threshold resolution failed (check logs for reason)"

    return True, f"Resolved: recommended_threshold_bps={resolution.threshold_bps}"


def _check_evidence(evidence_dir: Path) -> tuple[bool, str]:
    """Check 5: evidence artifacts directory exists with at least one file."""
    if not evidence_dir.is_dir():
        return False, f"Evidence directory not found: {evidence_dir}"

    artifacts = list(evidence_dir.glob("*.json"))
    if not artifacts:
        return False, f"No evidence artifacts found in {evidence_dir} (run shadow mode first)"

    return True, f"Found {len(artifacts)} evidence artifact(s)"


def main() -> int:
    """Run all pre-flight checks."""
    parser = argparse.ArgumentParser(
        description="Pre-flight checks for fill probability enforcement rollout (PR-C8)",
    )
    parser.add_argument(
        "--model",
        type=Path,
        required=True,
        help="Path to FillModelV0 model directory",
    )
    parser.add_argument(
        "--eval",
        type=Path,
        required=True,
        help="Path to evaluation report directory",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="Path to fill probability evidence artifacts directory",
    )
    parser.add_argument(
        "--threshold-bps",
        type=int,
        default=2500,
        help="Configured GRINDER_FILL_PROB_MIN_BPS value (default: 2500)",
    )
    parser.add_argument(
        "--auto-threshold",
        action="store_true",
        default=False,
        help="Check auto-threshold resolution (PR-C9). Validates eval→model provenance.",
    )

    args = parser.parse_args()

    checks: list[tuple[str, bool, str]] = []
    eval_report: dict[str, Any] | None = None

    # Check 1: Model
    ok, msg = _check_model(args.model)
    checks.append(("Model loads", ok, msg))

    # Check 2: Eval report
    ok, msg, eval_report = _check_eval(args.eval)
    checks.append(("Eval report loads", ok, msg))

    # Check 3: Calibration (requires eval report)
    if eval_report is not None:
        ok, msg = _check_calibration(eval_report)
        checks.append(("Calibration", ok, msg))
    else:
        checks.append(("Calibration", False, "Skipped (eval report failed to load)"))

    # Check 4: Threshold match (requires eval report)
    if eval_report is not None:
        ok, msg = _check_threshold(eval_report, args.threshold_bps)
        checks.append(("Threshold match", ok, msg))
    else:
        checks.append(("Threshold match", False, "Skipped (eval report failed to load)"))

    # Check 5: Evidence artifacts
    ok, msg = _check_evidence(args.evidence_dir)
    checks.append(("Evidence artifacts", ok, msg))

    # Check 6: Auto-threshold resolution (optional, PR-C9)
    if args.auto_threshold:
        ok, msg = _check_auto_threshold(args.model, args.eval)
        checks.append(("Auto-threshold resolution", ok, msg))

    # Print results
    print("=" * 60)
    print("Fill Probability Enforcement Pre-flight Checks")
    print("=" * 60)

    all_pass = True
    for name, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}: {detail}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("All checks passed. Safe to set GRINDER_FILL_MODEL_ENFORCE=1.")
        return 0

    print("One or more checks failed. Do NOT enable enforcement until all pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
