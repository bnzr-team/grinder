"""
GRINDER - Adaptive Grid Trading System for Crypto Perpetuals.

A market-making grid trading system that adapts to toxicity and volatility.
"""

__version__ = "0.1.0"
__author__ = "bnzr-hub"

from grinder.core import GridMode, SystemState

__all__ = ["GridMode", "SystemState", "__version__"]
