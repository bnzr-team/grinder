"""Configuration for LiveEngineV0.

See ADR-036 for design decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from grinder.connectors.live_connector import SafeMode


@dataclass
class LiveEngineConfig:
    """Configuration for live engine wiring.

    Safety hierarchy (from ADR-036):
        1. armed=False (default) → engine blocks ALL writes, logs as DRY_RUN_ENGINE
        2. armed=True + mode≠LIVE_TRADE → writes blocked at port level
        3. armed=True + mode=LIVE_TRADE → writes allowed if other gates pass

    Attributes:
        armed: Master switch for write operations. False by default (nothing writes).
        mode: SafeMode from underlying port. Determines what operations are allowed.
        kill_switch_active: If True, blocks PLACE/REPLACE but allows CANCEL.
        symbol_whitelist: Symbols allowed to trade. Empty = all allowed.
    """

    armed: bool = False
    mode: SafeMode = SafeMode.READ_ONLY
    kill_switch_active: bool = False
    symbol_whitelist: list[str] = field(default_factory=list)

    def is_symbol_allowed(self, symbol: str) -> bool:
        """Check if symbol is in whitelist (empty = all allowed)."""
        if not self.symbol_whitelist:
            return True
        return symbol in self.symbol_whitelist

    def can_write(self) -> bool:
        """Check if writes are possible (armed + LIVE_TRADE mode)."""
        return self.armed and self.mode == SafeMode.LIVE_TRADE
