"""Tests for grinder.ml.fill_model_eval (Track C, PR-C7).

Covers:
- Threshold sweep: 101 entries, correct fields, cost-score formula.
- Cost ratio: configurable, validation, effect on recommendation.
- Calibration: per-bin diagnostics, max error, well-calibrated flag.
- Report: schema fields, determinism, sha256 sidecar.
- Edge cases: all-win, all-loss datasets.
- Tie-breaker: lowest threshold wins on equal cost_score.
- No runtime deps / no new library deps.
- CLI stdout summary.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from scripts.eval_fill_model_v0 import _print_summary

import grinder.ml.fill_model_eval as _fill_model_eval_mod
from grinder.ml.fill_dataset import FillOutcomeRow
from grinder.ml.fill_model_eval import (
    EVAL_SCHEMA_VERSION,
    EvalReport,
    evaluate_fill_model,
    write_eval_report,
)
from grinder.ml.fill_model_v0 import FillModelV0

if TYPE_CHECKING:
    from pathlib import Path

# --- Helpers ---------------------------------------------------------------

_D = Decimal


def _make_row(
    *,
    direction: str = "long",
    entry_fill_count: int = 1,
    holding_time_ms: int = 1000,
    notional: str = "5000",
    outcome: str = "win",
    symbol: str = "BTCUSDT",
) -> FillOutcomeRow:
    """Convenience row builder for eval tests."""
    return FillOutcomeRow(
        row_id="test_row",
        symbol=symbol,
        direction=direction,
        entry_ts=1000,
        entry_price=_D("50000"),
        entry_qty=_D("0.1"),
        entry_fee=_D("0"),
        entry_fill_count=entry_fill_count,
        exit_ts=2000,
        exit_price=_D("51000"),
        exit_qty=_D("0.1"),
        exit_fee=_D("0"),
        exit_fill_count=1,
        realized_pnl=_D("100"),
        net_pnl=_D("100"),
        pnl_bps=200,
        holding_time_ms=holding_time_ms,
        notional=_D(notional),
        outcome=outcome,
        source="paper",
        dataset_version="v1",
    )


def _make_mixed_rows() -> list[FillOutcomeRow]:
    """6 wins + 4 losses → global prior ~6000 bps."""
    rows: list[FillOutcomeRow] = []
    for _ in range(6):
        rows.append(_make_row(outcome="win"))
    for _ in range(4):
        rows.append(_make_row(outcome="loss"))
    return rows


def _train_model(rows: list[FillOutcomeRow]) -> FillModelV0:
    """Train a model from rows."""
    return FillModelV0.train(rows)


def _eval_mixed(cost_ratio: float = 2.0) -> EvalReport:
    """Evaluate on 6-win/4-loss dataset."""
    rows = _make_mixed_rows()
    model = _train_model(rows)
    return evaluate_fill_model(
        rows=rows,
        model=model,
        cost_ratio=cost_ratio,
        dataset_path="/test/dataset",
        model_path="/test/model",
        dataset_manifest_sha256="abc123",
        model_manifest_sha256="def456",
    )


# --- Threshold sweep -------------------------------------------------------


class TestThresholdSweep:
    """REQ-003, REQ-004: threshold sweep has 101 entries with correct fields."""

    def test_threshold_sweep_101_entries(self) -> None:
        """Sweep contains exactly 101 entries (0, 100, ..., 10000)."""
        report = _eval_mixed()
        assert len(report.threshold_sweep) == 101

    def test_sweep_entry_fields(self) -> None:
        """Each entry has all required fields."""
        report = _eval_mixed()
        entry = report.threshold_sweep[0]
        d = entry.to_dict()
        required = {
            "threshold_bps",
            "tp",
            "fp",
            "tn",
            "fn",
            "precision_pct",
            "recall_pct",
            "f1_pct",
            "block_rate_pct",
            "cost_score",
        }
        assert required == set(d.keys())

    def test_sweep_sorted_asc(self) -> None:
        """Entries are sorted by threshold_bps ascending."""
        report = _eval_mixed()
        thresholds = [e.threshold_bps for e in report.threshold_sweep]
        assert thresholds == list(range(0, 10001, 100))

    def test_sweep_confusion_matrix_sums(self) -> None:
        """TP + FP + TN + FN = total rows at every threshold."""
        report = _eval_mixed()
        n = report.n_rows
        for entry in report.threshold_sweep:
            assert entry.tp + entry.fp + entry.tn + entry.fn == n

    def test_sweep_threshold_zero_allows_all(self) -> None:
        """At threshold 0, everything is allowed (TP + FP = N)."""
        report = _eval_mixed()
        entry = report.threshold_sweep[0]
        assert entry.threshold_bps == 0
        assert entry.tp + entry.fp == report.n_rows
        assert entry.tn == 0
        assert entry.fn == 0


# --- Cost score & recommendation -------------------------------------------


class TestCostScore:
    """REQ-005, REQ-006: cost ratio configurable, recommended threshold."""

    def test_recommended_threshold_is_best_cost_score(self) -> None:
        """Recommended threshold has the highest cost_score."""
        report = _eval_mixed()
        best = max(report.threshold_sweep, key=lambda e: e.cost_score)
        assert report.recommended_threshold_bps == best.threshold_bps

    def test_cost_ratio_configurable(self) -> None:
        """Different cost_ratio is recorded in report and may shift recommendation."""
        r1 = _eval_mixed(cost_ratio=1.0)
        r2 = _eval_mixed(cost_ratio=100.0)
        assert r1.cost_ratio == 1.0
        assert r2.cost_ratio == 100.0

    def test_cost_ratio_validation(self) -> None:
        """cost_ratio <= 0 raises ValueError."""
        rows = _make_mixed_rows()
        model = _train_model(rows)
        with pytest.raises(ValueError, match="cost_ratio must be positive"):
            evaluate_fill_model(rows=rows, model=model, cost_ratio=0.0)
        with pytest.raises(ValueError, match="cost_ratio must be positive"):
            evaluate_fill_model(rows=rows, model=model, cost_ratio=-1.0)

    def test_cost_score_formula(self) -> None:
        """cost_score = TP + cost_ratio * TN at each threshold."""
        cost_ratio = 3.0
        report = _eval_mixed(cost_ratio=cost_ratio)
        for entry in report.threshold_sweep:
            expected = float(entry.tp) + cost_ratio * float(entry.tn)
            assert abs(entry.cost_score - expected) < 0.01

    def test_tiebreaker_lowest_threshold(self) -> None:
        """When multiple thresholds tie on cost_score, pick lowest."""
        # All rows have same prediction → all thresholds below that value
        # have identical cost_score (all allowed).  Recommended = 0 (lowest).
        rows = [_make_row(outcome="win") for _ in range(5)]
        model = _train_model(rows)
        report = evaluate_fill_model(rows=rows, model=model)
        # At threshold 0, all allowed → cost_score = 5 + 2*0 = 5.
        # At all thresholds <= predicted_bps, same: all allowed.
        # At thresholds > predicted_bps, all blocked.
        # Best is "all allowed" → lowest such threshold = 0.
        assert report.recommended_threshold_bps == 0


# --- Calibration ------------------------------------------------------------


class TestCalibration:
    """REQ-007: calibration diagnostics."""

    def test_calibration_diagnostics(self) -> None:
        """Calibration entries have correct fields and bin coverage."""
        report = _eval_mixed()
        assert len(report.calibration) >= 1
        for entry in report.calibration:
            d = entry.to_dict()
            assert "bin_key" in d
            assert "predicted_bps" in d
            assert "actual_bps" in d
            assert "n_samples" in d
            assert "error_bps" in d
            assert d["n_samples"] > 0
            assert d["error_bps"] >= 0

    def test_calibration_max_error(self) -> None:
        """calibration_max_error_bps is max of per-bin error_bps."""
        report = _eval_mixed()
        max_err = max(e.error_bps for e in report.calibration)
        assert report.calibration_max_error_bps == max_err

    def test_calibration_well_calibrated_flag(self) -> None:
        """well_calibrated is True when max_error < 500 bps."""
        report = _eval_mixed()
        expected = report.calibration_max_error_bps < 500
        assert report.calibration_well_calibrated == expected

    def test_calibration_sorted_lexicographic(self) -> None:
        """Calibration entries sorted by bin_key."""
        report = _eval_mixed()
        keys = [e.bin_key for e in report.calibration]
        assert keys == sorted(keys)


# --- Report artifact --------------------------------------------------------


class TestReportArtifact:
    """REQ-008, REQ-009: schema, sha256, determinism."""

    def test_report_schema_fields(self) -> None:
        """Report dict has all required schema fields."""
        report = _eval_mixed()
        d = report.to_dict()
        required = {
            "schema_version",
            "dataset_path",
            "model_path",
            "dataset_manifest_sha256",
            "model_manifest_sha256",
            "n_rows",
            "n_wins",
            "n_losses",
            "n_breakeven",
            "global_prior_bps",
            "cost_ratio",
            "sweep_step_bps",
            "threshold_sweep",
            "recommended_threshold_bps",
            "calibration",
            "calibration_max_error_bps",
            "calibration_well_calibrated",
        }
        assert required.issubset(set(d.keys()))
        assert d["schema_version"] == EVAL_SCHEMA_VERSION

    def test_report_artifact_sha256(self, tmp_path: Path) -> None:
        """Written manifest.json sha256 matches eval_report.json."""
        report = _eval_mixed()
        report_path, manifest_path = write_eval_report(report, tmp_path / "out")

        report_content = report_path.read_text(encoding="utf-8")
        actual_sha = hashlib.sha256(report_content.encode("utf-8")).hexdigest()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_sha = manifest["sha256"]["eval_report.json"]
        assert actual_sha == expected_sha

    def test_determinism_two_runs(self, tmp_path: Path) -> None:
        """Two evaluations on same inputs produce byte-identical report."""
        rows = _make_mixed_rows()
        model = _train_model(rows)

        r1 = evaluate_fill_model(rows=rows, model=model, dataset_path="d", model_path="m")
        r2 = evaluate_fill_model(rows=rows, model=model, dataset_path="d", model_path="m")

        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        write_eval_report(r1, out1)
        write_eval_report(r2, out2)

        content1 = (out1 / "eval_report.json").read_text(encoding="utf-8")
        content2 = (out2 / "eval_report.json").read_text(encoding="utf-8")
        assert content1 == content2

    def test_report_overwrite_requires_force(self, tmp_path: Path) -> None:
        """FileExistsError without --force."""
        report = _eval_mixed()
        out = tmp_path / "out"
        write_eval_report(report, out)
        with pytest.raises(FileExistsError):
            write_eval_report(report, out)
        # With force, succeeds
        write_eval_report(report, out, force=True)


# --- Edge cases -------------------------------------------------------------


class TestEdgeCases:
    """All-win, all-loss datasets."""

    def test_all_win_dataset(self) -> None:
        """All wins: at threshold 0 → TP=N, FP=0, TN=0, FN=0."""
        rows = [_make_row(outcome="win") for _ in range(5)]
        model = _train_model(rows)
        report = evaluate_fill_model(rows=rows, model=model)

        entry = report.threshold_sweep[0]
        assert entry.tp == 5
        assert entry.fp == 0
        assert entry.tn == 0
        assert entry.fn == 0
        assert report.n_wins == 5
        assert report.n_losses == 0
        assert report.n_breakeven == 0

    def test_all_loss_dataset(self) -> None:
        """All losses: at threshold 0 → TP=0, FP=N."""
        rows = [_make_row(outcome="loss") for _ in range(5)]
        model = _train_model(rows)
        report = evaluate_fill_model(rows=rows, model=model)

        entry = report.threshold_sweep[0]
        assert entry.tp == 0
        assert entry.fp == 5
        assert report.n_losses == 5
        assert report.n_wins == 0


# --- No runtime / library deps ----------------------------------------------


class TestDeps:
    """REQ-011, REQ-012: no runtime deps, no new library deps."""

    def test_no_runtime_deps(self) -> None:
        """fill_model_eval.py has no os.environ reads or runtime imports."""
        source = inspect.getsource(_fill_model_eval_mod)
        assert "os.environ" not in source
        assert "from grinder.execution" not in source
        assert "from grinder.gating" not in source
        assert "from grinder.paper.engine" not in source

    def test_no_new_library_deps(self) -> None:
        """No sklearn/numpy/scipy/pandas imports."""
        source = inspect.getsource(_fill_model_eval_mod)
        assert "import sklearn" not in source
        assert "import numpy" not in source
        assert "import scipy" not in source
        assert "import pandas" not in source


# --- Stdout summary ---------------------------------------------------------


class TestStdoutSummary:
    """REQ-010: human-readable stdout output."""

    def test_stdout_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_print_summary outputs recommended threshold, block rate, precision, recall."""
        report = _eval_mixed()
        _print_summary(report)

        captured = capsys.readouterr().out
        assert "Recommended threshold:" in captured
        assert "bps" in captured
        assert "Block rate:" in captured
        assert "Precision:" in captured
        assert "Recall:" in captured
        assert "Calibration:" in captured
