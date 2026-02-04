"""Order execution engine.

This module provides:
- ExchangePort: Protocol for exchange interactions
- NoOpExchangePort: Stub for replay/paper mode (no real exchange writes)
- ExecutionEngine: Grid order reconciliation and management
- ExecutionState: State tracking for orders
- ExecutionMetrics: Metrics collection

See: docs/09_EXECUTION_SPEC.md
"""

from grinder.execution.binance_port import (
    BINANCE_SPOT_TESTNET_URL,
    BinanceExchangePort,
    BinanceExchangePortConfig,
    HttpClient,
    HttpResponse,
    NoopHttpClient,
    map_binance_error,
)
from grinder.execution.engine import ExecutionEngine, ExecutionResult, GridLevel
from grinder.execution.idempotent_port import IdempotentExchangePort, IdempotentPortStats
from grinder.execution.metrics import ExecutionMetrics, get_metrics, reset_metrics
from grinder.execution.port import ExchangePort, NoOpExchangePort
from grinder.execution.types import (
    ActionType,
    ExecutionAction,
    ExecutionEvent,
    ExecutionState,
    OrderRecord,
)

__all__ = [
    "BINANCE_SPOT_TESTNET_URL",
    "ActionType",
    "BinanceExchangePort",
    "BinanceExchangePortConfig",
    "ExchangePort",
    "ExecutionAction",
    "ExecutionEngine",
    "ExecutionEvent",
    "ExecutionMetrics",
    "ExecutionResult",
    "ExecutionState",
    "GridLevel",
    "HttpClient",
    "HttpResponse",
    "IdempotentExchangePort",
    "IdempotentPortStats",
    "NoOpExchangePort",
    "NoopHttpClient",
    "OrderRecord",
    "get_metrics",
    "map_binance_error",
    "reset_metrics",
]
