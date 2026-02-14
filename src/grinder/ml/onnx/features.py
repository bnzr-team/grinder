"""Feature vectorization for ONNX models.

M8-02b: Convert policy_features dict to fixed-order numpy array.

SSOT: FEATURE_ORDER defines the exact order of features expected by models.
Missing features are filled with 0.0.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# SSOT: Feature order for ONNX model input
# This must match the order used during model training
FEATURE_ORDER: tuple[str, ...] = (
    # Price features
    "price_mid",
    "price_bid",
    "price_ask",
    "spread_bps",
    # Volume features
    "volume_24h",
    "volume_1h",
    # Volatility features
    "volatility_1h_bps",
    "volatility_24h_bps",
    # Position features
    "position_size",
    "position_notional",
    "position_pnl_bps",
    # Grid state features
    "grid_levels_active",
    "grid_utilization_pct",
    # Market regime indicators
    "trend_strength",
    "momentum_1h",
)


def vectorize(policy_features: dict[str, Any]) -> np.ndarray:
    """Convert policy_features dict to fixed-order float array.

    Args:
        policy_features: Dict of feature name -> value.

    Returns:
        1D numpy array of shape (len(FEATURE_ORDER),) with float32 values.
        Missing features are filled with 0.0.

    Example:
        >>> features = {"price_mid": 50000.0, "spread_bps": 5}
        >>> vec = vectorize(features)
        >>> vec.shape
        (15,)
    """
    result = np.zeros(len(FEATURE_ORDER), dtype=np.float32)

    for i, name in enumerate(FEATURE_ORDER):
        if name in policy_features:
            value = policy_features[name]
            # Convert to float, handling various numeric types
            if isinstance(value, (int, float, np.number)):
                result[i] = float(value)

    return result


def unvectorize(arr: np.ndarray) -> dict[str, float]:
    """Convert feature array back to dict (for debugging).

    Args:
        arr: 1D array of shape (len(FEATURE_ORDER),).

    Returns:
        Dict mapping feature names to values.
    """
    if len(arr) != len(FEATURE_ORDER):
        raise ValueError(f"Array length {len(arr)} != expected {len(FEATURE_ORDER)}")
    return {name: float(arr[i]) for i, name in enumerate(FEATURE_ORDER)}


__all__ = [
    "FEATURE_ORDER",
    "unvectorize",
    "vectorize",
]
