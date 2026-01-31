"""Prefilter constants and thresholds.

Default values from docs/15_CONSTANTS.md §1.1 Hard Gates.
"""

# Hard gate thresholds
SPREAD_MAX_BPS: float = 15.0  # Max bid-ask spread in basis points
VOL_MIN_24H_USD: float = 10_000_000  # Min 24h volume in USD
VOL_MIN_1H_USD: float = 500_000  # Min 1h volume in USD
TRADE_COUNT_MIN_1M: int = 100  # Min trades per minute
OI_MIN_USD: float = 5_000_000  # Min open interest in USD

# Blacklist (symbols to always reject)
BLACKLIST: frozenset[str] = frozenset()
