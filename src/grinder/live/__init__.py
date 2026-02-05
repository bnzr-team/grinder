"""Live trading engine module.

Provides:
- LiveEngineV0 for live write-path wiring (ADR-036)
- LiveFeed for read-only data pipeline (ADR-037)

Write-path (LiveEngineV0):
- PaperEngine → ExchangePort integration
- Safety gates (arming, mode, kill-switch, whitelist)
- DrawdownGuardV1 intent-based blocking
- H3/H4 integration via IdempotentExchangePort

Read-path (LiveFeed):
- WebSocket → Snapshot → FeatureEngine → features
- Strictly read-only (no execution imports)
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
from grinder.live.feed import LiveFeed, LiveFeedConfig, LiveFeedRunner
from grinder.live.types import (
    BookTickerData,
    LiveFeaturesUpdate,
    LiveFeedStats,
    WsMessage,
)

__all__ = [
    # Write-path (LC-05)
    "BlockReason",
    # Read-path (LC-06)
    "BookTickerData",
    "LiveAction",
    "LiveActionStatus",
    "LiveEngineConfig",
    "LiveEngineOutput",
    "LiveEngineV0",
    # Read-path (LC-06)
    "LiveFeaturesUpdate",
    "LiveFeed",
    "LiveFeedConfig",
    "LiveFeedRunner",
    "LiveFeedStats",
    "WsMessage",
    "classify_intent",
]
