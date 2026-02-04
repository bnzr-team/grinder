"""Order execution engine.

This module provides:
- ExchangePort: Protocol for exchange interactions
- NoOpExchangePort: Stub for replay/paper mode (no real exchange writes)
- ExecutionEngine: Grid order reconciliation and management
- ExecutionState: State tracking for orders
- ExecutionMetrics: Metrics collection

See: docs/09_EXECUTION_SPEC.md
"""

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
    "ActionType",
    "ExchangePort",
    "ExecutionAction",
    "ExecutionEngine",
    "ExecutionEvent",
    "ExecutionMetrics",
    "ExecutionResult",
    "ExecutionState",
    "GridLevel",
    "IdempotentExchangePort",
    "IdempotentPortStats",
    "NoOpExchangePort",
    "OrderRecord",
    "get_metrics",
    "reset_metrics",
]
