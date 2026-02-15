#!/usr/bin/env python3
"""Train and export regime classifier to ONNX artifact.

M8-03b: Reproducible training pipeline for regime classification model.

Usage:
    python -m scripts.train_regime_model --out-dir /tmp/artifact --dataset-id toy1
    python -m scripts.train_regime_model --help

Output:
    <out-dir>/model.onnx         - ONNX model file
    <out-dir>/manifest.json      - Artifact manifest with SHA256
    <out-dir>/train_report.json  - Training metrics and metadata

Requirements:
    pip install grinder[ml]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from grinder.ml.onnx.features import FEATURE_ORDER
from grinder.ml.onnx.types import ARTIFACT_SCHEMA_VERSION

logger = logging.getLogger(__name__)

# Default seed for reproducibility
DEFAULT_SEED = 42
DEFAULT_N_SAMPLES = 300


@dataclass
class TrainReport:
    """Training run report.

    Contains all metadata needed to reproduce the training run.
    """

    dataset_id: str
    n_samples: int
    seed: int
    n_features: int
    train_accuracy: float
    regime_distribution: dict[str, int]
    created_at: str
    model_sha256: str
    onnx_opset_version: int
    sklearn_version: str
    skl2onnx_version: str
    notes: str | None = None


# Feature ranges for synthetic data generation (low, high)
# Used by generate_synthetic_data to create plausible feature values
_FEATURE_RANGES: dict[str, tuple[float, float]] = {
    "price_mid": (10000, 100000),
    "spread_bps": (1, 50),
    "volume_24h": (1e6, 1e9),
    "volume_1h": (1e6, 1e9),
    "volatility_1h_bps": (10, 500),
    "volatility_24h_bps": (10, 500),
    "position_size": (-1000, 1000),
    "position_notional": (-1000, 1000),
    "position_pnl_bps": (-500, 500),
    "grid_levels_active": (0, 100),
    "grid_utilization_pct": (0, 100),
    "trend_strength": (-1, 1),
    "momentum_1h": (-100, 100),
}


def generate_synthetic_data(
    n_samples: int,
    seed: int,
    dataset_id: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate deterministic synthetic training data.

    Creates feature matrix X, regime labels y_regime (0,1,2 for LOW/MID/HIGH),
    and spacing multipliers y_spacing.

    The data generation uses the dataset_id to derive additional seed mixing,
    ensuring different datasets produce different data even with same seed.

    Args:
        n_samples: Number of samples to generate.
        seed: Random seed for reproducibility.
        dataset_id: Dataset identifier (mixed into seed).

    Returns:
        Tuple of (X, y_regime, y_spacing).
    """
    # Mix dataset_id into seed for different data per dataset
    id_hash = int(hashlib.md5(dataset_id.encode()).hexdigest()[:8], 16)
    combined_seed = (seed + id_hash) % (2**31)

    rng = np.random.default_rng(combined_seed)

    # Generate features: each row is a sample with FEATURE_ORDER features
    n_features = len(FEATURE_ORDER)
    X = np.zeros((n_samples, n_features), dtype=np.float32)

    # Populate features with plausible ranges using lookup table
    for i, feat in enumerate(FEATURE_ORDER):
        if feat in _FEATURE_RANGES:
            low, high = _FEATURE_RANGES[feat]
            X[:, i] = rng.uniform(low, high, n_samples)
        elif feat == "price_bid":
            X[:, i] = X[:, 0] * rng.uniform(0.9990, 0.9999, n_samples)
        elif feat == "price_ask":
            X[:, i] = X[:, 0] * rng.uniform(1.0001, 1.0010, n_samples)
        else:
            X[:, i] = rng.standard_normal(n_samples)

    # Generate regime labels based on simple rules:
    # HIGH regime: high volatility or wide spread
    # LOW regime: low volatility and tight spread
    # MID regime: everything else
    volatility_idx = FEATURE_ORDER.index("volatility_1h_bps")
    spread_idx = FEATURE_ORDER.index("spread_bps")

    volatility = X[:, volatility_idx]
    spread = X[:, spread_idx]

    y_regime = np.ones(n_samples, dtype=np.int64)  # Default MID (1)
    y_regime[(volatility > 300) | (spread > 30)] = 2  # HIGH
    y_regime[(volatility < 100) & (spread < 10)] = 0  # LOW

    # Ensure all 3 classes are represented (required for sklearn classifier)
    # If any class is missing, force at least one sample per class
    for class_id in [0, 1, 2]:
        if not np.any(y_regime == class_id):
            y_regime[class_id] = class_id  # Set sample at index class_id to that class

    # Generate spacing multipliers: higher in volatile regimes
    y_spacing = np.where(
        y_regime == 2,
        rng.uniform(1.3, 1.8, n_samples),  # HIGH: widen spacing
        np.where(
            y_regime == 0,
            rng.uniform(0.7, 0.9, n_samples),  # LOW: tighten spacing
            rng.uniform(0.9, 1.1, n_samples),  # MID: normal
        ),
    ).astype(np.float32)

    return X, y_regime, y_spacing


def train_model(
    X: np.ndarray,
    y_regime: np.ndarray,
    y_spacing: np.ndarray,
    seed: int,
) -> tuple[Any, Any, float]:
    """Train sklearn models for regime and spacing.

    Args:
        X: Feature matrix (n_samples, n_features).
        y_regime: Regime labels (0,1,2).
        y_spacing: Spacing multipliers.
        seed: Random seed.

    Returns:
        Tuple of (regime_model, spacing_model, train_accuracy).
    """
    try:
        from sklearn.ensemble import (  # noqa: PLC0415
            RandomForestClassifier,
            RandomForestRegressor,
        )
    except ImportError as e:
        raise ImportError("scikit-learn required: pip install grinder[ml]") from e

    # Train regime classifier
    regime_model = RandomForestClassifier(
        n_estimators=10,
        max_depth=4,
        random_state=seed,
        n_jobs=1,  # Deterministic single-thread
    )
    regime_model.fit(X, y_regime)

    # Train spacing regressor
    spacing_model = RandomForestRegressor(
        n_estimators=10,
        max_depth=4,
        random_state=seed,
        n_jobs=1,
    )
    spacing_model.fit(X, y_spacing)

    # Compute train accuracy
    y_pred = regime_model.predict(X)
    train_accuracy = float(np.mean(y_pred == y_regime))

    return regime_model, spacing_model, train_accuracy


def export_to_onnx(
    regime_model: Any,
    spacing_model: Any,  # noqa: ARG001 (reserved for future use)
    n_features: int,
    output_path: Path,
) -> int:
    """Export sklearn regime classifier to ONNX file.

    Creates model with output:
    - regime_probs: (3,) probabilities for [LOW, MID, HIGH]

    Note: spacing_multiplier is not exported in MVP; OnnxMlModel defaults to 1.0.

    Args:
        regime_model: Trained regime classifier.
        spacing_model: Trained spacing regressor (reserved for future use).
        n_features: Number of input features.
        output_path: Path to write model.onnx.

    Returns:
        ONNX opset version used.
    """
    try:
        import onnx  # noqa: PLC0415
        from skl2onnx import convert_sklearn  # noqa: PLC0415
        from skl2onnx.common.data_types import FloatTensorType  # noqa: PLC0415
    except ImportError as e:
        raise ImportError("skl2onnx required: pip install grinder[ml]") from e

    opset_version = 15  # Stable opset for ONNX 1.15+

    # Convert regime classifier
    initial_type = [("input", FloatTensorType([None, n_features]))]
    regime_onnx = convert_sklearn(
        regime_model,
        initial_types=initial_type,
        target_opset=opset_version,
        options={id(regime_model): {"zipmap": False}},  # Get raw probs, not dict
    )

    # The OnnxMlModel._parse_outputs expects:
    # - "regime_probs": array shape (3,)
    # - "spacing_multiplier": scalar (optional, defaults to 1.0)

    # Rename "probabilities" -> "regime_probs" in all places
    # 1. In graph nodes that produce this output
    for node in regime_onnx.graph.node:
        for i, out_name in enumerate(node.output):
            if out_name == "probabilities":
                node.output[i] = "regime_probs"

    # 2. In graph outputs
    for output in regime_onnx.graph.output:
        if output.name == "probabilities":
            output.name = "regime_probs"

    # Remove "label" output (we only need probabilities)
    outputs_to_keep = [o for o in regime_onnx.graph.output if o.name == "regime_probs"]
    while len(regime_onnx.graph.output) > 0:
        regime_onnx.graph.output.pop()
    for o in outputs_to_keep:
        regime_onnx.graph.output.append(o)

    # Note: spacing_multiplier is not included in MVP; defaults to 1.0 in OnnxMlModel

    # Set deterministic graph name (skl2onnx generates random UUIDs)
    regime_onnx.graph.name = "regime_classifier"

    # Save model
    onnx.save(regime_onnx, str(output_path))

    return opset_version


def compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hash of file.

    Args:
        file_path: Path to file.

    Returns:
        Lowercase hex digest (64 chars).
    """
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            sha256.update(chunk)
    return sha256.hexdigest().lower()


def create_manifest(
    out_dir: Path,
    model_sha256: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create artifact manifest.json.

    Args:
        out_dir: Output directory.
        model_sha256: SHA256 of model.onnx.
        notes: Optional notes.

    Returns:
        Manifest dict.
    """
    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model_file": "model.onnx",
        "sha256": {"model.onnx": model_sha256},
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes": notes,
    }

    manifest_path = out_dir / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    return manifest


def train_and_export(
    out_dir: Path,
    dataset_id: str,
    seed: int = DEFAULT_SEED,
    n_samples: int = DEFAULT_N_SAMPLES,
    notes: str | None = None,
) -> TrainReport:
    """Full training pipeline: generate data, train, export to ONNX.

    Args:
        out_dir: Output directory for artifact.
        dataset_id: Dataset identifier.
        seed: Random seed.
        n_samples: Number of training samples.
        notes: Optional notes.

    Returns:
        TrainReport with all metadata.
    """
    import skl2onnx  # noqa: PLC0415
    import sklearn  # noqa: PLC0415

    logger.info(
        "Training regime model: dataset_id=%s, seed=%d, n_samples=%d",
        dataset_id,
        seed,
        n_samples,
    )

    # Create output directory
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate data
    X, y_regime, y_spacing = generate_synthetic_data(n_samples, seed, dataset_id)
    n_features = X.shape[1]

    regime_counts = {
        "LOW": int(np.sum(y_regime == 0)),
        "MID": int(np.sum(y_regime == 1)),
        "HIGH": int(np.sum(y_regime == 2)),
    }
    logger.info("Data generated: %d samples, regime distribution: %s", n_samples, regime_counts)

    # Train models
    regime_model, spacing_model, train_accuracy = train_model(X, y_regime, y_spacing, seed)
    logger.info("Training complete: accuracy=%.3f", train_accuracy)

    # Export to ONNX
    model_path = out_dir / "model.onnx"
    opset_version = export_to_onnx(regime_model, spacing_model, n_features, model_path)
    logger.info("ONNX export complete: %s", model_path)

    # Compute SHA256
    model_sha256 = compute_sha256(model_path)
    logger.info("Model SHA256: %s", model_sha256)

    # Create manifest
    create_manifest(out_dir, model_sha256, notes)
    logger.info("Manifest created: %s/manifest.json", out_dir)

    # Create training report
    report = TrainReport(
        dataset_id=dataset_id,
        n_samples=n_samples,
        seed=seed,
        n_features=n_features,
        train_accuracy=train_accuracy,
        regime_distribution=regime_counts,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        model_sha256=model_sha256,
        onnx_opset_version=opset_version,
        sklearn_version=sklearn.__version__,
        skl2onnx_version=skl2onnx.__version__,
        notes=notes,
    )

    report_path = out_dir / "train_report.json"
    with report_path.open("w") as f:
        json.dump(asdict(report), f, indent=2)
        f.write("\n")
    logger.info("Report written: %s", report_path)

    return report


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Train and export regime classifier to ONNX artifact",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Basic training with defaults
    python -m scripts.train_regime_model --out-dir /tmp/regime_v1 --dataset-id toy1

    # Custom parameters
    python -m scripts.train_regime_model \\
        --out-dir /tmp/regime_v2 \\
        --dataset-id production_v1 \\
        --seed 12345 \\
        --n-samples 1000 \\
        --notes "Initial production model"
""",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Output directory for artifact (created if not exists)",
    )
    parser.add_argument(
        "--dataset-id",
        type=str,
        required=True,
        help="Dataset identifier (affects data generation)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Random seed for reproducibility (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--n-samples",
        type=int,
        default=DEFAULT_N_SAMPLES,
        help=f"Number of training samples (default: {DEFAULT_N_SAMPLES})",
    )
    parser.add_argument(
        "--notes",
        type=str,
        default=None,
        help="Optional notes for manifest",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        report = train_and_export(
            out_dir=args.out_dir,
            dataset_id=args.dataset_id,
            seed=args.seed,
            n_samples=args.n_samples,
            notes=args.notes,
        )
        print(f"\n✓ Artifact created: {args.out_dir}")
        print(f"  Model SHA256: {report.model_sha256}")
        print(f"  Train accuracy: {report.train_accuracy:.3f}")
        print(f"  Regime distribution: {report.regime_distribution}")
        return 0

    except ImportError as e:
        print(f"ERROR: Missing dependency: {e}", file=sys.stderr)
        print("Install with: pip install grinder[ml]", file=sys.stderr)
        return 1

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        logger.exception("Training failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
