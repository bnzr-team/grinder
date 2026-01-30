"""Pytest configuration and fixtures."""

from typing import Any

import pytest


@pytest.fixture
def sample_features() -> dict[str, Any]:
    """Sample feature dict for testing."""
    return {
        "mid": 50000.0,
        "spread_bps": 2.0,
        "natr_14_5m": 0.005,
        "tox_score": 0.5,
        "ofi_zscore": 0.2,
        "cvd_zscore": 0.1,
        "vpin": 0.4,
        "funding_rate": 0.0001,
        "oi_change_pct": 0.02,
    }


@pytest.fixture
def high_toxicity_features(sample_features: dict[str, Any]) -> dict[str, Any]:
    """Features with high toxicity."""
    return {
        **sample_features,
        "tox_score": 2.5,
        "vpin": 0.8,
        "ofi_zscore": 2.5,
    }
