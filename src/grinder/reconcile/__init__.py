"""Reconciliation module for detecting state mismatches and active remediation.

See ADR-042 for passive reconciliation design decisions.
See ADR-043 for active remediation design decisions.
"""

from grinder.reconcile.config import ReconcileConfig, RemediationAction
from grinder.reconcile.engine import ReconcileEngine
from grinder.reconcile.expected_state import ExpectedStateStore
from grinder.reconcile.metrics import (
    ReconcileMetrics,
    get_reconcile_metrics,
    reset_reconcile_metrics,
)
from grinder.reconcile.observed_state import ObservedStateStore
from grinder.reconcile.remediation import (
    GRINDER_PREFIX,
    RemediationBlockReason,
    RemediationExecutor,
    RemediationResult,
    RemediationStatus,
)
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
    "GRINDER_PREFIX",
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
    "RemediationAction",
    "RemediationBlockReason",
    "RemediationExecutor",
    "RemediationResult",
    "RemediationStatus",
    "SnapshotClient",
    "SnapshotClientConfig",
    "get_reconcile_metrics",
    "reset_reconcile_metrics",
]
