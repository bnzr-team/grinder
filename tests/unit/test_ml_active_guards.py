"""Tests for M8-02c ACTIVE mode guards and kill-switch (ADR-065).

Tests 15 scenarios from ADR-065:
1. Default OFF
2. Two-key activation
3. Ack required
4-7. Kill-switch behavior
8-9. ONNX load failures
10-11. Inference errors
12-13. ACTIVE success path vs SHADOW
14-15. Env allowlist
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from grinder.contracts import Snapshot
from grinder.ml.metrics import MlBlockReason
from grinder.ml.onnx import ONNX_AVAILABLE
from grinder.paper.engine import PaperEngine

# Test artifact path (if exists)
TEST_ARTIFACT_DIR = Path(__file__).parent.parent / "testdata" / "onnx_artifacts" / "tiny_regime"


def make_snapshot(ts: int = 1000, symbol: str = "BTCUSDT") -> Snapshot:
    """Create a minimal test snapshot."""
    return Snapshot(
        ts=ts,
        symbol=symbol,
        bid_price=Decimal("50000"),
        ask_price=Decimal("50010"),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        last_price=Decimal("50005"),
        last_qty=Decimal("0.1"),
    )


class TestDefaultOff:
    """Test 1: Default config → mode OFF."""

    def test_default_config_is_off(self) -> None:
        """All ML flags default to OFF/False."""
        engine = PaperEngine()

        assert engine._ml_infer_enabled is False
        assert engine._ml_shadow_mode is False
        assert engine._ml_active_enabled is False
        assert engine._ml_kill_switch is False
        assert engine._ml_active_ack is None
        assert engine._ml_active_allowed_envs == []
        assert engine._onnx_model is None


class TestTwoKeyActivation:
    """Test 2: Two-key activation required."""

    def test_active_requires_infer_enabled(self) -> None:
        """ml_active_enabled=True without ml_infer_enabled=True → ConfigError."""
        with pytest.raises(ValueError, match="requires ml_infer_enabled=True"):
            PaperEngine(
                ml_active_enabled=True,
                ml_infer_enabled=False,
                ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
            )

    def test_infer_only_without_mode_is_ambiguous(self) -> None:
        """ml_infer_enabled=True without shadow or active → ConfigError."""
        if not ONNX_AVAILABLE:
            pytest.skip("onnxruntime not installed")

        with pytest.raises(ValueError, match="ambiguous configuration"):
            PaperEngine(
                ml_infer_enabled=True,
                ml_shadow_mode=False,
                ml_active_enabled=False,
            )


class TestAckRequired:
    """Test 3: Explicit acknowledgment required."""

    @pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
    def test_active_requires_ack(self) -> None:
        """ml_active_enabled=True without ack → ConfigError."""
        with pytest.raises(ValueError, match="requires explicit acknowledgment"):
            PaperEngine(
                ml_active_enabled=True,
                ml_infer_enabled=True,
                ml_active_ack=None,
                onnx_artifact_dir=str(TEST_ARTIFACT_DIR),
            )

    @pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
    def test_active_wrong_ack_string(self) -> None:
        """ml_active_ack='yes' → ConfigError."""
        with pytest.raises(ValueError, match="I_UNDERSTAND_THIS_AFFECTS_TRADING"):
            PaperEngine(
                ml_active_enabled=True,
                ml_infer_enabled=True,
                ml_active_ack="yes",
                onnx_artifact_dir=str(TEST_ARTIFACT_DIR),
            )


class TestEnvAllowlist:
    """Tests 4-5: Environment allowlist."""

    @pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
    def test_env_allowlist_empty_allows_all(
        self, monkeypatch: pytest.MonkeyPatch, ml_registry_for_active: tuple[str, str, str]
    ) -> None:
        """Empty allowlist → ACTIVE allowed (with other conditions met)."""
        monkeypatch.setenv("GRINDER_ENV", "dev")
        registry_path, model_name, stage = ml_registry_for_active

        # Should NOT raise with empty allowlist
        engine = PaperEngine(
            ml_active_enabled=True,
            ml_infer_enabled=True,
            ml_shadow_mode=True,  # Both modes for flexibility
            ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
            ml_active_allowed_envs=[],  # Empty = allow all
            ml_registry_path=registry_path,
            ml_model_name=model_name,
            ml_stage=stage,
        )
        assert engine._ml_active_enabled is True

    @pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
    def test_env_allowlist_blocks_wrong_env(
        self, monkeypatch: pytest.MonkeyPatch, ml_registry_for_active: tuple[str, str, str]
    ) -> None:
        """ml_active_allowed_envs=['prod'] + GRINDER_ENV=dev → ConfigError."""
        monkeypatch.setenv("GRINDER_ENV", "dev")
        registry_path, model_name, stage = ml_registry_for_active

        with pytest.raises(ValueError, match="not allowed in environment 'dev'"):
            PaperEngine(
                ml_active_enabled=True,
                ml_infer_enabled=True,
                ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
                ml_active_allowed_envs=["prod"],
                ml_registry_path=registry_path,
                ml_model_name=model_name,
                ml_stage=stage,
            )

    @pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
    def test_env_allowlist_missing_env(
        self, monkeypatch: pytest.MonkeyPatch, ml_registry_for_active: tuple[str, str, str]
    ) -> None:
        """Non-empty allowlist + GRINDER_ENV missing → ConfigError."""
        monkeypatch.delenv("GRINDER_ENV", raising=False)
        registry_path, model_name, stage = ml_registry_for_active

        with pytest.raises(ValueError, match="GRINDER_ENV to be set"):
            PaperEngine(
                ml_active_enabled=True,
                ml_infer_enabled=True,
                ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
                ml_active_allowed_envs=["prod"],
                ml_registry_path=registry_path,
                ml_model_name=model_name,
                ml_stage=stage,
            )


class TestKillSwitch:
    """Tests 6-7: Kill-switch behavior."""

    def test_kill_switch_env_forces_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ML_KILL_SWITCH=1 → _is_ml_kill_switch_active() returns (True, KILL_SWITCH_ENV)."""
        monkeypatch.setenv("ML_KILL_SWITCH", "1")

        engine = PaperEngine()
        is_active, reason = engine._is_ml_kill_switch_active()
        assert is_active is True
        assert reason == MlBlockReason.KILL_SWITCH_ENV

    def test_kill_switch_config_forces_off(self) -> None:
        """ml_kill_switch=True → _is_ml_kill_switch_active() returns (True, KILL_SWITCH_CONFIG)."""
        engine = PaperEngine(ml_kill_switch=True)
        is_active, reason = engine._is_ml_kill_switch_active()
        assert is_active is True
        assert reason == MlBlockReason.KILL_SWITCH_CONFIG

    def test_kill_switch_per_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kill-switch activates mid-run → affects next snapshot."""
        engine = PaperEngine()

        # Initially OFF
        is_active, reason = engine._is_ml_kill_switch_active()
        assert is_active is False
        assert reason is None

        # Activate mid-run
        monkeypatch.setenv("ML_KILL_SWITCH", "1")

        # Should now be active (per-snapshot check)
        is_active, reason = engine._is_ml_kill_switch_active()
        assert is_active is True
        assert reason == MlBlockReason.KILL_SWITCH_ENV

    def test_kill_switch_not_active_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without kill-switch, _is_ml_kill_switch_active() returns (False, None)."""
        monkeypatch.delenv("ML_KILL_SWITCH", raising=False)

        engine = PaperEngine(ml_kill_switch=False)
        is_active, reason = engine._is_ml_kill_switch_active()
        assert is_active is False
        assert reason is None


class TestOnnxLoadFailure:
    """Tests 8-9: ONNX load failures."""

    def test_shadow_soft_fail_continues(self, tmp_path: Path) -> None:
        """ONNX load error in SHADOW → soft-fail, continue without model."""
        if not ONNX_AVAILABLE:
            pytest.skip("onnxruntime not installed")

        # Create invalid artifact dir (no model.onnx)
        artifact_dir = tmp_path / "invalid_artifact"
        artifact_dir.mkdir()
        (artifact_dir / "manifest.json").write_text('{"model_sha256":"abc"}')

        engine = PaperEngine(
            ml_infer_enabled=True,
            ml_shadow_mode=True,
            onnx_artifact_dir=str(artifact_dir),
        )

        # Load should soft-fail (no exception)
        engine._load_onnx_model()

        # Model should be None
        assert engine._onnx_model is None

    def test_active_load_fail_raises(self, tmp_path: Path) -> None:
        """ONNX load error in ACTIVE → ConfigError (fail-closed)."""
        if not ONNX_AVAILABLE:
            pytest.skip("onnxruntime not installed")

        # Create invalid artifact dir
        artifact_dir = tmp_path / "invalid_artifact"
        artifact_dir.mkdir()
        (artifact_dir / "manifest.json").write_text('{"model_sha256":"abc"}')

        # Create registry pointing to invalid artifact
        registry_file = tmp_path / "models.json"
        registry_file.write_text(
            json.dumps(
                {
                    "schema_version": "v1",
                    "models": {
                        "test_model": {
                            "shadow": None,
                            "active": {
                                "artifact_dir": "invalid_artifact",
                                "artifact_id": "invalid_v1",
                            },
                        }
                    },
                }
            )
        )

        engine = PaperEngine(
            ml_active_enabled=True,
            ml_infer_enabled=True,
            ml_shadow_mode=True,
            ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
            ml_active_allowed_envs=[],
            ml_registry_path=str(registry_file),
            ml_model_name="test_model",
            ml_stage="active",
        )

        # Load should raise for ACTIVE mode
        with pytest.raises(ValueError, match="ACTIVE mode: Failed to load"):
            engine._load_onnx_model()


class TestInferenceErrors:
    """Tests 10-11: Inference error handling."""

    def test_active_inference_error_no_policy_mutation(self) -> None:
        """Inference error in ACTIVE → policy_features unchanged."""
        engine = PaperEngine()

        # Mock model that raises
        mock_model = MagicMock()
        mock_model.predict.side_effect = RuntimeError("Inference failed")
        engine._onnx_model = mock_model

        policy_features: dict[str, Any] = {"mid_price": Decimal("50000")}
        original_features = policy_features.copy()

        # Should return False and NOT modify policy_features
        result = engine._run_active_inference(1000, "BTCUSDT", policy_features)

        assert result is False
        assert policy_features == original_features

    def test_shadow_inference_error_continues(self) -> None:
        """Inference error in SHADOW → no crash, just log."""
        engine = PaperEngine()

        # Mock model that raises
        mock_model = MagicMock()
        mock_model.predict.side_effect = RuntimeError("Inference failed")
        engine._onnx_model = mock_model

        policy_features: dict[str, Any] = {"mid_price": Decimal("50000")}

        # Should NOT raise, just log warning
        engine._run_shadow_inference(1000, "BTCUSDT", policy_features)

        # Policy features unchanged
        assert "mid_price" in policy_features


class TestActivePath:
    """Tests 12-13: ACTIVE success path."""

    def test_active_success_injects_features(self) -> None:
        """ACTIVE success → ML features added to policy_features."""
        engine = PaperEngine()

        # Mock model with successful prediction
        mock_prediction = MagicMock()
        mock_prediction.to_policy_features.return_value = {
            "ml_regime": 1,
            "ml_spacing_x1000": 1200,
        }
        mock_prediction.predicted_regime = "trending"
        mock_prediction.regime_probs_bps = [3000, 5000, 2000]
        mock_prediction.spacing_multiplier_x1000 = 1200

        mock_model = MagicMock()
        mock_model.predict.return_value = mock_prediction
        engine._onnx_model = mock_model

        policy_features: dict[str, Any] = {"mid_price": Decimal("50000")}

        result = engine._run_active_inference(1000, "BTCUSDT", policy_features)

        assert result is True
        assert "ml_regime" in policy_features
        assert "ml_spacing_x1000" in policy_features

    def test_shadow_does_not_inject_features(self) -> None:
        """SHADOW path → ML features NOT added to policy_features."""
        engine = PaperEngine()

        # Mock model with successful prediction
        mock_prediction = MagicMock()
        mock_prediction.predicted_regime = "trending"
        mock_prediction.regime_probs_bps = [3000, 5000, 2000]
        mock_prediction.spacing_multiplier_x1000 = 1200

        mock_model = MagicMock()
        mock_model.predict.return_value = mock_prediction
        engine._onnx_model = mock_model

        policy_features: dict[str, Any] = {"mid_price": Decimal("50000")}
        original_keys = set(policy_features.keys())

        engine._run_shadow_inference(1000, "BTCUSDT", policy_features)

        # Policy features should NOT have new ML keys
        assert set(policy_features.keys()) == original_keys


class TestIsActiveAllowed:
    """Test _is_ml_active_allowed() truth table."""

    def test_all_conditions_met(
        self, monkeypatch: pytest.MonkeyPatch, ml_registry_for_active: tuple[str, str, str]
    ) -> None:
        """All conditions true → ACTIVE allowed."""
        if not ONNX_AVAILABLE:
            pytest.skip("onnxruntime not installed")

        monkeypatch.delenv("ML_KILL_SWITCH", raising=False)
        monkeypatch.setenv("GRINDER_ENV", "prod")
        registry_path, model_name, stage = ml_registry_for_active

        engine = PaperEngine(
            ml_infer_enabled=True,
            ml_active_enabled=True,
            ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
            ml_active_allowed_envs=["prod"],
            ml_kill_switch=False,
            ml_registry_path=registry_path,
            ml_model_name=model_name,
            ml_stage=stage,
        )

        # Mock model loaded
        engine._onnx_model = MagicMock()

        allowed, reason = engine._is_ml_active_allowed(1000)
        assert allowed is True
        assert reason is None

    @pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
    def test_kill_switch_blocks(
        self, monkeypatch: pytest.MonkeyPatch, ml_registry_for_active: tuple[str, str, str]
    ) -> None:
        """Kill-switch ON → ACTIVE blocked with KILL_SWITCH_ENV reason."""
        monkeypatch.setenv("ML_KILL_SWITCH", "1")
        registry_path, model_name, stage = ml_registry_for_active

        engine = PaperEngine(
            ml_infer_enabled=True,
            ml_active_enabled=True,
            ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
            ml_active_allowed_envs=[],
            ml_registry_path=registry_path,
            ml_model_name=model_name,
            ml_stage=stage,
        )
        engine._onnx_model = MagicMock()

        allowed, reason = engine._is_ml_active_allowed(1000)
        assert allowed is False
        assert reason == MlBlockReason.KILL_SWITCH_ENV

    def test_infer_disabled_blocks(self) -> None:
        """ml_infer_enabled=False → ACTIVE blocked with INFER_DISABLED reason."""
        engine = PaperEngine(
            ml_infer_enabled=False,
            ml_active_enabled=False,
        )

        allowed, reason = engine._is_ml_active_allowed(1000)
        assert allowed is False
        assert reason == MlBlockReason.INFER_DISABLED

    @pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
    def test_active_disabled_blocks(self) -> None:
        """ml_active_enabled=False → ACTIVE blocked with ACTIVE_DISABLED reason."""
        engine = PaperEngine(
            ml_infer_enabled=True,
            ml_shadow_mode=True,  # To pass G2
            ml_active_enabled=False,
            onnx_artifact_dir=str(TEST_ARTIFACT_DIR),
        )

        allowed, reason = engine._is_ml_active_allowed(1000)
        assert allowed is False
        assert reason == MlBlockReason.ACTIVE_DISABLED

    def test_model_not_loaded_blocks(self, ml_registry_for_active: tuple[str, str, str]) -> None:
        """Model not loaded → ACTIVE blocked with MODEL_NOT_LOADED reason."""
        if not ONNX_AVAILABLE:
            pytest.skip("onnxruntime not installed")

        registry_path, model_name, stage = ml_registry_for_active

        engine = PaperEngine(
            ml_infer_enabled=True,
            ml_active_enabled=True,
            ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
            ml_active_allowed_envs=[],
            ml_registry_path=registry_path,
            ml_model_name=model_name,
            ml_stage=stage,
        )
        # Don't load model
        engine._onnx_model = None

        allowed, reason = engine._is_ml_active_allowed(1000)
        assert allowed is False
        assert reason == MlBlockReason.MODEL_NOT_LOADED
