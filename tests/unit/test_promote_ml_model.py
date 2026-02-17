"""Unit tests for promote_ml_model.py

M8-03c-3: Tests for promotion CLI with audit trail and fail-closed guards.
M8-04d: Tests for dataset verification guard on ACTIVE promotion.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from scripts.promote_ml_model import promote_model, verify_dataset_for_promotion

from grinder.ml.onnx import ONNX_AVAILABLE
from grinder.ml.onnx.registry import ModelRegistry, RegistryError, Stage

if TYPE_CHECKING:
    from typing import Any

# Test artifact directory (used by ML tests)
TEST_ARTIFACT_DIR = Path(__file__).parent.parent / "testdata" / "onnx_artifacts" / "tiny_regime"


def create_seed_registry(tmp_path: Path) -> Path:
    """Create a minimal seed registry for testing."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "generated_at_utc": "2026-01-01T00:00:00Z",
                "models": {
                    "test_model": {
                        "shadow": None,
                        "staging": None,
                        "active": None,
                        "history": [],
                    }
                },
            }
        )
    )
    return registry_file


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_promote_to_shadow_creates_history(tmp_path: Path) -> None:
    """Promote to SHADOW creates history entry."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        # Create minimal dummy artifact for tests when real artifact doesn't exist
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Promote to shadow
    updated_data = promote_model(
        registry_path=registry_file,
        model_name="test_model",
        stage=Stage.SHADOW,
        artifact_dir="test_artifact",
        artifact_id="shadow_v1",
        dataset_id="test_dataset",
        git_sha=None,
        notes="Test promotion",
        reason="Testing shadow promotion",
        dry_run=False,
    )

    # Verify shadow pointer was set
    assert updated_data["models"]["test_model"]["shadow"] is not None
    assert updated_data["models"]["test_model"]["shadow"]["artifact_id"] == "shadow_v1"

    # Verify history was appended
    history = updated_data["models"]["test_model"]["history"]
    assert len(history) == 1
    assert history[0]["to_stage"] == "shadow"
    assert history[0]["from_stage"] is None  # First promotion
    assert history[0]["reason"] == "Testing shadow promotion"

    # Verify registry can be loaded again
    registry = ModelRegistry.load(registry_file)
    shadow = registry.get_stage_pointer("test_model", Stage.SHADOW)
    assert shadow is not None
    assert shadow.artifact_id == "shadow_v1"


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_promote_to_active_requires_git_sha(tmp_path: Path) -> None:
    """ACTIVE promotion requires git_sha (fail-closed)."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Attempt ACTIVE promotion without git_sha
    with pytest.raises(RegistryError, match="ACTIVE promotion requires --git-sha"):
        promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.ACTIVE,
            artifact_dir="test_artifact",
            artifact_id="active_v1",
            dataset_id="test_dataset",
            git_sha=None,
            notes=None,
            reason=None,
            dry_run=False,
        )


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_promote_to_active_requires_dataset_id(tmp_path: Path) -> None:
    """ACTIVE promotion requires dataset_id (fail-closed)."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Attempt ACTIVE promotion without dataset_id
    with pytest.raises(RegistryError, match="ACTIVE promotion requires --dataset-id"):
        promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.ACTIVE,
            artifact_dir="test_artifact",
            artifact_id="active_v1",
            dataset_id=None,
            git_sha="1234567890abcdef1234567890abcdef12345678",
            notes=None,
            reason=None,
            dry_run=False,
        )


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_promote_to_active_with_full_metadata(tmp_path: Path) -> None:
    """ACTIVE promotion succeeds with git_sha and dataset_id."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Mock dataset verification (this test focuses on metadata, not dataset guard)
    with patch("scripts.promote_ml_model.verify_dataset_for_promotion"):
        # Promote to ACTIVE with full metadata
        updated_data = promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.ACTIVE,
            artifact_dir="test_artifact",
            artifact_id="active_v1",
            dataset_id="market_data_2026_q1",
            git_sha="1234567890abcdef1234567890abcdef12345678",
            notes="Production ready",
            reason="Passed 7 days validation",
            dry_run=False,
        )

    # Verify ACTIVE pointer was set
    active = updated_data["models"]["test_model"]["active"]
    assert active is not None
    assert active["artifact_id"] == "active_v1"
    assert active["git_sha"] == "1234567890abcdef1234567890abcdef12345678"
    assert active["dataset_id"] == "market_data_2026_q1"
    assert active["promoted_at_utc"] is not None

    # Verify history was appended
    history = updated_data["models"]["test_model"]["history"]
    assert len(history) == 1
    assert history[0]["to_stage"] == "active"
    assert history[0]["pointer"]["git_sha"] == "1234567890abcdef1234567890abcdef12345678"

    # Verify registry passes strict ACTIVE validation
    registry = ModelRegistry.load(registry_file)
    active_pointer = registry.get_stage_pointer("test_model", Stage.ACTIVE)
    assert active_pointer is not None
    assert active_pointer.git_sha == "1234567890abcdef1234567890abcdef12345678"


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_path_traversal_blocked(tmp_path: Path) -> None:
    """Path traversal in artifact_dir is blocked (fail-closed)."""
    registry_file = create_seed_registry(tmp_path)

    # Attempt path traversal
    with pytest.raises(RegistryError, match="Path traversal not allowed"):
        promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.SHADOW,
            artifact_dir="../../../etc/passwd",
            artifact_id="evil",
            dataset_id=None,
            git_sha=None,
            notes=None,
            reason=None,
            dry_run=False,
        )


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_absolute_path_blocked(tmp_path: Path) -> None:
    """Absolute paths in artifact_dir are blocked (fail-closed)."""
    registry_file = create_seed_registry(tmp_path)

    # Attempt absolute path
    with pytest.raises(RegistryError, match="Absolute paths not allowed"):
        promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.SHADOW,
            artifact_dir="/absolute/path",
            artifact_id="evil",
            dataset_id=None,
            git_sha=None,
            notes=None,
            reason=None,
            dry_run=False,
        )


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_dry_run_does_not_write(tmp_path: Path) -> None:
    """Dry-run mode previews changes without writing to registry."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Save original registry content
    original_content = registry_file.read_text()

    # Dry-run promotion
    updated_data = promote_model(
        registry_path=registry_file,
        model_name="test_model",
        stage=Stage.SHADOW,
        artifact_dir="test_artifact",
        artifact_id="shadow_v1",
        dataset_id="test_dataset",
        git_sha=None,
        notes="Test dry-run",
        reason=None,
        dry_run=True,
    )

    # Verify registry file was NOT modified
    assert registry_file.read_text() == original_content

    # Verify updated_data reflects the proposed changes
    assert updated_data["models"]["test_model"]["shadow"]["artifact_id"] == "shadow_v1"


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_history_max_50_entries(tmp_path: Path) -> None:
    """History is truncated to max 50 entries."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Create a registry with 50 existing history entries
    with registry_file.open() as f:
        registry_data: dict[str, Any] = json.load(f)

    # Add 50 dummy history entries
    for i in range(50):
        registry_data["models"]["test_model"]["history"].append(
            {
                "ts_utc": f"2026-01-{i + 1:02d}T00:00:00Z",
                "from_stage": None,
                "to_stage": "shadow",
                "actor": None,
                "source": "test",
                "reason": None,
                "notes": None,
                "pointer": {
                    "artifact_dir": "test_artifact",
                    "artifact_id": f"v{i}",
                    "git_sha": None,
                    "dataset_id": None,
                    "promoted_at_utc": f"2026-01-{i + 1:02d}T00:00:00Z",
                    "notes": None,
                    "actor": None,
                    "source": "test",
                    "feature_order_hash": None,
                },
                "registry_git_sha": None,
            }
        )

    with registry_file.open("w") as f:
        json.dump(registry_data, f, indent=2)
        f.write("\n")

    # Promote again (should truncate to 50)
    promote_model(
        registry_path=registry_file,
        model_name="test_model",
        stage=Stage.SHADOW,
        artifact_dir="test_artifact",
        artifact_id="shadow_new",
        dataset_id="test_dataset",
        git_sha=None,
        notes="Should truncate",
        reason=None,
        dry_run=False,
    )

    # Verify history is exactly 50 entries (newest first)
    with registry_file.open() as f:
        final_data = json.load(f)

    history = final_data["models"]["test_model"]["history"]
    assert len(history) == 50
    assert history[0]["pointer"]["artifact_id"] == "shadow_new"  # Newest first


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_artifact_not_found_fails(tmp_path: Path) -> None:
    """Promotion fails if artifact directory doesn't exist (fail-closed)."""
    registry_file = create_seed_registry(tmp_path)

    # Attempt promotion with non-existent artifact
    with pytest.raises(RegistryError, match="Artifact directory not found"):
        promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.SHADOW,
            artifact_dir="nonexistent",
            artifact_id="ghost",
            dataset_id=None,
            git_sha=None,
            notes=None,
            reason=None,
            dry_run=False,
        )


# ---------------------------------------------------------------------------
# M8-04d: Dataset verification guard tests
# ---------------------------------------------------------------------------


def test_verify_dataset_for_promotion_pass(tmp_path: Path) -> None:
    """M8-04d: verify_dataset_for_promotion passes when dataset is valid."""
    dataset_id = "test_ds_v1"
    datasets_dir = tmp_path / "datasets"
    dataset_dir = datasets_dir / dataset_id
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "manifest.json").write_text("{}")

    with patch("scripts.verify_dataset.verify_dataset", return_value=[]):
        # Should not raise
        verify_dataset_for_promotion(dataset_id, datasets_dir)


def test_verify_dataset_for_promotion_missing_dir(tmp_path: Path) -> None:
    """M8-04d: Fail-closed when dataset directory does not exist."""
    datasets_dir = tmp_path / "datasets"
    datasets_dir.mkdir()

    with pytest.raises(RegistryError, match="Dataset artifact not found"):
        verify_dataset_for_promotion("nonexistent_ds", datasets_dir)


def test_verify_dataset_for_promotion_missing_manifest(tmp_path: Path) -> None:
    """M8-04d: Fail-closed when dataset manifest.json is missing."""
    dataset_id = "test_ds_v1"
    datasets_dir = tmp_path / "datasets"
    dataset_dir = datasets_dir / dataset_id
    dataset_dir.mkdir(parents=True)
    # No manifest.json created

    with pytest.raises(RegistryError, match="Dataset manifest not found"):
        verify_dataset_for_promotion(dataset_id, datasets_dir)


def test_verify_dataset_for_promotion_sha_mismatch(tmp_path: Path) -> None:
    """M8-04d: Fail-closed when verify_dataset reports SHA mismatch."""
    dataset_id = "test_ds_v1"
    datasets_dir = tmp_path / "datasets"
    dataset_dir = datasets_dir / dataset_id
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "manifest.json").write_text("{}")

    sha_errors = ["SHA256 mismatch for data.parquet: manifest=abc..., actual=def..."]
    with (
        patch("scripts.verify_dataset.verify_dataset", return_value=sha_errors),
        pytest.raises(RegistryError, match="Dataset verification failed"),
    ):
        verify_dataset_for_promotion(dataset_id, datasets_dir)


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_promote_active_dataset_missing_fails(tmp_path: Path) -> None:
    """M8-04d: ACTIVE promotion fails when dataset artifact is missing."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Create empty datasets dir (no dataset inside)
    datasets_dir = tmp_path / "datasets"
    datasets_dir.mkdir()

    with pytest.raises(RegistryError, match="Dataset artifact not found"):
        promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.ACTIVE,
            artifact_dir="test_artifact",
            artifact_id="active_v1",
            dataset_id="missing_dataset",
            git_sha="1234567890abcdef1234567890abcdef12345678",
            notes=None,
            reason=None,
            dry_run=False,
            datasets_dir=datasets_dir,
        )


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_promote_active_dataset_sha_mismatch_fails(tmp_path: Path) -> None:
    """M8-04d: ACTIVE promotion fails when dataset SHA verification fails."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Create dataset dir with manifest (verify_dataset will be mocked to fail)
    datasets_dir = tmp_path / "datasets"
    dataset_dir = datasets_dir / "bad_sha_ds"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "manifest.json").write_text("{}")

    sha_error = "SHA256 mismatch for data.parquet: manifest=abc..., actual=def..."
    with (
        patch("scripts.verify_dataset.verify_dataset", return_value=[sha_error]),
        pytest.raises(RegistryError, match="Dataset verification failed"),
    ):
        promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.ACTIVE,
            artifact_dir="test_artifact",
            artifact_id="active_v1",
            dataset_id="bad_sha_ds",
            git_sha="1234567890abcdef1234567890abcdef12345678",
            notes=None,
            reason=None,
            dry_run=False,
            datasets_dir=datasets_dir,
        )


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_promote_active_with_verified_dataset(tmp_path: Path) -> None:
    """M8-04d: ACTIVE promotion succeeds when dataset verification passes."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Mock dataset verification to pass
    with patch("scripts.promote_ml_model.verify_dataset_for_promotion"):
        updated_data = promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.ACTIVE,
            artifact_dir="test_artifact",
            artifact_id="active_v1",
            dataset_id="verified_dataset",
            git_sha="1234567890abcdef1234567890abcdef12345678",
            notes="M8-04d pass test",
            reason="Dataset verified",
            dry_run=False,
        )

    active = updated_data["models"]["test_model"]["active"]
    assert active is not None
    assert active["artifact_id"] == "active_v1"
    assert active["dataset_id"] == "verified_dataset"


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
def test_promote_shadow_skips_dataset_verification(tmp_path: Path) -> None:
    """M8-04d: SHADOW promotion does NOT trigger dataset verification."""
    registry_file = create_seed_registry(tmp_path)

    # Copy test artifact
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    with patch("scripts.promote_ml_model.verify_dataset_for_promotion") as mock_verify:
        promote_model(
            registry_path=registry_file,
            model_name="test_model",
            stage=Stage.SHADOW,
            artifact_dir="test_artifact",
            artifact_id="shadow_v1",
            dataset_id="any_ds",
            git_sha=None,
            notes="Shadow test",
            reason=None,
            dry_run=False,
        )
        # verify_dataset_for_promotion should NOT be called for SHADOW
        mock_verify.assert_not_called()
