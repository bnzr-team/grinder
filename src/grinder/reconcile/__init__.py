"""Reconciliation module for detecting state mismatches and active remediation.

See ADR-042 for passive reconciliation design decisions.
See ADR-043 for active remediation design decisions.
See ADR-044 for runner wiring and routing policy.
See ADR-045 for order identity design decisions.
"""

from grinder.reconcile.config import ReconcileConfig, RemediationAction
from grinder.reconcile.engine import ReconcileEngine
from grinder.reconcile.expected_state import ExpectedStateStore
from grinder.reconcile.identity import (
    DEFAULT_PREFIX,
    DEFAULT_STRATEGY_ID,
    LEGACY_STRATEGY_ID,
    OrderIdentityConfig,
    ParsedOrderId,
    generate_client_order_id,
    get_default_identity_config,
    is_ours,
    parse_client_order_id,
    reset_default_identity_config,
    set_default_identity_config,
)
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
from grinder.reconcile.runner import (
    ACTIONABLE_STATUSES,
    MISMATCH_PRIORITY,
    NO_ACTION_MISMATCHES,
    ORDER_MISMATCHES_FOR_CANCEL,
    POSITION_MISMATCHES_FOR_FLATTEN,
    TERMINAL_STATUSES,
    ReconcileRunner,
    ReconcileRunReport,
)

# NOTE: SnapshotClient is NOT exported here to avoid circular import with execution.binance_port.
# Import directly: from grinder.reconcile.snapshot_client import SnapshotClient, SnapshotClientConfig
from grinder.reconcile.types import (
    ExpectedOrder,
    ExpectedPosition,
    Mismatch,
    MismatchType,
    ObservedOrder,
    ObservedPosition,
)

__all__ = [
    # Constants
    "ACTIONABLE_STATUSES",
    "DEFAULT_PREFIX",
    "DEFAULT_STRATEGY_ID",
    "GRINDER_PREFIX",
    "LEGACY_STRATEGY_ID",
    "MISMATCH_PRIORITY",
    "NO_ACTION_MISMATCHES",
    "ORDER_MISMATCHES_FOR_CANCEL",
    "POSITION_MISMATCHES_FOR_FLATTEN",
    "TERMINAL_STATUSES",
    # Types
    "ExpectedOrder",
    "ExpectedPosition",
    "ExpectedStateStore",
    "Mismatch",
    "MismatchType",
    "ObservedOrder",
    "ObservedPosition",
    "ObservedStateStore",
    "OrderIdentityConfig",
    "ParsedOrderId",
    "ReconcileConfig",
    "ReconcileEngine",
    "ReconcileMetrics",
    "ReconcileRunReport",
    "ReconcileRunner",
    "RemediationAction",
    "RemediationBlockReason",
    "RemediationExecutor",
    "RemediationResult",
    "RemediationStatus",
    # NOTE: SnapshotClient/Config not exported here - import from grinder.reconcile.snapshot_client
    # Functions
    "generate_client_order_id",
    "get_default_identity_config",
    "get_reconcile_metrics",
    "is_ours",
    "parse_client_order_id",
    "reset_default_identity_config",
    "reset_reconcile_metrics",
    "set_default_identity_config",
]
