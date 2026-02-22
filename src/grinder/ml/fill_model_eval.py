"""FillModelV0 offline evaluation & threshold calibration (Track C, PR-C7).

Provides:
- ``threshold_sweep()``: evaluate TP/FP/TN/FN at 101 threshold points.
- ``calibration_check()``: compare predicted vs actual win-rate per bin.
- ``evaluate_fill_model()``: full evaluation producing an ``EvalReport``.
- ``write_eval_report()``: deterministic JSON + sha256 manifest artifact.

Design:
- **Pure offline**: no env vars, no runtime dependencies, no side effects
  beyond writing the report artifact.
- **Deterministic**: sort_keys=True, indent=2, trailing newline.
  Threshold sweep sorted by threshold_bps ASC.
  Calibration bins sorted lexicographically.
- **Integer arithmetic** for confusion matrix; float only for display %.
- **Cost-weighted score**: ``cost_score = TP + cost_ratio * TN``.
  Higher is better.  Tie-break: lowest threshold (most conservative).

SSOT: this module.  ADR-072 in docs/DECISIONS.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from grinder.ml.fill_model_v0 import _bin_key, extract_features

if TYPE_CHECKING:
    from pathlib import Path

    from grinder.ml.fill_dataset import FillOutcomeRow
    from grinder.ml.fill_model_v0 import FillModelV0

EVAL_SCHEMA_VERSION = "fill_model_eval_v0"
SWEEP_STEP_BPS = 100
CALIBRATION_WELL_CALIBRATED_MAX_ERROR_BPS = 500


# --- Data structures --------------------------------------------------------


@dataclass(frozen=True)
class SweepEntry:
    """One row of the threshold sweep table."""

    threshold_bps: int
    tp: int
    fp: int
    tn: int
    fn: int
    precision_pct: float
    recall_pct: float
    f1_pct: float
    block_rate_pct: float
    cost_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold_bps": self.threshold_bps,
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "precision_pct": round(self.precision_pct, 2),
            "recall_pct": round(self.recall_pct, 2),
            "f1_pct": round(self.f1_pct, 2),
            "block_rate_pct": round(self.block_rate_pct, 2),
            "cost_score": round(self.cost_score, 2),
        }


@dataclass(frozen=True)
class CalibrationEntry:
    """Per-bin calibration diagnostic."""

    bin_key: str
    predicted_bps: int
    actual_bps: int
    n_samples: int
    error_bps: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "bin_key": self.bin_key,
            "predicted_bps": self.predicted_bps,
            "actual_bps": self.actual_bps,
            "n_samples": self.n_samples,
            "error_bps": self.error_bps,
        }


@dataclass(frozen=True)
class EvalReport:
    """Full evaluation report."""

    schema_version: str
    dataset_path: str
    model_path: str
    dataset_manifest_sha256: str
    model_manifest_sha256: str
    n_rows: int
    n_wins: int
    n_losses: int
    n_breakeven: int
    global_prior_bps: int
    cost_ratio: float
    sweep_step_bps: int
    threshold_sweep: list[SweepEntry]
    recommended_threshold_bps: int
    calibration: list[CalibrationEntry]
    calibration_max_error_bps: int
    calibration_well_calibrated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_path": self.dataset_path,
            "model_path": self.model_path,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "model_manifest_sha256": self.model_manifest_sha256,
            "n_rows": self.n_rows,
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
            "n_breakeven": self.n_breakeven,
            "global_prior_bps": self.global_prior_bps,
            "cost_ratio": self.cost_ratio,
            "sweep_step_bps": self.sweep_step_bps,
            "threshold_sweep": [e.to_dict() for e in self.threshold_sweep],
            "recommended_threshold_bps": self.recommended_threshold_bps,
            "calibration": [e.to_dict() for e in self.calibration],
            "calibration_max_error_bps": self.calibration_max_error_bps,
            "calibration_well_calibrated": self.calibration_well_calibrated,
        }


# --- Predictions ------------------------------------------------------------


@dataclass(frozen=True)
class _Prediction:
    """Internal: one row's prediction + ground truth."""

    prob_bps: int
    is_win: bool
    bin_key: str


def _predict_all(
    rows: list[FillOutcomeRow],
    model: FillModelV0,
) -> list[_Prediction]:
    """Predict on all rows.  Returns list sorted by prob_bps ASC."""
    preds: list[_Prediction] = []
    for row in rows:
        features = extract_features(row)
        prob_bps = model.predict(features)
        key = _bin_key(features)
        preds.append(
            _Prediction(
                prob_bps=prob_bps,
                is_win=(row.outcome == "win"),
                bin_key=key,
            )
        )
    return preds


# --- Threshold sweep ---------------------------------------------------------


def threshold_sweep(
    predictions: list[_Prediction],
    cost_ratio: float,
) -> list[SweepEntry]:
    """Evaluate confusion matrix at each threshold (0..10000, step 100).

    At threshold T:
    - ALLOW if prob_bps >= T
    - BLOCK if prob_bps < T
    - TP = wins allowed, FP = non-wins allowed
    - TN = non-wins blocked, FN = wins blocked

    Cost score formula: ``cost_score = TP + cost_ratio * TN``
    Higher is better.  Each TP (win allowed) contributes 1.
    Each TN (loss blocked) contributes cost_ratio.

    Returns list of 101 SweepEntry, sorted by threshold_bps ASC.
    """
    n = len(predictions)
    total_pos = sum(1 for p in predictions if p.is_win)
    total_neg = n - total_pos

    entries: list[SweepEntry] = []

    for t in range(0, 10001, SWEEP_STEP_BPS):
        tp = 0
        fp = 0
        for p in predictions:
            if p.prob_bps >= t:
                if p.is_win:
                    tp += 1
                else:
                    fp += 1
        fn = total_pos - tp
        tn = total_neg - fp

        precision_pct = tp / (tp + fp) * 100.0 if tp + fp > 0 else 0.0
        recall_pct = tp / total_pos * 100.0 if total_pos > 0 else 0.0
        if precision_pct + recall_pct > 0:
            f1_pct = 2 * precision_pct * recall_pct / (precision_pct + recall_pct)
        else:
            f1_pct = 0.0

        # Block rate = (FN + TN) / N
        blocked = fn + tn
        block_rate_pct = blocked / n * 100.0 if n > 0 else 0.0

        # Cost score: TP + cost_ratio * TN
        cost_score = float(tp) + cost_ratio * float(tn)

        entries.append(
            SweepEntry(
                threshold_bps=t,
                tp=tp,
                fp=fp,
                tn=tn,
                fn=fn,
                precision_pct=precision_pct,
                recall_pct=recall_pct,
                f1_pct=f1_pct,
                block_rate_pct=block_rate_pct,
                cost_score=cost_score,
            )
        )

    return entries


def _best_threshold(sweep: list[SweepEntry]) -> int:
    """Pick threshold with highest cost_score.  Tie-break: lowest threshold."""
    best_score = -1.0
    best_threshold = 0
    for entry in sweep:
        if entry.cost_score > best_score:
            best_score = entry.cost_score
            best_threshold = entry.threshold_bps
    return best_threshold


# --- Calibration check -------------------------------------------------------


def calibration_check(
    predictions: list[_Prediction],
    model: FillModelV0,
) -> tuple[list[CalibrationEntry], int, bool]:
    """Compare predicted vs actual win-rate per bin.

    For each bin that appears in predictions:
    - actual_bps = (wins * 10000 + total // 2) // total  (same formula as training)
    - predicted_bps = model.bins[key] (or global_prior_bps for unseen bins)
    - error_bps = abs(predicted_bps - actual_bps)

    Empty bins (0 samples in predictions) are skipped.

    Returns:
        (entries sorted by bin_key, max_error_bps, well_calibrated)
    """
    # Aggregate per bin
    bin_wins: dict[str, int] = {}
    bin_totals: dict[str, int] = {}

    for pred in predictions:
        bin_wins[pred.bin_key] = bin_wins.get(pred.bin_key, 0) + (1 if pred.is_win else 0)
        bin_totals[pred.bin_key] = bin_totals.get(pred.bin_key, 0) + 1

    entries: list[CalibrationEntry] = []
    max_error = 0

    for key in sorted(bin_totals):
        total = bin_totals[key]
        if total == 0:
            continue

        wins = bin_wins.get(key, 0)
        actual_bps = (wins * 10000 + total // 2) // total
        predicted_bps = model.bins.get(key, model.global_prior_bps)
        error_bps = abs(predicted_bps - actual_bps)

        max_error = max(max_error, error_bps)

        entries.append(
            CalibrationEntry(
                bin_key=key,
                predicted_bps=predicted_bps,
                actual_bps=actual_bps,
                n_samples=total,
                error_bps=error_bps,
            )
        )

    well_calibrated = max_error < CALIBRATION_WELL_CALIBRATED_MAX_ERROR_BPS
    return entries, max_error, well_calibrated


# --- Full evaluation ---------------------------------------------------------


def evaluate_fill_model(
    *,
    rows: list[FillOutcomeRow],
    model: FillModelV0,
    cost_ratio: float = 2.0,
    dataset_path: str = "",
    model_path: str = "",
    dataset_manifest_sha256: str = "",
    model_manifest_sha256: str = "",
) -> EvalReport:
    """Run full offline evaluation.

    Args:
        rows: Fill outcome rows (same format as training data).
        model: Trained FillModelV0 instance.
        cost_ratio: Weight for blocking a loss vs allowing a win.
            Must be positive.
        dataset_path: Path string for provenance (recorded in report).
        model_path: Path string for provenance (recorded in report).
        dataset_manifest_sha256: SHA256 of dataset manifest.json.
        model_manifest_sha256: SHA256 of model manifest.json.

    Returns:
        EvalReport with threshold sweep, calibration, recommendation.

    Raises:
        ValueError: If cost_ratio <= 0.
    """
    if cost_ratio <= 0:
        raise ValueError("cost_ratio must be positive")

    predictions = _predict_all(rows, model)

    n_wins = sum(1 for p in predictions if p.is_win)
    n_total = len(predictions)
    # breakeven is outcome == "breakeven" → not win, not loss
    n_losses = sum(1 for r in rows if r.outcome == "loss")
    n_breakeven = n_total - n_wins - n_losses

    sweep = threshold_sweep(predictions, cost_ratio)
    recommended = _best_threshold(sweep)

    cal_entries, cal_max_error, cal_well_cal = calibration_check(predictions, model)

    return EvalReport(
        schema_version=EVAL_SCHEMA_VERSION,
        dataset_path=dataset_path,
        model_path=model_path,
        dataset_manifest_sha256=dataset_manifest_sha256,
        model_manifest_sha256=model_manifest_sha256,
        n_rows=n_total,
        n_wins=n_wins,
        n_losses=n_losses,
        n_breakeven=n_breakeven,
        global_prior_bps=model.global_prior_bps,
        cost_ratio=cost_ratio,
        sweep_step_bps=SWEEP_STEP_BPS,
        threshold_sweep=sweep,
        recommended_threshold_bps=recommended,
        calibration=cal_entries,
        calibration_max_error_bps=cal_max_error,
        calibration_well_calibrated=cal_well_cal,
    )


# --- Artifact writer ---------------------------------------------------------


def write_eval_report(
    report: EvalReport,
    out_dir: Path,
    *,
    force: bool = False,
) -> tuple[Path, Path]:
    """Write eval_report.json + manifest.json to out_dir.

    Deterministic: json.dumps(sort_keys=True, indent=2) + "\\n".

    Args:
        report: EvalReport to serialize.
        out_dir: Target directory (created if needed).
        force: Overwrite existing directory.

    Returns:
        (report_path, manifest_path)

    Raises:
        FileExistsError: If directory exists and force=False.
    """
    import shutil  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    out_dir = _Path(out_dir)

    if out_dir.exists():
        if not force:
            raise FileExistsError(
                f"Output directory already exists: {out_dir} (use --force to overwrite)"
            )
        shutil.rmtree(out_dir)

    out_dir.mkdir(parents=True)

    # Write eval_report.json (deterministic)
    report_path = out_dir / "eval_report.json"
    report_content = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
    report_path.write_text(report_content, encoding="utf-8")

    # SHA256 of report
    report_sha256 = hashlib.sha256(report_content.encode("utf-8")).hexdigest()

    # Write manifest
    manifest: dict[str, object] = {
        "schema_version": EVAL_SCHEMA_VERSION,
        "sha256": {
            "eval_report.json": report_sha256,
        },
    }
    manifest_path = out_dir / "manifest.json"
    manifest_content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_path.write_text(manifest_content, encoding="utf-8")

    return report_path, manifest_path
