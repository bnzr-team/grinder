"""Metrics contract patterns (SSOT) - no external dependencies.

This module contains ONLY the contract patterns for /metrics validation.
It has NO imports from other grinder modules to avoid transitive dependencies
(e.g., redis via HA module).

Used by:
- live_contract.py (runtime contract validation)
- smoke_metrics_contract.py (CI smoke test)
"""

from __future__ import annotations

# Required metric patterns for contract validation
REQUIRED_HEALTHZ_KEYS = ["status", "uptime_s"]
REQUIRED_READYZ_KEYS = ["ready", "role"]

REQUIRED_METRICS_PATTERNS = [
    # System metrics
    "# HELP grinder_up",
    "# TYPE grinder_up",
    "grinder_up 1",
    "# HELP grinder_uptime_seconds",
    "# TYPE grinder_uptime_seconds",
    "grinder_uptime_seconds",
    # Gating metrics
    "# HELP grinder_gating_allowed_total",
    "# TYPE grinder_gating_allowed_total",
    "# HELP grinder_gating_blocked_total",
    "# TYPE grinder_gating_blocked_total",
    # Risk metrics
    "# HELP grinder_kill_switch_triggered",
    "# TYPE grinder_kill_switch_triggered",
    "grinder_kill_switch_triggered",
    "# HELP grinder_kill_switch_trips_total",
    "# TYPE grinder_kill_switch_trips_total",
    "# HELP grinder_drawdown_pct",
    "# TYPE grinder_drawdown_pct",
    "grinder_drawdown_pct",
    "# HELP grinder_high_water_mark",
    "# TYPE grinder_high_water_mark",
    "grinder_high_water_mark",
    # HA metrics
    "# HELP grinder_ha_role",
    "# TYPE grinder_ha_role",
    "grinder_ha_role",
    # Connector metrics (H2/H3/H4)
    "# HELP grinder_connector_retries_total",
    "# TYPE grinder_connector_retries_total",
    "# HELP grinder_idempotency_hits_total",
    "# TYPE grinder_idempotency_hits_total",
    "# HELP grinder_idempotency_conflicts_total",
    "# TYPE grinder_idempotency_conflicts_total",
    "# HELP grinder_idempotency_misses_total",
    "# TYPE grinder_idempotency_misses_total",
    "# HELP grinder_circuit_state",
    "# TYPE grinder_circuit_state",
    "# HELP grinder_circuit_rejected_total",
    "# TYPE grinder_circuit_rejected_total",
    "# HELP grinder_circuit_trips_total",
    "# TYPE grinder_circuit_trips_total",
    # Series-level patterns (H5-02): validate metrics emit series with correct label schema
    # These patterns verify the metric is emitted as a series (not just HELP/TYPE)
    # and uses the expected label keys (op, reason, state). Values are NOT checked.
    "grinder_connector_retries_total{op=",
    "grinder_idempotency_hits_total{op=",
    "grinder_idempotency_conflicts_total{op=",
    "grinder_idempotency_misses_total{op=",
    "grinder_circuit_state{op=",
    "grinder_circuit_rejected_total{op=",
    "grinder_circuit_trips_total{op=",
    # Reconcile metrics (LC-09b/LC-10/LC-11/LC-15b)
    "# HELP grinder_reconcile_mismatch_total",
    "# TYPE grinder_reconcile_mismatch_total",
    "# HELP grinder_reconcile_last_snapshot_age_seconds",
    "# TYPE grinder_reconcile_last_snapshot_age_seconds",
    "grinder_reconcile_last_snapshot_age_seconds",
    "# HELP grinder_reconcile_runs_total",
    "# TYPE grinder_reconcile_runs_total",
    "grinder_reconcile_runs_total",
    "# HELP grinder_reconcile_action_planned_total",
    "# TYPE grinder_reconcile_action_planned_total",
    "# HELP grinder_reconcile_action_executed_total",
    "# TYPE grinder_reconcile_action_executed_total",
    "# HELP grinder_reconcile_action_blocked_total",
    "# TYPE grinder_reconcile_action_blocked_total",
    "# HELP grinder_reconcile_runs_with_mismatch_total",
    "# TYPE grinder_reconcile_runs_with_mismatch_total",
    "grinder_reconcile_runs_with_mismatch_total",
    "# HELP grinder_reconcile_runs_with_remediation_total",
    "# TYPE grinder_reconcile_runs_with_remediation_total",
    "# HELP grinder_reconcile_last_remediation_ts_ms",
    "# TYPE grinder_reconcile_last_remediation_ts_ms",
    "grinder_reconcile_last_remediation_ts_ms",
    # Reconcile series-level patterns (type/action labels)
    "grinder_reconcile_mismatch_total{type=",
    "grinder_reconcile_action_planned_total{action=",
    "grinder_reconcile_action_executed_total{action=",
    "grinder_reconcile_runs_with_remediation_total{action=",
    # LC-18: Budget metrics
    "# HELP grinder_reconcile_budget_calls_used_day",
    "# TYPE grinder_reconcile_budget_calls_used_day",
    "grinder_reconcile_budget_calls_used_day",
    "# HELP grinder_reconcile_budget_notional_used_day",
    "# TYPE grinder_reconcile_budget_notional_used_day",
    "grinder_reconcile_budget_notional_used_day",
    "# HELP grinder_reconcile_budget_calls_remaining_day",
    "# TYPE grinder_reconcile_budget_calls_remaining_day",
    "grinder_reconcile_budget_calls_remaining_day",
    "# HELP grinder_reconcile_budget_notional_remaining_day",
    "# TYPE grinder_reconcile_budget_notional_remaining_day",
    "grinder_reconcile_budget_notional_remaining_day",
]

# Forbidden high-cardinality labels (H5-02 contract tightening)
# These labels MUST NOT appear in /metrics output to prevent cardinality explosion.
# See ADR-028 for design decisions.
FORBIDDEN_METRIC_LABELS = [
    "symbol=",
    "order_id=",
    "key=",
    "client_id=",
    "idempotency_key=",
]
