"""ONNX model implementation.

M8-02b: OnnxMlModel produces MlSignalSnapshot from policy features.

Soft-fail behavior:
- Errors during predict() return None (not raise)
- Errors are logged and counted in metrics
- This ensures the trading loop continues even if ML fails
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from grinder.ml import PROBS_SUM_BPS, MlSignalSnapshot

from .artifact import load_artifact
from .features import vectorize
from .runtime import ONNX_AVAILABLE, OnnxRuntimeError, OnnxSession

if TYPE_CHECKING:
    from .types import OnnxArtifact

logger = logging.getLogger(__name__)


class OnnxModelError(Exception):
    """Error during ONNX model operations."""

    pass


class OnnxMlModel:
    """ONNX-based ML model for regime prediction.

    Produces MlSignalSnapshot from policy_features dict.

    Model contract:
    - Input: 1D float array of shape (len(FEATURE_ORDER),)
    - Output: Dict with:
      - "regime_probs": array of shape (3,) for [LOW, MID, HIGH] probabilities
      - "spacing_multiplier": scalar spacing adjustment

    Probabilities are normalized to sum to 10000 bps.
    Spacing is scaled by 1000 (1.0 -> 1000).
    """

    def __init__(
        self,
        artifact: OnnxArtifact,
        session: OnnxSession,
    ) -> None:
        """Initialize model with validated artifact and session.

        Use load_from_dir() for normal initialization.
        """
        self._artifact = artifact
        self._session = session

        # Stats for logging/metrics
        self._predict_count = 0
        self._predict_errors = 0

    @classmethod
    def load_from_dir(cls, artifact_dir: Path | str) -> OnnxMlModel:
        """Load model from artifact directory.

        Args:
            artifact_dir: Path to artifact directory with manifest.json.

        Returns:
            Initialized OnnxMlModel.

        Raises:
            OnnxArtifactError: If artifact validation fails.
            OnnxRuntimeError: If model loading fails.
        """
        if not ONNX_AVAILABLE:
            raise OnnxRuntimeError("onnxruntime not installed; install with: pip install .[ml]")

        artifact_dir = Path(artifact_dir)
        logger.info("Loading ONNX model from %s", artifact_dir)

        # Load and validate artifact
        artifact = load_artifact(artifact_dir)

        # Create ONNX session
        session = OnnxSession(artifact.model_path)

        return cls(artifact=artifact, session=session)

    @property
    def artifact(self) -> OnnxArtifact:
        """Get the loaded artifact."""
        return self._artifact

    @property
    def stats(self) -> dict[str, int]:
        """Get prediction statistics."""
        return {
            "predict_count": self._predict_count,
            "predict_errors": self._predict_errors,
        }

    def predict(
        self,
        ts_ms: int,
        symbol: str,
        policy_features: dict[str, Any],
    ) -> MlSignalSnapshot | None:
        """Run inference and return MlSignalSnapshot.

        Soft-fail: Returns None on any error (logs + increments counter).

        Args:
            ts_ms: Timestamp in milliseconds.
            symbol: Trading symbol.
            policy_features: Dict of feature name -> value.

        Returns:
            MlSignalSnapshot or None if inference fails.
        """
        start_time = time.perf_counter()
        self._predict_count += 1

        try:
            # Vectorize features
            features = vectorize(policy_features)

            # Reshape for batch dimension: (1, n_features)
            input_array = features.reshape(1, -1)

            # Run inference
            outputs = self._session.run({"input": input_array})

            # Parse outputs
            result = self._parse_outputs(ts_ms, symbol, outputs)

            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                "ONNX prediction: symbol=%s regime=%s latency_ms=%.2f",
                symbol,
                result.predicted_regime,
                latency_ms,
            )

            return result

        except Exception as e:
            self._predict_errors += 1
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.warning(
                "ONNX prediction failed: symbol=%s error=%s latency_ms=%.2f",
                symbol,
                str(e),
                latency_ms,
            )
            return None

    def _parse_outputs(
        self,
        ts_ms: int,
        symbol: str,
        outputs: dict[str, Any],
    ) -> MlSignalSnapshot:
        """Parse ONNX outputs into MlSignalSnapshot.

        Args:
            ts_ms: Timestamp.
            symbol: Trading symbol.
            outputs: Dict of output name -> numpy array.

        Returns:
            MlSignalSnapshot.

        Raises:
            OnnxModelError: If output format is invalid.
        """
        # Extract regime probabilities
        if "regime_probs" not in outputs:
            raise OnnxModelError("Model output missing 'regime_probs'")

        probs_raw = outputs["regime_probs"]
        if hasattr(probs_raw, "flatten"):
            probs_raw = probs_raw.flatten()

        if len(probs_raw) != 3:
            raise OnnxModelError(f"regime_probs must have 3 values, got {len(probs_raw)}")

        # Normalize to bps (sum = 10000)
        probs_sum = float(np.sum(probs_raw))
        if probs_sum <= 0:
            raise OnnxModelError(f"regime_probs sum must be positive, got {probs_sum}")

        probs_normalized = [p / probs_sum for p in probs_raw]
        regime_probs_bps = {
            "LOW": round(probs_normalized[0] * PROBS_SUM_BPS),
            "MID": round(probs_normalized[1] * PROBS_SUM_BPS),
            "HIGH": round(probs_normalized[2] * PROBS_SUM_BPS),
        }

        # Fix rounding to ensure sum = 10000
        diff = PROBS_SUM_BPS - sum(regime_probs_bps.values())
        if diff != 0:
            # Add difference to largest probability
            max_key = max(regime_probs_bps, key=lambda k: regime_probs_bps[k])
            regime_probs_bps[max_key] += diff

        # Determine predicted regime (argmax)
        regime_order = ["LOW", "MID", "HIGH"]
        predicted_idx = int(np.argmax(probs_raw))
        predicted_regime = regime_order[predicted_idx]

        # Extract spacing multiplier
        if "spacing_multiplier" in outputs:
            spacing_raw = float(outputs["spacing_multiplier"].flatten()[0])
        else:
            spacing_raw = 1.0  # Default to no adjustment

        # Scale to x1000 representation
        spacing_multiplier_x1000 = max(1, round(spacing_raw * 1000))

        return MlSignalSnapshot(
            ts_ms=ts_ms,
            symbol=symbol,
            regime_probs_bps=regime_probs_bps,
            predicted_regime=predicted_regime,
            spacing_multiplier_x1000=spacing_multiplier_x1000,
        )


__all__ = [
    "OnnxMlModel",
    "OnnxModelError",
]
