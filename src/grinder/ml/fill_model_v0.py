"""Fill probability model v0 (Track C, PR-C2).

Pure-Python baseline fill probability estimator using calibrated
bin-count model.  No sklearn/numpy/scipy dependencies.

Model design
------------
* Label: ``outcome == "win"`` → 1, else → 0.
* Features: entry-side + timing only (no exit-price/pnl leakage).
  See ``FillModelFeaturesV0`` TypedDict for the SSOT contract.
* Binning: composite key from (direction, notional_bucket,
  entry_fill_count, holding_ms_bucket).
* Prediction: lookup bin → calibrated win-rate (0..10000 bps).
* Fallback: global prior for unseen bins.

Determinism
-----------
* ``model.json``: ``json.dumps(sort_keys=True, indent=2) + "\\n"``.
* All model params are int — no floats, no serialization drift.
* ``manifest.json`` includes sha256 of ``model.json``.

SSOT: this module.  ADR-069 in docs/DECISIONS.md.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from pathlib import Path

    from grinder.ml.fill_dataset import FillOutcomeRow


# --- Feature contract (SSOT) ------------------------------------------------


class FillModelFeaturesV0(TypedDict):
    """Entry-side features available BEFORE exit (no leakage).

    All fields are int or str — no floats.  This TypedDict is the
    SSOT contract between ``extract_features()`` and ``predict()``.
    """

    direction: str  # "long" or "short"
    notional_bucket: int  # quantize(entry_price * entry_qty) → 0..4
    entry_fill_count: int  # clamped: 1, 2, or 3 (3 means 3+)
    holding_ms_bucket: int  # quantize(holding_time_ms) → 0..4


# --- Bucket boundaries (fixed for v0) ---------------------------------------

# Notional thresholds (in quote currency units, e.g. USDT).
NOTIONAL_THRESHOLDS: tuple[int, ...] = (100, 500, 1_000, 5_000)

# Holding time thresholds (milliseconds).
HOLDING_MS_THRESHOLDS: tuple[int, ...] = (1_000, 10_000, 60_000, 300_000)

# Max entry_fill_count bucket (3 means "3 or more").
MAX_FILL_COUNT_BUCKET: int = 3


# --- Quantization functions --------------------------------------------------


def quantize_notional(notional: Any) -> int:
    """Quantize notional into bucket 0..4.

    Uses fixed thresholds defined in ``NOTIONAL_THRESHOLDS``.
    """
    val = float(notional)
    for i, threshold in enumerate(NOTIONAL_THRESHOLDS):
        if val <= threshold:
            return i
    return len(NOTIONAL_THRESHOLDS)


def quantize_holding_ms(holding_ms: int) -> int:
    """Quantize holding time (ms) into bucket 0..4.

    Uses fixed thresholds defined in ``HOLDING_MS_THRESHOLDS``.
    """
    for i, threshold in enumerate(HOLDING_MS_THRESHOLDS):
        if holding_ms <= threshold:
            return i
    return len(HOLDING_MS_THRESHOLDS)


def quantize_fill_count(count: int) -> int:
    """Clamp entry_fill_count to 1..MAX_FILL_COUNT_BUCKET."""
    return min(max(count, 1), MAX_FILL_COUNT_BUCKET)


def extract_features(row: FillOutcomeRow) -> FillModelFeaturesV0:
    """Extract features from a FillOutcomeRow (entry-side only).

    No leakage: does not use exit_price, realized_pnl, net_pnl,
    or pnl_bps as features.
    """
    return FillModelFeaturesV0(
        direction=row.direction,
        notional_bucket=quantize_notional(row.notional),
        entry_fill_count=quantize_fill_count(row.entry_fill_count),
        holding_ms_bucket=quantize_holding_ms(row.holding_time_ms),
    )


# --- Bin key -----------------------------------------------------------------


def _bin_key(features: FillModelFeaturesV0) -> str:
    """Deterministic bin key from features."""
    return (
        f"{features['direction']}|"
        f"{features['notional_bucket']}|"
        f"{features['entry_fill_count']}|"
        f"{features['holding_ms_bucket']}"
    )


# --- Model -------------------------------------------------------------------

# Default probability when no data available (50% = neutral).
_DEFAULT_PRIOR_BPS: int = 5000


class FillModelV0:
    """Calibrated bin-count fill probability model v0.

    Pure Python.  No ML library dependencies.

    Usage::

        model = FillModelV0.train(rows)
        features = extract_features(row)
        prob_bps = model.predict(features)  # 0..10000
        model.save(model_dir)

        model2 = FillModelV0.load(model_dir)
        assert model2.predict(features) == prob_bps
    """

    def __init__(
        self,
        bins: dict[str, int],
        global_prior_bps: int,
        n_train_rows: int,
    ) -> None:
        self.bins = bins
        self.global_prior_bps = global_prior_bps
        self.n_train_rows = n_train_rows

    def predict(self, features: FillModelFeaturesV0) -> int:
        """Predict fill probability in bps (0..10000).

        Returns the calibrated win-rate for the matching bin,
        or ``global_prior_bps`` if the bin was not seen during training.
        Result is always clamped to [0, 10000].
        """
        key = _bin_key(features)
        raw = self.bins.get(key, self.global_prior_bps)
        return max(0, min(10000, raw))

    @classmethod
    def train(cls, rows: list[FillOutcomeRow]) -> FillModelV0:
        """Train from a list of completed FillOutcomeRow objects.

        Label: outcome == "win" → 1, else → 0.
        Features: entry-side only (no pnl/exit leakage).
        """
        if not rows:
            return cls(
                bins={},
                global_prior_bps=_DEFAULT_PRIOR_BPS,
                n_train_rows=0,
            )

        # Count wins per bin (integer arithmetic only).
        bin_wins: dict[str, int] = {}
        bin_totals: dict[str, int] = {}
        total_wins = 0

        for row in rows:
            features = extract_features(row)
            key = _bin_key(features)
            win = 1 if row.outcome == "win" else 0

            bin_wins[key] = bin_wins.get(key, 0) + win
            bin_totals[key] = bin_totals.get(key, 0) + 1
            total_wins += win

        # Global prior (integer division with rounding).
        n = len(rows)
        global_prior_bps = (total_wins * 10000 + n // 2) // n

        # Per-bin calibrated probability (integer division).
        bins: dict[str, int] = {}
        for key in sorted(bin_totals):
            wins = bin_wins[key]
            total = bin_totals[key]
            bins[key] = (wins * 10000 + total // 2) // total

        return cls(
            bins=bins,
            global_prior_bps=global_prior_bps,
            n_train_rows=n,
        )

    # --- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict (all values are int/str)."""
        return {
            "schema_version": "fill_model_v0",
            "bins": self.bins,
            "global_prior_bps": self.global_prior_bps,
            "n_train_rows": self.n_train_rows,
            "bucket_thresholds": {
                "notional": list(NOTIONAL_THRESHOLDS),
                "holding_ms": list(HOLDING_MS_THRESHOLDS),
                "max_fill_count": MAX_FILL_COUNT_BUCKET,
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FillModelV0:
        """Deserialize from dict."""
        return cls(
            bins=d["bins"],
            global_prior_bps=d["global_prior_bps"],
            n_train_rows=d["n_train_rows"],
        )

    def save(
        self,
        model_dir: Path,
        *,
        force: bool = False,
        created_at_utc: str | None = None,
    ) -> Path:
        """Write model.json + manifest.json to ``model_dir``.

        Args:
            model_dir: Target directory (created if needed).
            force: Overwrite existing directory.
            created_at_utc: Override timestamp (for deterministic tests).

        Returns:
            Path to the created model directory.

        Raises:
            FileExistsError: If directory exists and force=False.
        """
        if model_dir.exists():
            if not force:
                raise FileExistsError(
                    f"Model directory already exists: {model_dir} (use --force to overwrite)"
                )
            shutil.rmtree(model_dir)

        model_dir.mkdir(parents=True)

        # Write model.json (deterministic).
        model_path = model_dir / "model.json"
        model_bytes = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        model_path.write_text(model_bytes)

        # Compute sha256 of model.json.
        model_sha256 = hashlib.sha256(model_bytes.encode()).hexdigest()

        # Build manifest.
        if created_at_utc is None:
            created_at_utc = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        manifest: dict[str, object] = {
            "schema_version": "fill_model_v0",
            "model_file": "model.json",
            "created_at_utc": created_at_utc,
            "n_train_rows": self.n_train_rows,
            "global_prior_bps": self.global_prior_bps,
            "n_bins": len(self.bins),
            "sha256": {
                "model.json": model_sha256,
            },
        }

        manifest_path = model_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

        return model_dir

    @classmethod
    def load(cls, model_dir: Path) -> FillModelV0:
        """Load model from ``model_dir``, verifying sha256 integrity.

        Args:
            model_dir: Directory containing model.json + manifest.json.

        Returns:
            FillModelV0 instance.

        Raises:
            FileNotFoundError: If manifest.json or model.json missing.
            ValueError: If sha256 mismatch (tampered model.json).
        """
        manifest_path = model_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())

        model_path = model_dir / "model.json"
        model_bytes = model_path.read_text()

        # Verify integrity.
        expected_sha = manifest["sha256"]["model.json"]
        actual_sha = hashlib.sha256(model_bytes.encode()).hexdigest()
        if actual_sha != expected_sha:
            raise ValueError(
                f"SHA256 mismatch for model.json: expected {expected_sha}, got {actual_sha}"
            )

        d = json.loads(model_bytes)
        return cls.from_dict(d)
