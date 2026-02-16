"""Integration tests for registry → artifact → predict roundtrip.

M8-03c-1b: Validates that registry can load artifacts and run predictions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grinder.ml.onnx.artifact import load_artifact
from grinder.ml.onnx.model import OnnxMlModel
from grinder.ml.onnx.registry import ModelRegistry, Stage

# Skip if onnxruntime not available
try:
    import onnxruntime  # type: ignore  # noqa: F401

    ONNX_AVAILABLE = True
except ImportError:
    ONNX_AVAILABLE = False

pytestmark = pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")


def test_registry_to_predict_roundtrip(tmp_path: Path) -> None:
    """Test complete flow: registry → load artifact → predict.

    Uses golden_regime artifact from testdata.
    """
    # Create registry pointing to golden artifact
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "regime_classifier": {
                        "shadow": {
                            "artifact_dir": "tests/testdata/onnx_artifacts/golden_regime",
                            "artifact_id": "golden_regime_v1",
                            "git_sha": None,
                            "dataset_id": "golden_synthetic",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    # Load registry
    registry = ModelRegistry.load(registry_file)
    assert registry.schema_version == "v1"
    assert "regime_classifier" in registry.models

    # Get shadow pointer
    shadow = registry.get_stage_pointer("regime_classifier", Stage.SHADOW)
    assert shadow is not None
    assert shadow.artifact_id == "golden_regime_v1"
    assert shadow.artifact_dir == "tests/testdata/onnx_artifacts/golden_regime"

    # Resolve artifact directory (use repo root as base)
    repo_root = Path(__file__).parent.parent.parent
    artifact_dir = registry.resolve_artifact_dir(shadow, repo_root)
    assert artifact_dir.exists()
    assert artifact_dir.is_dir()

    # Load artifact
    artifact = load_artifact(artifact_dir)
    assert artifact.manifest.schema_version in ("v1", "v1.1")
    assert artifact.model_path.exists()

    # Load ONNX model
    model = OnnxMlModel.load_from_dir(artifact_dir)
    assert model is not None

    # Run prediction with fixed feature vector
    ts = 1234567890
    symbol = "TESTUSDT"
    features = {
        "price_mid": 50000.0,
        "price_bid": 49995.0,
        "price_ask": 50005.0,
        "spread_bps": 2.0,
        "notional_depth_bid_1bps": 100000.0,
        "notional_depth_ask_1bps": 95000.0,
        "depth_imbalance": 0.05,
        "ofi_zscore": 0.2,
        "natr_14_5m": 0.015,
        "vwap_distance_bps": 1.5,
        "volatility_zscore_30m": 0.8,
        "volume_flow_imbalance": -0.1,
        "avg_trade_size_zscore": 0.3,
        "spread_zscore": -0.5,
        "tick_direction": 1.0,
    }

    result = model.predict(ts, symbol, features)
    assert result is not None

    # Verify prediction structure (result is MlSignalSnapshot)
    assert hasattr(result, "regime_probs_bps")
    probs = result.regime_probs_bps  # Dict[str, int]
    assert isinstance(probs, dict)
    assert len(probs) == 3  # LOW, MID, HIGH
    assert set(probs.keys()) == {"LOW", "MID", "HIGH"}
    assert all(isinstance(v, int) for v in probs.values())
    assert sum(probs.values()) == 10000  # Must sum to 100.00% in bps

    # Verify determinism (run twice, should get same result)
    result2 = model.predict(ts, symbol, features)
    assert result2 is not None
    assert result2.regime_probs_bps == result.regime_probs_bps


def test_registry_with_null_active(tmp_path: Path) -> None:
    """Test registry with null active pointer."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {
                            "artifact_dir": "tests/testdata/onnx_artifacts/tiny_regime",
                            "artifact_id": "tiny_v1",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    registry = ModelRegistry.load(registry_file)

    shadow = registry.get_stage_pointer("test_model", Stage.SHADOW)
    assert shadow is not None

    active = registry.get_stage_pointer("test_model", Stage.ACTIVE)
    assert active is None


def test_registry_with_both_stages(tmp_path: Path) -> None:
    """Test registry with both shadow and active pointers."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "regime_classifier": {
                        "shadow": {
                            "artifact_dir": "tests/testdata/onnx_artifacts/golden_regime",
                            "artifact_id": "shadow_v2",
                        },
                        "active": {
                            "artifact_dir": "tests/testdata/onnx_artifacts/tiny_regime",
                            "artifact_id": "active_v1",
                        },
                    }
                },
            }
        )
    )

    registry = ModelRegistry.load(registry_file)

    shadow = registry.get_stage_pointer("regime_classifier", Stage.SHADOW)
    assert shadow is not None
    assert shadow.artifact_id == "shadow_v2"
    assert "golden_regime" in shadow.artifact_dir

    active = registry.get_stage_pointer("regime_classifier", Stage.ACTIVE)
    assert active is not None
    assert active.artifact_id == "active_v1"
    assert "tiny_regime" in active.artifact_dir
