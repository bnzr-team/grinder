"""Tests for threshold resolver (Track C, PR-C9).

Covers:
- REQ-001: resolve_threshold extracts recommended_threshold_bps from valid report.
- REQ-002: sha256 mismatch in eval report → None.
- REQ-003: schema_version mismatch → None.
- REQ-004: fail-open on any unexpected error → None.
- REQ-005: recommend-only vs auto-apply modes in engine wiring.
- REQ-006: model provenance mismatch → None.
- REQ-007: evidence artifact written when GRINDER_ARTIFACT_DIR is set.
- REQ-008: engine auto-threshold wiring + preflight check.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from grinder.connectors.live_connector import SafeMode
from grinder.execution.sor_metrics import get_sor_metrics, reset_sor_metrics
from grinder.live import LiveEngineConfig, LiveEngineV0
from grinder.ml.fill_model_v0 import FillModelV0
from grinder.ml.threshold_resolver import (
    ThresholdResolution,
    resolve_threshold,
    resolve_threshold_result,
    write_threshold_resolution_evidence,
)
from grinder.observability.metrics_contract import (
    FORBIDDEN_METRIC_LABELS,
    REQUIRED_METRICS_PATTERNS,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- Helpers ------------------------------------------------------------------


def _write_model_dir(model_dir: Path) -> str:
    """Write a valid model directory, return manifest sha256."""
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

    # Return the sha256 of the manifest.json file itself (binary read)
    h = hashlib.sha256()
    with (model_dir / "manifest.json").open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_eval_dir(
    eval_dir: Path,
    model_manifest_sha256: str,
    *,
    threshold_bps: int | str = 2500,
    schema_version: str = "fill_model_eval_v0",
    well_calibrated: bool = True,
    ts_ms: int | None = None,
    extra_fields: dict[str, Any] | None = None,
    omit_fields: tuple[str, ...] = (),
) -> None:
    """Write a valid eval report directory.

    Args:
        ts_ms: If set, adds ``ts_ms`` to the report (for freshness tests).
        extra_fields: Extra keys to merge into the report dict.
        omit_fields: Field names to remove after building the report dict.
    """
    eval_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "schema_version": schema_version,
        "dataset_path": "ml/datasets/fill_outcomes/v1",
        "model_path": "ml/models/fill_model_v0",
        "dataset_manifest_sha256": "deadbeef" * 8,
        "model_manifest_sha256": model_manifest_sha256,
        "n_rows": 100,
        "n_wins": 60,
        "n_losses": 30,
        "n_breakeven": 10,
        "global_prior_bps": 5000,
        "cost_ratio": 2.0,
        "sweep_step_bps": 100,
        "threshold_sweep": [],
        "recommended_threshold_bps": threshold_bps,
        "calibration": [],
        "calibration_max_error_bps": 200 if well_calibrated else 800,
        "calibration_well_calibrated": well_calibrated,
    }
    if ts_ms is not None:
        report["ts_ms"] = ts_ms
    if extra_fields:
        report.update(extra_fields)
    for field in omit_fields:
        report.pop(field, None)

    report_content = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (eval_dir / "eval_report.json").write_text(report_content, encoding="utf-8")

    report_sha = hashlib.sha256(report_content.encode("utf-8")).hexdigest()
    manifest: dict[str, Any] = {
        "schema_version": "fill_model_eval_v0",
        "sha256": {"eval_report.json": report_sha},
    }
    (eval_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --- Tests: Core resolver ----------------------------------------------------


class TestResolveThreshold:
    """TR-001..TR-008: threshold resolution logic."""

    def test_valid_report(self, tmp_path: Path) -> None:
        """TR-001 (REQ-001): valid report → ThresholdResolution."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)

        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, threshold_bps=3000)

        result = resolve_threshold(eval_dir, model_dir)
        assert result is not None
        assert result.threshold_bps == 3000
        assert result.model_manifest_sha256 == model_sha
        assert result.eval_dir == str(eval_dir)
        assert len(result.eval_sha256) == 64  # hex sha256

    def test_sha256_mismatch(self, tmp_path: Path) -> None:
        """TR-002 (REQ-002): tampered eval_report.json → None."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)

        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha)

        # Tamper with eval_report.json
        (eval_dir / "eval_report.json").write_text('{"tampered": true}')

        result = resolve_threshold(eval_dir, model_dir)
        assert result is None

    def test_schema_mismatch(self, tmp_path: Path) -> None:
        """TR-003 (REQ-003): wrong schema_version → None."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)

        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, schema_version="fill_model_eval_v99")

        result = resolve_threshold(eval_dir, model_dir)
        assert result is None

    def test_model_version_mismatch(self, tmp_path: Path) -> None:
        """TR-004 (REQ-006): model provenance mismatch → None."""
        model_dir = tmp_path / "model"
        _write_model_dir(model_dir)

        eval_dir = tmp_path / "eval"
        # Use wrong model_manifest_sha256
        _write_eval_dir(eval_dir, "wrong_sha256" * 6)

        result = resolve_threshold(eval_dir, model_dir)
        assert result is None

    def test_fail_open_missing_eval_dir(self, tmp_path: Path) -> None:
        """TR-005 (REQ-004): missing eval dir → None (fail-open)."""
        model_dir = tmp_path / "model"
        _write_model_dir(model_dir)

        result = resolve_threshold(tmp_path / "nonexistent", model_dir)
        assert result is None

    def test_fail_open_missing_model_dir(self, tmp_path: Path) -> None:
        """TR-006 (REQ-004): missing model dir → None (fail-open)."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)

        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha)

        result = resolve_threshold(eval_dir, tmp_path / "no_model")
        assert result is None

    def test_fail_open_corrupt_manifest(self, tmp_path: Path) -> None:
        """TR-007 (REQ-004): corrupt eval manifest → None (fail-open)."""
        model_dir = tmp_path / "model"
        _write_model_dir(model_dir)

        eval_dir = tmp_path / "eval"
        eval_dir.mkdir()
        (eval_dir / "manifest.json").write_text("not json")
        (eval_dir / "eval_report.json").write_text("{}")

        result = resolve_threshold(eval_dir, model_dir)
        assert result is None

    def test_threshold_out_of_range(self, tmp_path: Path) -> None:
        """TR-008: threshold_bps out of [0, 10000] → None."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)

        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, threshold_bps=99999)

        result = resolve_threshold(eval_dir, model_dir)
        assert result is None

    def test_missing_recommended_threshold(self, tmp_path: Path) -> None:
        """TR-009: report has no recommended_threshold_bps → None."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)

        eval_dir = tmp_path / "eval"
        eval_dir.mkdir(parents=True)

        # Write report without recommended_threshold_bps
        report: dict[str, Any] = {
            "schema_version": "fill_model_eval_v0",
            "model_manifest_sha256": model_sha,
            "n_rows": 100,
        }
        report_content = json.dumps(report, indent=2, sort_keys=True) + "\n"
        (eval_dir / "eval_report.json").write_text(report_content, encoding="utf-8")
        report_sha = hashlib.sha256(report_content.encode("utf-8")).hexdigest()
        manifest: dict[str, Any] = {
            "schema_version": "fill_model_eval_v0",
            "sha256": {"eval_report.json": report_sha},
        }
        (eval_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )

        result = resolve_threshold(eval_dir, model_dir)
        assert result is None

    def test_zero_threshold_valid(self, tmp_path: Path) -> None:
        """TR-010: threshold_bps=0 is valid (edge case)."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)

        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, threshold_bps=0)

        result = resolve_threshold(eval_dir, model_dir)
        assert result is not None
        assert result.threshold_bps == 0


# --- Tests: Evidence artifact -------------------------------------------------


class TestThresholdEvidence:
    """TE-001..TE-003: threshold resolution evidence artifacts."""

    def test_evidence_written(self, tmp_path: Path) -> None:
        """TE-001 (REQ-007): evidence artifact written when out_dir is set."""
        resolution = ThresholdResolution(
            threshold_bps=3000,
            eval_sha256="a" * 64,
            model_manifest_sha256="b" * 64,
            eval_dir="/eval",
        )

        result = write_threshold_resolution_evidence(
            resolution=resolution,
            configured_bps=2500,
            mode="auto_apply",
            effective_bps=3000,
            out_dir=tmp_path,
        )

        assert result is not None
        json_path, sha_path = result
        assert json_path.exists()
        assert sha_path.exists()

        data = json.loads(json_path.read_text())
        assert data["artifact_version"] == "threshold_resolution_v1"
        assert data["mode"] == "auto_apply"
        assert data["recommended_bps"] == 3000
        assert data["configured_bps"] == 2500
        assert data["effective_bps"] == 3000

    def test_no_artifact_dir_returns_none(self, monkeypatch: Any) -> None:
        """TE-002: no GRINDER_ARTIFACT_DIR → None (no write)."""
        monkeypatch.delenv("GRINDER_ARTIFACT_DIR", raising=False)

        resolution = ThresholdResolution(
            threshold_bps=3000,
            eval_sha256="a" * 64,
            model_manifest_sha256="b" * 64,
            eval_dir="/eval",
        )

        result = write_threshold_resolution_evidence(
            resolution=resolution,
            configured_bps=2500,
            mode="recommend_only",
            effective_bps=2500,
        )
        assert result is None

    def test_sha256_sidecar_valid(self, tmp_path: Path) -> None:
        """TE-003: sha256 sidecar matches JSON content."""
        resolution = ThresholdResolution(
            threshold_bps=2500,
            eval_sha256="c" * 64,
            model_manifest_sha256="d" * 64,
            eval_dir="/eval",
        )

        result = write_threshold_resolution_evidence(
            resolution=resolution,
            configured_bps=2500,
            mode="recommend_only",
            effective_bps=2500,
            out_dir=tmp_path,
        )

        assert result is not None
        json_path, sha_path = result

        content = json_path.read_text(encoding="utf-8")
        expected_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        sha_line = sha_path.read_text(encoding="utf-8").strip()
        assert sha_line.startswith(expected_sha)


# --- Tests: Metrics -----------------------------------------------------------


class TestAutoThresholdMetrics:
    """TM-001..TM-004: auto-threshold metrics."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()

    def test_default_metric_zero(self) -> None:
        """TM-001: default auto_threshold_bps is 0."""
        metrics = get_sor_metrics()
        assert metrics.fill_prob_auto_threshold_bps == 0

    def test_set_auto_threshold(self) -> None:
        """TM-002: set_fill_prob_auto_threshold updates value."""
        metrics = get_sor_metrics()
        metrics.set_fill_prob_auto_threshold(3000)
        assert metrics.fill_prob_auto_threshold_bps == 3000

    def test_prometheus_lines_include_auto_threshold(self) -> None:
        """TM-003: to_prometheus_lines() includes auto-threshold metric."""
        metrics = get_sor_metrics()
        metrics.set_fill_prob_auto_threshold(2500)
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_router_fill_prob_auto_threshold_bps 2500" in text

    def test_prometheus_help_and_type(self) -> None:
        """TM-004: Prometheus output includes HELP and TYPE for auto-threshold."""
        metrics = get_sor_metrics()
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        assert "# HELP grinder_router_fill_prob_auto_threshold_bps" in text
        assert "# TYPE grinder_router_fill_prob_auto_threshold_bps gauge" in text


# --- Tests: Contract ----------------------------------------------------------


class TestAutoThresholdContract:
    """TC-001..TC-002: metrics contract compliance."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()

    def test_required_patterns_present(self) -> None:
        """TC-001: REQUIRED_METRICS_PATTERNS includes auto-threshold patterns."""
        expected = [
            "# HELP grinder_router_fill_prob_auto_threshold_bps",
            "# TYPE grinder_router_fill_prob_auto_threshold_bps",
            "grinder_router_fill_prob_auto_threshold_bps",
        ]
        for pattern in expected:
            assert pattern in REQUIRED_METRICS_PATTERNS, f"Missing pattern: {pattern!r}"

    def test_no_forbidden_labels(self) -> None:
        """TC-002: auto-threshold metric has no FORBIDDEN_METRIC_LABELS."""
        metrics = get_sor_metrics()
        metrics.set_fill_prob_auto_threshold(3000)
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        for label in FORBIDDEN_METRIC_LABELS:
            assert label not in text, f"Forbidden label {label!r} found"


# --- Tests: Engine wiring ----------------------------------------------------


class TestEngineAutoThreshold:
    """TEW-001..TEW-003: engine auto-threshold wiring."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()

    def test_recommend_only_mode(self, tmp_path: Path, monkeypatch: Any) -> None:
        """TEW-001 (REQ-005): recommend-only mode logs but does not override."""
        # Set up model + eval dirs
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, threshold_bps=4000)

        # Configure env: eval_dir set, auto_threshold OFF
        monkeypatch.setenv("GRINDER_FILL_MODEL_DIR", str(model_dir))
        monkeypatch.setenv("GRINDER_FILL_PROB_EVAL_DIR", str(eval_dir))
        monkeypatch.setenv("GRINDER_FILL_PROB_AUTO_THRESHOLD", "0")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "2500")

        model = FillModelV0(bins={}, global_prior_bps=5000, n_train_rows=10)
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(
                armed=False,
                mode=SafeMode.LIVE_TRADE,
                kill_switch_active=False,
                symbol_whitelist=[],
            ),
            fill_model=model,
        )

        # Threshold should NOT be overridden (recommend-only)
        assert engine._fill_prob_min_bps == 2500
        # But metric should show resolved value
        assert get_sor_metrics().fill_prob_auto_threshold_bps == 4000

    def test_auto_apply_mode(self, tmp_path: Path, monkeypatch: Any) -> None:
        """TEW-002 (REQ-005): auto-apply mode overrides configured threshold."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, threshold_bps=3500)

        monkeypatch.setenv("GRINDER_FILL_MODEL_DIR", str(model_dir))
        monkeypatch.setenv("GRINDER_FILL_PROB_EVAL_DIR", str(eval_dir))
        monkeypatch.setenv("GRINDER_FILL_PROB_AUTO_THRESHOLD", "1")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "2500")

        model = FillModelV0(bins={}, global_prior_bps=5000, n_train_rows=10)
        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(
                armed=False,
                mode=SafeMode.LIVE_TRADE,
                kill_switch_active=False,
                symbol_whitelist=[],
            ),
            fill_model=model,
        )

        # Threshold SHOULD be overridden
        assert engine._fill_prob_min_bps == 3500
        assert get_sor_metrics().fill_prob_auto_threshold_bps == 3500

    def test_no_eval_dir_no_change(self, monkeypatch: Any) -> None:
        """TEW-003: no GRINDER_FILL_PROB_EVAL_DIR → threshold unchanged."""
        monkeypatch.delenv("GRINDER_FILL_PROB_EVAL_DIR", raising=False)
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "2500")

        engine = LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(
                armed=False,
                mode=SafeMode.LIVE_TRADE,
                kill_switch_active=False,
                symbol_whitelist=[],
            ),
        )

        assert engine._fill_prob_min_bps == 2500
        assert get_sor_metrics().fill_prob_auto_threshold_bps == 0

    def test_engine_init_sets_initialized_gauge(self, monkeypatch: Any) -> None:
        """PR-C4: LiveEngineV0.__init__ sets engine_initialized=True in SorMetrics."""
        monkeypatch.delenv("GRINDER_FILL_PROB_EVAL_DIR", raising=False)

        assert get_sor_metrics().engine_initialized is False

        LiveEngineV0(
            paper_engine=MagicMock(),
            exchange_port=MagicMock(),
            config=LiveEngineConfig(
                armed=False,
                mode=SafeMode.LIVE_TRADE,
                kill_switch_active=False,
                symbol_whitelist=[],
            ),
        )

        assert get_sor_metrics().engine_initialized is True


# --- Tests: Sanity checks (PR-B1) -------------------------------------------


class TestSanityChecks:
    """BS-001..BS-012: hardened sanity checks via resolve_threshold_result()."""

    def test_valid_report_returns_ok(self, tmp_path: Path) -> None:
        """BS-001: valid report → reason_code == 'ok', resolution not None."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, threshold_bps=3000)

        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is not None
        assert result.reason_code == "ok"
        assert result.resolution.threshold_bps == 3000

    def test_recommended_threshold_string_type(self, tmp_path: Path) -> None:
        """BS-002: recommended_threshold_bps = '2500' (str) → bad_type."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, threshold_bps="2500")

        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is None
        assert result.reason_code == "bad_type"
        assert "recommended_threshold_bps" in result.detail

    def test_recommended_threshold_negative(self, tmp_path: Path) -> None:
        """BS-003: recommended_threshold_bps = -1 → out_of_range."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, threshold_bps=-1)

        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is None
        assert result.reason_code == "out_of_range"

    def test_recommended_threshold_boundary_10001(self, tmp_path: Path) -> None:
        """BS-004: recommended_threshold_bps = 10001 → out_of_range."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, threshold_bps=10001)

        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is None
        assert result.reason_code == "out_of_range"

    def test_missing_schema_version_field(self, tmp_path: Path) -> None:
        """BS-005: report without 'schema_version' → missing_field."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, omit_fields=("schema_version",))

        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is None
        assert result.reason_code == "missing_field"
        assert "schema_version" in result.detail

    def test_missing_model_manifest_sha256_field(self, tmp_path: Path) -> None:
        """BS-006: report without 'model_manifest_sha256' → missing_field."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha, omit_fields=("model_manifest_sha256",))

        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is None
        assert result.reason_code == "missing_field"
        assert "model_manifest_sha256" in result.detail

    def test_manifest_missing_sha256_block(self, tmp_path: Path) -> None:
        """BS-007: eval manifest.json without 'sha256' key → missing_field."""
        model_dir = tmp_path / "model"
        _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        eval_dir.mkdir(parents=True)
        # Write a manifest without sha256 block
        (eval_dir / "manifest.json").write_text(
            json.dumps({"schema_version": "fill_model_eval_v0"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (eval_dir / "eval_report.json").write_text("{}", encoding="utf-8")

        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is None
        assert result.reason_code == "missing_field"
        assert "sha256" in result.detail

    def test_freshness_too_old(self, tmp_path: Path, monkeypatch: Any) -> None:
        """BS-008: stale ts_ms + max_age set → timestamp_too_old."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        # 48 hours ago

        stale_ts_ms = int((time.time() - 48 * 3600) * 1000)
        _write_eval_dir(eval_dir, model_sha, ts_ms=stale_ts_ms)

        monkeypatch.setenv("GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS", "24")
        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is None
        assert result.reason_code == "timestamp_too_old"

    def test_freshness_future(self, tmp_path: Path, monkeypatch: Any) -> None:
        """BS-009: future ts_ms + max_age set → timestamp_future."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        # 1 hour in the future (well beyond 5-min tolerance)

        future_ts_ms = int((time.time() + 3600) * 1000)
        _write_eval_dir(eval_dir, model_sha, ts_ms=future_ts_ms)

        monkeypatch.setenv("GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS", "24")
        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is None
        assert result.reason_code == "timestamp_future"

    def test_freshness_disabled_by_default(self, tmp_path: Path, monkeypatch: Any) -> None:
        """BS-010: very stale report but env not set → ok (freshness disabled)."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        # 72 hours ago — very stale

        stale_ts_ms = int((time.time() - 72 * 3600) * 1000)
        _write_eval_dir(eval_dir, model_sha, ts_ms=stale_ts_ms)

        monkeypatch.delenv("GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS", raising=False)
        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is not None
        assert result.reason_code == "ok"

    def test_freshness_no_timestamp_passes(self, tmp_path: Path, monkeypatch: Any) -> None:
        """BS-011: env set but no ts in report → ok (no timestamp to check)."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha)  # no ts_ms

        monkeypatch.setenv("GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS", "24")
        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is not None
        assert result.reason_code == "ok"

    def test_freshness_env_invalid(self, tmp_path: Path, monkeypatch: Any) -> None:
        """BS-012: bad env var value → env_invalid."""
        model_dir = tmp_path / "model"
        model_sha = _write_model_dir(model_dir)
        eval_dir = tmp_path / "eval"
        _write_eval_dir(eval_dir, model_sha)

        monkeypatch.setenv("GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS", "not_a_number")
        result = resolve_threshold_result(eval_dir, model_dir)
        assert result.resolution is None
        assert result.reason_code == "env_invalid"
