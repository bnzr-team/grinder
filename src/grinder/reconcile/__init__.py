"""Reconciliation module for detecting state mismatches.

See ADR-042 for design decisions.
"""

from grinder.reconcile.config import ReconcileConfig
from grinder.reconcile.engine import ReconcileEngine
from grinder.reconcile.expected_state import ExpectedStateStore
from grinder.reconcile.metrics import (
    ReconcileMetrics,
    get_reconcile_metrics,
    reset_reconcile_metrics,
)
from grinder.reconcile.observed_state import ObservedStateStore
from grinder.reconcile.snapshot_client import SnapshotClient, SnapshotClientConfig
from grinder.reconcile.types import (
    ExpectedOrder,
    ExpectedPosition,
    Mismatch,
    MismatchType,
    ObservedOrder,
    ObservedPosition,
)

__all__ = [
    "ExpectedOrder",
    "ExpectedPosition",
    "ExpectedStateStore",
    "Mismatch",
    "MismatchType",
    "ObservedOrder",
    "ObservedPosition",
    "ObservedStateStore",
    "ReconcileConfig",
    "ReconcileEngine",
    "ReconcileMetrics",
    "SnapshotClient",
    "SnapshotClientConfig",
    "get_reconcile_metrics",
    "reset_reconcile_metrics",
]
