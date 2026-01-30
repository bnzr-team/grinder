"""GRINDER - Adaptive Grid Trading System for Crypto Perpetuals.

A market-making grid trading system that adapts to toxicity and volatility.

Note: version is sourced from package metadata (pyproject.toml).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from grinder.core import GridMode, SystemState


def _pkg_version() -> str:
    try:
        return version("grinder")
    except PackageNotFoundError:
        return "0.0.0"


__version__ = _pkg_version()
__author__ = "bnzr-hub"

__all__ = ["GridMode", "SystemState", "__version__"]
