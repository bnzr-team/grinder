"""Live trading engine module.

Provides LiveEngineV0 for live write-path wiring:
- PaperEngine → ExchangePort integration
- Safety gates (arming, mode, kill-switch, whitelist)
- DrawdownGuardV1 intent-based blocking
- H3/H4 integration via IdempotentExchangePort

See: ADR-036 for design decisions
"""

from grinder.live.config import LiveEngineConfig
from grinder.live.engine import (
    BlockReason,
    LiveAction,
    LiveActionStatus,
    LiveEngineOutput,
    LiveEngineV0,
    classify_intent,
)

__all__ = [
    "BlockReason",
    "LiveAction",
    "LiveActionStatus",
    "LiveEngineConfig",
    "LiveEngineOutput",
    "LiveEngineV0",
    "classify_intent",
]
