"""ONNX Runtime wrapper with optional import.

M8-02b: Thin wrapper around onnxruntime.InferenceSession for:
- Optional import (graceful fallback when not installed)
- Easy mocking in tests
- Consistent error handling
"""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: TC003 (used at runtime in method signature)
from typing import Any

logger = logging.getLogger(__name__)

# Optional import - onnxruntime is in [ml] extras
try:
    import onnxruntime as ort  # type: ignore[import-not-found]

    ONNX_AVAILABLE = True
except ImportError:
    ort = None
    ONNX_AVAILABLE = False


class OnnxRuntimeError(Exception):
    """Error during ONNX runtime operations."""

    pass


class OnnxSession:
    """Thin wrapper around onnxruntime.InferenceSession.

    Provides:
    - Consistent initialization from file path
    - Input/output name discovery
    - Simple run() interface

    This wrapper exists to:
    1. Centralize onnxruntime imports
    2. Enable easy mocking in tests
    3. Provide consistent error messages
    """

    def __init__(self, model_path: Path) -> None:
        """Initialize ONNX session from model file.

        Args:
            model_path: Path to .onnx model file.

        Raises:
            OnnxRuntimeError: If onnxruntime not available or load fails.
        """
        if not ONNX_AVAILABLE:
            raise OnnxRuntimeError("onnxruntime not installed; install with: pip install .[ml]")

        self._model_path = model_path

        try:
            # Use CPU provider only for determinism
            sess_options = ort.SessionOptions()
            sess_options.inter_op_num_threads = 1
            sess_options.intra_op_num_threads = 1

            self._session = ort.InferenceSession(
                str(model_path),
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )

            # Cache input/output metadata
            self._input_names = [inp.name for inp in self._session.get_inputs()]
            self._output_names = [out.name for out in self._session.get_outputs()]

            logger.info(
                "ONNX session loaded: %s (inputs=%s, outputs=%s)",
                model_path.name,
                self._input_names,
                self._output_names,
            )

        except Exception as e:
            raise OnnxRuntimeError(f"Failed to load ONNX model: {e}") from e

    @property
    def input_names(self) -> list[str]:
        """Get input tensor names."""
        return self._input_names

    @property
    def output_names(self) -> list[str]:
        """Get output tensor names."""
        return self._output_names

    def run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Run inference with given inputs.

        Args:
            inputs: Dict mapping input names to numpy arrays.

        Returns:
            Dict mapping output names to numpy arrays.

        Raises:
            OnnxRuntimeError: If inference fails.
        """
        try:
            outputs = self._session.run(self._output_names, inputs)
            return dict(zip(self._output_names, outputs, strict=True))
        except Exception as e:
            raise OnnxRuntimeError(f"Inference failed: {e}") from e


__all__ = [
    "ONNX_AVAILABLE",
    "OnnxRuntimeError",
    "OnnxSession",
]
