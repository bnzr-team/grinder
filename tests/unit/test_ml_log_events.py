"""Tests for M8-02c-3 ML structured log events (ADR-065).

Tests verify:
1. ML_ACTIVE_ON logged on successful inference
2. ML_ACTIVE_BLOCKED logged with reason code (truth table priority)
3. ML_KILL_SWITCH_ON logged when kill-switch active
4. ML_INFER_OK logged with prediction details + artifact_dir
5. ML_INFER_ERROR logged on inference exception
6. Log levels are correct (info/warning/error)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from grinder.ml.metrics import MlBlockReason, reset_ml_metrics_state
from grinder.ml.onnx import ONNX_AVAILABLE
from grinder.paper.engine import PaperEngine

# Test artifact path (if exists)
TEST_ARTIFACT_DIR = Path(__file__).parent.parent / "testdata" / "onnx_artifacts" / "tiny_regime"


@pytest.fixture(autouse=True)
def reset_ml_state() -> None:
    """Reset ML metrics state before each test."""
    reset_ml_metrics_state()


class TestMlKillSwitchLogEvents:
    """Tests for ML_KILL_SWITCH_ON log events."""

    def test_kill_switch_env_returns_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ML_KILL_SWITCH=1 env var returns KILL_SWITCH_ENV reason."""
        monkeypatch.setenv("ML_KILL_SWITCH", "1")

        engine = PaperEngine()

        is_active, reason = engine._is_ml_kill_switch_active()
        assert is_active is True
        assert reason == MlBlockReason.KILL_SWITCH_ENV

    def test_kill_switch_config_returns_reason(self) -> None:
        """ml_kill_switch=True returns KILL_SWITCH_CONFIG reason."""
        engine = PaperEngine(ml_kill_switch=True)

        is_active, reason = engine._is_ml_kill_switch_active()
        assert is_active is True
        assert reason == MlBlockReason.KILL_SWITCH_CONFIG


class TestMlActiveBlockedLogEvents:
    """Tests for ML_ACTIVE_BLOCKED log events."""

    def test_model_not_loaded_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """MODEL_NOT_LOADED reason logs ML_ACTIVE_BLOCKED at warning level."""
        engine = PaperEngine()
        engine._onnx_model = None

        policy_features: dict[str, Any] = {"mid_price": 50000}

        with caplog.at_level(logging.WARNING):
            result = engine._run_active_inference(1000, "BTCUSDT", policy_features)

        assert result is False
        assert "ML_ACTIVE_BLOCKED" in caplog.text
        assert "reason=MODEL_NOT_LOADED" in caplog.text

    def test_prediction_none_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """PREDICTION_NONE logs ML_ACTIVE_BLOCKED at warning level."""
        engine = PaperEngine()

        # Mock model that returns None prediction
        mock_model = MagicMock()
        mock_model.predict.return_value = None
        engine._onnx_model = mock_model

        policy_features: dict[str, Any] = {"mid_price": 50000}

        with caplog.at_level(logging.WARNING):
            result = engine._run_active_inference(1000, "BTCUSDT", policy_features)

        assert result is False
        assert "ML_ACTIVE_BLOCKED" in caplog.text
        assert "reason=PREDICTION_NONE" in caplog.text
        assert "latency_ms=" in caplog.text


class TestMlInferOkLogEvents:
    """Tests for ML_INFER_OK log events."""

    def test_successful_inference_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """Successful inference logs ML_INFER_OK at info level."""
        engine = PaperEngine()
        engine._onnx_artifact_dir = "/test/artifact/dir"

        # Mock successful prediction
        mock_prediction = MagicMock()
        mock_prediction.predicted_regime = "trending"
        mock_prediction.regime_probs_bps = [3000, 5000, 2000]
        mock_prediction.spacing_multiplier_x1000 = 1200
        mock_prediction.to_policy_features.return_value = {
            "ml_regime": 1,
            "ml_spacing_x1000": 1200,
        }

        mock_model = MagicMock()
        mock_model.predict.return_value = mock_prediction
        engine._onnx_model = mock_model

        policy_features: dict[str, Any] = {"mid_price": 50000}

        with caplog.at_level(logging.INFO):
            result = engine._run_active_inference(1000, "BTCUSDT", policy_features)

        assert result is True
        assert "ML_INFER_OK" in caplog.text
        assert "regime=trending" in caplog.text
        assert "latency_ms=" in caplog.text
        assert "artifact_dir=/test/artifact/dir" in caplog.text


class TestMlInferErrorLogEvents:
    """Tests for ML_INFER_ERROR log events."""

    def test_inference_exception_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """Inference exception logs ML_INFER_ERROR at error level."""
        engine = PaperEngine()
        engine._onnx_artifact_dir = "/test/artifact/dir"

        # Mock model that raises exception
        mock_model = MagicMock()
        mock_model.predict.side_effect = RuntimeError("Model execution failed")
        engine._onnx_model = mock_model

        policy_features: dict[str, Any] = {"mid_price": 50000}

        with caplog.at_level(logging.ERROR):
            result = engine._run_active_inference(1000, "BTCUSDT", policy_features)

        assert result is False
        assert "ML_INFER_ERROR" in caplog.text
        assert "error=Model execution failed" in caplog.text
        assert "latency_ms=" in caplog.text
        assert "artifact_dir=/test/artifact/dir" in caplog.text


class TestReasonCodePriority:
    """Tests for truth table reason code priority."""

    @pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
    def test_kill_switch_has_highest_priority(
        self, monkeypatch: pytest.MonkeyPatch, ml_registry_for_active: tuple[str, str, str]
    ) -> None:
        """Kill-switch reason takes priority over other reasons."""
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

        allowed, reason = engine._is_ml_active_allowed(1000)

        assert allowed is False
        assert reason == MlBlockReason.KILL_SWITCH_ENV

    def test_infer_disabled_before_active_disabled(self) -> None:
        """INFER_DISABLED reason comes before ACTIVE_DISABLED."""
        engine = PaperEngine(
            ml_infer_enabled=False,
            ml_active_enabled=False,
        )

        allowed, reason = engine._is_ml_active_allowed(1000)

        assert allowed is False
        assert reason == MlBlockReason.INFER_DISABLED

    @pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
    def test_model_not_loaded_checked_after_config(
        self, ml_registry_for_active: tuple[str, str, str]
    ) -> None:
        """MODEL_NOT_LOADED only checked if config conditions pass."""
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
        # Explicitly set model to None to test MODEL_NOT_LOADED
        engine._onnx_model = None

        allowed, reason = engine._is_ml_active_allowed(1000)

        assert allowed is False
        assert reason == MlBlockReason.MODEL_NOT_LOADED
