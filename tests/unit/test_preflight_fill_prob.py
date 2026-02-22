"""Tests for preflight_fill_prob.py (Track C, PR-C8).

Covers:
- Model check: pass on valid model, fail on missing/corrupt.
- Eval check: pass on valid eval report, fail on missing/corrupt.
- Calibration check: pass on well-calibrated, fail on not.
- Threshold check: pass on match, fail on mismatch.
- Evidence check: pass when artifacts exist, fail when missing.
- Full preflight: exit 0 on all pass, exit 1 on any fail.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from scripts.preflight_fill_prob import (
    _check_auto_threshold,
    _check_calibration,
    _check_eval,
    _check_evidence,
    _check_model,
    _check_threshold,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- Helpers ---------------------------------------------------------------


def _write_model(model_dir: Path) -> None:
    """Write a valid model directory (model.json + manifest.json)."""
    model_dir.mkdir(parents=True, exist_ok=True)

    model_data = {
        "schema_version": "fill_model_v0",
        "bins": {"long|0|1|0": 6000},
        "global_prior_bps": 5000,
        "n_train_rows": 10,
        "bucket_thresholds": {
            "notional": [100, 500, 1000, 5000],
            "holding_ms": [1000, 10000, 60000, 300000],
            "max_fill_count": 3,
        },
    }
    model_content = json.dumps(model_data, indent=2, sort_keys=True) + "\n"
    (model_dir / "model.json").write_text(model_content)

    model_sha = hashlib.sha256(model_content.encode()).hexdigest()
    manifest = {
        "schema_version": "fill_model_v0",
        "sha256": {"model.json": model_sha},
    }
    (model_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _write_eval(eval_dir: Path, *, well_calibrated: bool = True, threshold: int = 2500) -> None:
    """Write a valid eval report directory."""
    eval_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "schema_version": "fill_model_eval_v0",
        "n_rows": 100,
        "calibration_well_calibrated": well_calibrated,
        "calibration_max_error_bps": 200 if well_calibrated else 800,
        "recommended_threshold_bps": threshold,
    }
    report_content = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (eval_dir / "eval_report.json").write_text(report_content, encoding="utf-8")

    report_sha = hashlib.sha256(report_content.encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": "fill_model_eval_v0",
        "sha256": {"eval_report.json": report_sha},
    }
    (eval_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


# --- Tests: Model check ---------------------------------------------------


class TestCheckModel:
    """PM-001..PM-004: model validation."""

    def test_valid_model_passes(self, tmp_path: Path) -> None:
        """PM-001: valid model directory → PASS."""
        model_dir = tmp_path / "model"
        _write_model(model_dir)
        ok, msg = _check_model(model_dir)
        assert ok
        assert "Loaded" in msg

    def test_missing_dir_fails(self, tmp_path: Path) -> None:
        """PM-002: missing directory → FAIL."""
        ok, msg = _check_model(tmp_path / "nonexistent")
        assert not ok
        assert "not found" in msg

    def test_missing_manifest_fails(self, tmp_path: Path) -> None:
        """PM-003: missing manifest.json → FAIL."""
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "model.json").write_text("{}")
        ok, msg = _check_model(model_dir)
        assert not ok
        assert "manifest" in msg.lower()

    def test_sha256_mismatch_fails(self, tmp_path: Path) -> None:
        """PM-004: sha256 mismatch → FAIL."""
        model_dir = tmp_path / "model"
        _write_model(model_dir)
        # Tamper with model.json
        (model_dir / "model.json").write_text('{"tampered": true}')
        ok, msg = _check_model(model_dir)
        assert not ok
        assert "mismatch" in msg.lower()


# --- Tests: Eval check ----------------------------------------------------


class TestCheckEval:
    """PE-001..PE-004: eval report validation."""

    def test_valid_eval_passes(self, tmp_path: Path) -> None:
        """PE-001: valid eval report → PASS."""
        eval_dir = tmp_path / "eval"
        _write_eval(eval_dir)
        ok, _msg, report = _check_eval(eval_dir)
        assert ok
        assert report is not None
        assert report["n_rows"] == 100

    def test_missing_dir_fails(self, tmp_path: Path) -> None:
        """PE-002: missing directory → FAIL."""
        ok, _msg, report = _check_eval(tmp_path / "nonexistent")
        assert not ok
        assert report is None

    def test_missing_report_fails(self, tmp_path: Path) -> None:
        """PE-003: missing eval_report.json → FAIL."""
        eval_dir = tmp_path / "eval"
        eval_dir.mkdir()
        (eval_dir / "manifest.json").write_text('{"sha256": {}}')
        ok, _msg, report = _check_eval(eval_dir)
        assert not ok
        assert report is None

    def test_sha256_mismatch_fails(self, tmp_path: Path) -> None:
        """PE-004: sha256 mismatch → FAIL."""
        eval_dir = tmp_path / "eval"
        _write_eval(eval_dir)
        (eval_dir / "eval_report.json").write_text('{"tampered": true}')
        ok, _msg, report = _check_eval(eval_dir)
        assert not ok
        assert report is None


# --- Tests: Calibration check ---------------------------------------------


class TestCheckCalibration:
    """PC-001..PC-002: calibration validation."""

    def test_well_calibrated_passes(self) -> None:
        """PC-001: well_calibrated=True → PASS."""
        report = {"calibration_well_calibrated": True, "calibration_max_error_bps": 200}
        ok, msg = _check_calibration(report)
        assert ok
        assert "Well-calibrated" in msg

    def test_not_calibrated_fails(self) -> None:
        """PC-002: well_calibrated=False → FAIL."""
        report = {"calibration_well_calibrated": False, "calibration_max_error_bps": 800}
        ok, msg = _check_calibration(report)
        assert not ok
        assert "NOT" in msg


# --- Tests: Threshold check -----------------------------------------------


class TestCheckThreshold:
    """PT-001..PT-003: threshold match validation."""

    def test_matching_threshold_passes(self) -> None:
        """PT-001: recommended == configured → PASS."""
        report = {"recommended_threshold_bps": 2500}
        ok, msg = _check_threshold(report, 2500)
        assert ok
        assert "matches" in msg

    def test_mismatching_threshold_fails(self) -> None:
        """PT-002: recommended != configured → FAIL."""
        report = {"recommended_threshold_bps": 3000}
        ok, msg = _check_threshold(report, 2500)
        assert not ok
        assert "MISMATCH" in msg

    def test_missing_threshold_fails(self) -> None:
        """PT-003: missing recommended_threshold_bps → FAIL."""
        report: dict[str, Any] = {}
        ok, msg = _check_threshold(report, 2500)
        assert not ok
        assert "missing" in msg.lower()


# --- Tests: Evidence check ------------------------------------------------


class TestCheckEvidence:
    """PEV-001..PEV-003: evidence artifacts validation."""

    def test_evidence_exists_passes(self, tmp_path: Path) -> None:
        """PEV-001: evidence directory with artifacts → PASS."""
        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        (evidence_dir / "artifact_001.json").write_text("{}")
        (evidence_dir / "artifact_002.json").write_text("{}")
        ok, msg = _check_evidence(evidence_dir)
        assert ok
        assert "2" in msg

    def test_missing_dir_fails(self, tmp_path: Path) -> None:
        """PEV-002: missing evidence directory → FAIL."""
        ok, msg = _check_evidence(tmp_path / "nonexistent")
        assert not ok
        assert "not found" in msg

    def test_empty_dir_fails(self, tmp_path: Path) -> None:
        """PEV-003: evidence directory with no .json files → FAIL."""
        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        ok, msg = _check_evidence(evidence_dir)
        assert not ok
        assert "No evidence" in msg


# --- Tests: Auto-threshold check (PR-C9) ------------------------------------


def _write_model_with_provenance(model_dir: Path) -> str:
    """Write a valid model directory, return manifest sha256 (binary)."""
    model_dir.mkdir(parents=True, exist_ok=True)

    model_data: dict[str, Any] = {
        "schema_version": "fill_model_v0",
        "bins": {"long|0|1|0": 6000},
        "global_prior_bps": 5000,
        "n_train_rows": 10,
        "bucket_thresholds": {
            "notional": [100, 500, 1000, 5000],
            "holding_ms": [1000, 10000, 60000, 300000],
            "max_fill_count": 3,
        },
    }
    model_content = json.dumps(model_data, indent=2, sort_keys=True) + "\n"
    (model_dir / "model.json").write_text(model_content)

    model_sha = hashlib.sha256(model_content.encode()).hexdigest()
    manifest: dict[str, Any] = {
        "schema_version": "fill_model_v0",
        "model_file": "model.json",
        "created_at_utc": "2025-01-01T00:00:00Z",
        "n_train_rows": 10,
        "global_prior_bps": 5000,
        "n_bins": 1,
        "sha256": {"model.json": model_sha},
    }
    manifest_content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (model_dir / "manifest.json").write_text(manifest_content)

    h = hashlib.sha256()
    with (model_dir / "manifest.json").open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_eval_with_provenance(
    eval_dir: Path, model_manifest_sha256: str, *, threshold: int = 2500
) -> None:
    """Write eval report with model provenance."""
    eval_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "schema_version": "fill_model_eval_v0",
        "model_manifest_sha256": model_manifest_sha256,
        "n_rows": 100,
        "recommended_threshold_bps": threshold,
        "calibration_well_calibrated": True,
        "calibration_max_error_bps": 200,
    }
    report_content = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (eval_dir / "eval_report.json").write_text(report_content, encoding="utf-8")

    report_sha = hashlib.sha256(report_content.encode("utf-8")).hexdigest()
    manifest: dict[str, Any] = {
        "schema_version": "fill_model_eval_v0",
        "sha256": {"eval_report.json": report_sha},
    }
    (eval_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


class TestCheckAutoThreshold:
    """PAT-001..PAT-003: auto-threshold preflight check (PR-C9)."""

    def test_valid_auto_threshold_passes(self, tmp_path: Path) -> None:
        """PAT-001: valid model+eval with matching provenance → PASS."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_with_provenance(model_dir)

        eval_dir = tmp_path / "eval"
        _write_eval_with_provenance(eval_dir, model_sha, threshold=3000)

        ok, msg = _check_auto_threshold(model_dir, eval_dir)
        assert ok
        assert "3000" in msg

    def test_provenance_mismatch_fails(self, tmp_path: Path) -> None:
        """PAT-002: model provenance mismatch → FAIL."""
        model_dir = tmp_path / "model"
        _write_model_with_provenance(model_dir)

        eval_dir = tmp_path / "eval"
        _write_eval_with_provenance(eval_dir, "wrong_sha" * 8)

        ok, msg = _check_auto_threshold(model_dir, eval_dir)
        assert not ok
        assert "failed" in msg.lower()

    def test_missing_eval_fails(self, tmp_path: Path) -> None:
        """PAT-003: missing eval dir → FAIL."""
        model_dir = tmp_path / "model"
        _write_model_with_provenance(model_dir)

        ok, msg = _check_auto_threshold(model_dir, tmp_path / "nonexistent")
        assert not ok
        assert "failed" in msg.lower()
