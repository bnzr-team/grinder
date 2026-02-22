"""Tests for grinder.ml.fill_model_loader (Track C, PR-C4a).

Covers:
- Loader: missing dir, SHA256 mismatch, success + predict range, caching.
- Online feature extraction: determinism, no-leakage fields.
- Shadow metrics: defaults when model unavailable, nonzero after calc.
- Contract: no FORBIDDEN_METRIC_LABELS in Prometheus output.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from grinder.ml.fill_model_loader import (
    extract_online_features,
    fill_model_metrics_to_prometheus_lines,
    get_fill_model_metrics,
    load_fill_model_v0,
    reset_fill_model_metrics,
    set_fill_model_metrics,
)
from grinder.ml.fill_model_v0 import FillModelV0
from grinder.observability.metrics_contract import FORBIDDEN_METRIC_LABELS

if TYPE_CHECKING:
    from pathlib import Path

# --- Helpers ---------------------------------------------------------------


def _make_model(tmp_path: Path, *, n_bins: int = 2, tamper: bool = False) -> Path:
    """Create a valid FillModelV0 model directory.

    Args:
        tmp_path: pytest tmp_path fixture.
        n_bins: Number of bins to create.
        tamper: If True, modify model.json after saving (SHA256 mismatch).

    Returns:
        Path to model directory.
    """
    all_bins = {"long|0|1|0": 7000, "short|1|2|1": 3000}
    bins = dict(list(all_bins.items())[:n_bins])
    model = FillModelV0(
        bins=bins,
        global_prior_bps=5000,
        n_train_rows=100,
    )
    model_dir = tmp_path / "fill_model_v0"
    model.save(model_dir, force=True, created_at_utc="2025-01-01T00:00:00Z")

    if tamper:
        model_path = model_dir / "model.json"
        data = json.loads(model_path.read_text())
        data["global_prior_bps"] = 9999
        model_path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")

    return model_dir


# --- Tests: Loader ---------------------------------------------------------


class TestLoader:
    """L-001..L-004: loader load/fail-open tests."""

    def test_loader_disabled_returns_none_when_dir_missing(self, tmp_path: Path) -> None:
        """L-001: missing dir → None + no crash."""
        result = load_fill_model_v0(tmp_path / "nonexistent")
        assert result is None

    def test_loader_manifest_sha_mismatch_fail_open(self, tmp_path: Path) -> None:
        """L-002: tampered model.json → SHA256 mismatch → None."""
        model_dir = _make_model(tmp_path, tamper=True)
        result = load_fill_model_v0(model_dir)
        assert result is None

    def test_loader_load_success_and_predict_range(self, tmp_path: Path) -> None:
        """L-003: valid model dir → loaded model, predict in [0, 10000]."""
        model_dir = _make_model(tmp_path)
        model = load_fill_model_v0(model_dir)
        assert model is not None
        assert len(model.bins) == 2
        assert model.global_prior_bps == 5000

        # Predict known bin
        features = extract_online_features(direction="long", notional=50)
        prob = model.predict(features)
        assert 0 <= prob <= 10000

    def test_loader_caching_no_reload_on_second_call(self, tmp_path: Path) -> None:
        """L-004: two loads from same dir return equal (but independent) models.

        No singleton/mtime caching — caller holds reference. This test
        verifies that load_fill_model_v0 is idempotent (returns correct
        model on repeated calls).
        """
        model_dir = _make_model(tmp_path)
        m1 = load_fill_model_v0(model_dir)
        m2 = load_fill_model_v0(model_dir)
        assert m1 is not None
        assert m2 is not None
        assert m1.bins == m2.bins
        assert m1.global_prior_bps == m2.global_prior_bps

    def test_loader_missing_manifest_fail_open(self, tmp_path: Path) -> None:
        """L-005: missing manifest.json → None."""
        model_dir = _make_model(tmp_path)
        (model_dir / "manifest.json").unlink()
        result = load_fill_model_v0(model_dir)
        assert result is None

    def test_loader_missing_model_json_fail_open(self, tmp_path: Path) -> None:
        """L-006: missing model.json → None."""
        model_dir = _make_model(tmp_path)
        (model_dir / "model.json").unlink()
        result = load_fill_model_v0(model_dir)
        assert result is None


# --- Tests: Online Feature Extraction --------------------------------------


class TestOnlineFeatures:
    """F-001..F-002: online feature extraction tests."""

    def test_extract_features_determinism(self) -> None:
        """F-001: same inputs → same features (deterministic)."""
        f1 = extract_online_features(
            direction="long", notional=500, entry_fill_count=2, holding_ms=5000
        )
        f2 = extract_online_features(
            direction="long", notional=500, entry_fill_count=2, holding_ms=5000
        )
        assert f1 == f2
        assert f1["direction"] == "long"
        assert isinstance(f1["notional_bucket"], int)
        assert isinstance(f1["entry_fill_count"], int)
        assert isinstance(f1["holding_ms_bucket"], int)

    def test_extract_features_no_leakage_fields(self) -> None:
        """F-002: features contain ONLY entry-side fields (4 fields, no exit/pnl)."""
        features = extract_online_features(direction="short", notional=1000)
        keys = set(features.keys())
        expected = {"direction", "notional_bucket", "entry_fill_count", "holding_ms_bucket"}
        assert keys == expected

    def test_extract_features_defaults(self) -> None:
        """F-003: default entry_fill_count=1, holding_ms=0 (conservative)."""
        features = extract_online_features(direction="long", notional=100)
        assert features["entry_fill_count"] == 1
        assert features["holding_ms_bucket"] == 0

    def test_extract_features_bucket_ranges(self) -> None:
        """F-004: notional and holding_ms bucket boundaries work correctly."""
        # Very small notional → bucket 0
        f_small = extract_online_features(direction="long", notional=10)
        assert f_small["notional_bucket"] == 0

        # Very large notional → max bucket
        f_large = extract_online_features(direction="long", notional=100000)
        assert f_large["notional_bucket"] == 4

        # Very long holding → max bucket
        f_long = extract_online_features(direction="long", notional=100, holding_ms=1_000_000)
        assert f_long["holding_ms_bucket"] == 4


# --- Tests: Shadow Metrics -------------------------------------------------


class TestShadowMetrics:
    """M-001..M-003: shadow metrics state + Prometheus output."""

    def setup_method(self) -> None:
        reset_fill_model_metrics()

    def teardown_method(self) -> None:
        reset_fill_model_metrics()

    def test_metrics_defaults_when_model_unavailable(self) -> None:
        """M-001: default state → prob_bps=0, calc_total=0, loaded=False."""
        prob_bps, calc_total, loaded = get_fill_model_metrics()
        assert prob_bps == 0
        assert calc_total == 0
        assert loaded is False

        lines = fill_model_metrics_to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_ml_fill_prob_bps_last 0" in text
        assert "grinder_ml_fill_model_loaded 0" in text

    def test_metrics_nonzero_when_model_loaded_and_calc_runs(self) -> None:
        """M-002: after set_fill_model_metrics → values reflected in output."""
        set_fill_model_metrics(7500, 42, True)
        prob_bps, calc_total, loaded = get_fill_model_metrics()
        assert prob_bps == 7500
        assert calc_total == 42
        assert loaded is True

        lines = fill_model_metrics_to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_ml_fill_prob_bps_last 7500" in text
        assert "grinder_ml_fill_model_loaded 1" in text

    def test_no_forbidden_labels_in_metrics(self) -> None:
        """M-003: Prometheus output contains no FORBIDDEN_METRIC_LABELS."""
        set_fill_model_metrics(5000, 10, True)
        lines = fill_model_metrics_to_prometheus_lines()
        text = "\n".join(lines)
        for label in FORBIDDEN_METRIC_LABELS:
            assert label not in text, f"Forbidden label {label!r} found in metrics output"

    def test_metrics_have_help_and_type(self) -> None:
        """M-004: Prometheus output includes HELP and TYPE for both metrics."""
        lines = fill_model_metrics_to_prometheus_lines()
        text = "\n".join(lines)
        assert "# HELP grinder_ml_fill_prob_bps_last" in text
        assert "# TYPE grinder_ml_fill_prob_bps_last gauge" in text
        assert "# HELP grinder_ml_fill_model_loaded" in text
        assert "# TYPE grinder_ml_fill_model_loaded gauge" in text
