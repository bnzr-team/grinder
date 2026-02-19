#!/usr/bin/env python3
"""Live reconcile runner with env-configurable LC-18 settings.

Operator entrypoint for running reconciliation with staged rollout modes.
All LC-18 fields are configurable via environment variables.

SAFETY:
- Default mode is DETECT_ONLY (no planning, no execution)
- EXECUTE modes require ALLOW_MAINNET_TRADE=1
- Invalid env config exits with code 2 (config error)

Exit Codes:
    0 - Success
    2 - Config error (env parse / unsafe execute without ALLOW_MAINNET_TRADE=1)
    3 - Connection/runtime error

Usage:
    # Stage A: Detect-only (default, safest)
    PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60

    # Stage B: Plan-only (logs plans but 0 port calls)
    REMEDIATION_MODE=plan_only \\
    PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60

    # Stage D: Execute cancel-only (requires ALLOW_MAINNET_TRADE=1)
    REMEDIATION_MODE=execute_cancel_all \\
    REMEDIATION_STRATEGY_ALLOWLIST=default \\
    ALLOW_MAINNET_TRADE=1 \\
    PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60

    # With artifacts run-dir (M4.1):
    GRINDER_ARTIFACTS_DIR=/var/lib/grinder/artifacts \\
    PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60
    # Creates: /var/lib/grinder/artifacts/YYYY-MM-DD/run_<ts>/{stdout.log,audit.jsonl,...}

    # First run (clean budget) - use --reset-budget-state:
    BUDGET_STATE_PATH=/var/lib/grinder/budget.json \\
    PYTHONPATH=src python3 -m scripts.run_live_reconcile --reset-budget-state --duration 60

    # Multi-run (persist budget) - no reset flag:
    BUDGET_STATE_PATH=/var/lib/grinder/budget.json \\
    PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60

Environment Variables:
    REMEDIATION_MODE              detect_only|plan_only|blocked|execute_cancel_all|execute_flatten
    ALLOW_MAINNET_TRADE           Must be "1" for EXECUTE_* modes (default: "0")
    REMEDIATION_STRATEGY_ALLOWLIST   CSV strategy IDs (empty = allow all)
    REMEDIATION_SYMBOL_ALLOWLIST     CSV symbols (empty = allow all)
    MAX_CALLS_PER_DAY             Max calls per day (default: 100)
    MAX_NOTIONAL_PER_DAY          Max notional USDT per day (default: 5000)
    MAX_CALLS_PER_RUN             Max calls per run (default: 10)
    MAX_NOTIONAL_PER_RUN          Max notional per run (default: 1000)
    FLATTEN_MAX_NOTIONAL_PER_CALL Max notional for single flatten (default: 500)
    BUDGET_STATE_PATH             Path to persist daily budget (default: None)
    BUDGET_STATE_STALE_HOURS      Hours before stale warning (default: 24)
    GRINDER_ARTIFACTS_DIR         Base artifacts directory (enables run-dir mode)
    GRINDER_ARTIFACT_TTL_DAYS     Days to keep old run-dirs (default: 14)
    LATENCY_RETRY_ENABLED         "1" to enable per-op HTTP deadlines + retries (default: off)
    HTTP_MAX_ATTEMPTS_READ        Max attempts for read ops (default: 1)
    HTTP_MAX_ATTEMPTS_WRITE       Max attempts for write ops (default: 1)
    HTTP_DEADLINE_<OP>_MS         Per-op deadline override (e.g. HTTP_DEADLINE_CANCEL_ORDER_MS=400)
    FILL_CURSOR_PATH              Path to persist fill cursor (default: None = in-memory only)
    FILL_INGEST_ENABLED           "1" to enable fill ingestion from userTrades (default: off)

See ADR-052 for LC-18 design decisions.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

try:
    import requests
except ImportError:
    requests = None
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grinder.core import OrderSide
    from grinder.net.measured_sync import MeasuredSyncHttpClient
    from grinder.reconcile.metrics import ReconcileMetrics

# Real port imports
from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.connectors.live_connector import SafeMode
from grinder.execution.binance_futures_port import (
    BINANCE_FUTURES_MAINNET_URL,
    BinanceFuturesPort,
    BinanceFuturesPortConfig,
)
from grinder.execution.fill_cursor import FillCursor, load_fill_cursor, save_fill_cursor
from grinder.execution.fill_ingest import ingest_fills, push_tracker_to_metrics
from grinder.execution.fill_tracker import FillTracker
from grinder.live.reconcile_loop import ReconcileLoop, ReconcileLoopConfig
from grinder.observability.fill_metrics import get_fill_metrics
from grinder.ops.artifacts import (
    ArtifactPaths,
    cleanup_old_runs,
    copy_budget_state,
    ensure_run_dir,
    load_artifact_config_from_env,
    resolve_artifact_paths,
    write_stdout_summary,
)
from grinder.ops.budget import check_budget_state_stale, reset_budget_state
from grinder.reconcile.audit import AuditConfig, AuditWriter
from grinder.reconcile.config import ReconcileConfig, RemediationAction, RemediationMode
from grinder.reconcile.engine import ReconcileEngine
from grinder.reconcile.expected_state import ExpectedStateStore
from grinder.reconcile.identity import OrderIdentityConfig
from grinder.reconcile.metrics import get_reconcile_metrics
from grinder.reconcile.observed_state import ObservedStateStore
from grinder.reconcile.remediation import RemediationExecutor
from grinder.reconcile.runner import ReconcileRunner
from grinder.reconcile.types import ExpectedPosition
from scripts.http_measured_client import RequestsHttpClient, build_measured_client

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================================
# EXIT CODES
# =============================================================================

EXIT_SUCCESS = 0
EXIT_CONFIG_ERROR = 2
EXIT_RUNTIME_ERROR = 3


# =============================================================================
# ENV VAR LOADING
# =============================================================================


class ConfigError(Exception):
    """Configuration error from invalid environment variables."""


def _parse_mode(value: str) -> RemediationMode:
    """Parse REMEDIATION_MODE env var."""
    mode_map = {
        "detect_only": RemediationMode.DETECT_ONLY,
        "plan_only": RemediationMode.PLAN_ONLY,
        "blocked": RemediationMode.BLOCKED,
        "execute_cancel_all": RemediationMode.EXECUTE_CANCEL_ALL,
        "execute_flatten": RemediationMode.EXECUTE_FLATTEN,
    }
    lower = value.lower().strip()
    if lower not in mode_map:
        valid = ", ".join(mode_map.keys())
        raise ConfigError(f"Invalid REMEDIATION_MODE='{value}'. Valid: {valid}")
    return mode_map[lower]


def _parse_csv_set(value: str) -> set[str]:
    """Parse comma-separated values into a set."""
    if not value.strip():
        return set()
    return {v.strip() for v in value.split(",") if v.strip()}


def _parse_int(name: str, value: str, default: int) -> int:
    """Parse integer env var with default."""
    if not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"Invalid {name}='{value}'. Must be integer.") from None


def _parse_decimal(name: str, value: str, default: str) -> Decimal:
    """Parse Decimal env var with default."""
    if not value.strip():
        return Decimal(default)
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ConfigError(f"Invalid {name}='{value}'. Must be decimal number.") from None


def load_reconcile_config_from_env() -> ReconcileConfig:
    """Load ReconcileConfig from environment variables.

    Returns:
        ReconcileConfig populated from env vars with safe defaults.

    Raises:
        ConfigError: If any env var has invalid value.
    """
    # Mode (default: detect_only)
    mode_str = os.environ.get("REMEDIATION_MODE", "detect_only")
    mode = _parse_mode(mode_str)

    # Allowlists
    strategy_allowlist = _parse_csv_set(os.environ.get("REMEDIATION_STRATEGY_ALLOWLIST", ""))
    symbol_allowlist = _parse_csv_set(os.environ.get("REMEDIATION_SYMBOL_ALLOWLIST", ""))

    # Budgets
    max_calls_per_day = _parse_int(
        "MAX_CALLS_PER_DAY",
        os.environ.get("MAX_CALLS_PER_DAY", ""),
        default=100,
    )
    max_notional_per_day = _parse_decimal(
        "MAX_NOTIONAL_PER_DAY",
        os.environ.get("MAX_NOTIONAL_PER_DAY", ""),
        default="5000",
    )
    max_calls_per_run = _parse_int(
        "MAX_CALLS_PER_RUN",
        os.environ.get("MAX_CALLS_PER_RUN", ""),
        default=10,
    )
    max_notional_per_run = _parse_decimal(
        "MAX_NOTIONAL_PER_RUN",
        os.environ.get("MAX_NOTIONAL_PER_RUN", ""),
        default="1000",
    )
    flatten_max_notional = _parse_decimal(
        "FLATTEN_MAX_NOTIONAL_PER_CALL",
        os.environ.get("FLATTEN_MAX_NOTIONAL_PER_CALL", ""),
        default="500",
    )

    # Persistence
    budget_state_path = os.environ.get("BUDGET_STATE_PATH", "").strip() or None

    # Determine action based on mode
    if mode == RemediationMode.EXECUTE_CANCEL_ALL:
        action = RemediationAction.CANCEL_ALL
        dry_run = False
        allow_active = True
    elif mode == RemediationMode.EXECUTE_FLATTEN:
        action = RemediationAction.FLATTEN
        dry_run = False
        allow_active = True
    else:
        action = RemediationAction.NONE
        dry_run = True
        allow_active = False

    return ReconcileConfig(
        action=action,
        dry_run=dry_run,
        allow_active_remediation=allow_active,
        remediation_mode=mode,
        remediation_strategy_allowlist=strategy_allowlist,
        remediation_symbol_allowlist=symbol_allowlist,
        max_calls_per_day=max_calls_per_day,
        max_notional_per_day=max_notional_per_day,
        max_calls_per_run=max_calls_per_run,
        max_notional_per_run=max_notional_per_run,
        flatten_max_notional_per_call=flatten_max_notional,
        budget_state_path=budget_state_path,
    )


def validate_safety_requirements(config: ReconcileConfig) -> None:
    """Validate that safety requirements are met for execute modes.

    Raises:
        ConfigError: If EXECUTE mode without ALLOW_MAINNET_TRADE=1.
    """
    is_execute_mode = config.remediation_mode in (
        RemediationMode.EXECUTE_CANCEL_ALL,
        RemediationMode.EXECUTE_FLATTEN,
    )

    if is_execute_mode:
        allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "0")
        if allow_mainnet != "1":
            raise ConfigError("execute mode requires ALLOW_MAINNET_TRADE=1")


# =============================================================================
# FAKE PORT (for testing without real exchange)
# =============================================================================


@dataclass
class FakePort:
    """Fake exchange port for testing (no real HTTP)."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def cancel_order(self, client_order_id: str) -> bool:
        """Record cancel call."""
        self.calls.append(
            {
                "method": "cancel_order",
                "client_order_id": client_order_id,
                "ts": int(time.time() * 1000),
            }
        )
        return True

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: Decimal,
        reduce_only: bool = False,
    ) -> str:
        """Record market order call."""
        self.calls.append(
            {
                "method": "place_market_order",
                "symbol": symbol,
                "side": side.value,
                "qty": str(qty),
                "reduce_only": reduce_only,
                "ts": int(time.time() * 1000),
            }
        )
        return f"fake_order_{int(time.time() * 1000)}"

    def fetch_user_trades_raw(
        self,
        symbol: str,  # noqa: ARG002
        *,
        start_time_ms: int | None = None,  # noqa: ARG002
        from_id: int | None = None,  # noqa: ARG002
        limit: int = 500,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """Return empty trades (FakePort has no real exchange)."""
        return []


# =============================================================================
# REAL PORT (for real exchange calls)
# =============================================================================


def validate_credentials() -> tuple[str, str]:
    """Validate and return API credentials.

    Returns:
        Tuple of (api_key, api_secret)

    Raises:
        ConfigError: If credentials are missing.
    """
    api_key = os.environ.get("BINANCE_API_KEY", "").strip()
    api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()

    if not api_key:
        raise ConfigError("BINANCE_API_KEY not set (required for real port)")
    if not api_secret:
        raise ConfigError("BINANCE_API_SECRET not set (required for real port)")

    return api_key, api_secret


def _build_measured_client(inner: RequestsHttpClient) -> MeasuredSyncHttpClient:
    """Wrap inner HttpClient with MeasuredSyncHttpClient if LATENCY_RETRY_ENABLED=1.

    Delegates to shared factory in scripts.http_measured_client (Launch-05c).
    """
    return build_measured_client(inner)


def create_real_port(
    symbol_whitelist: list[str],
    dry_run: bool,
    max_notional_per_order: Decimal = Decimal("500"),
    identity_config: OrderIdentityConfig | None = None,
) -> BinanceFuturesPort:
    """Create a real BinanceFuturesPort with validated credentials.

    Args:
        symbol_whitelist: Symbols to allow trading.
        dry_run: If True, port is in dry-run mode.
        max_notional_per_order: Max notional per order (safety cap).

    Returns:
        Configured BinanceFuturesPort.

    Raises:
        ConfigError: If credentials are missing or invalid.
    """
    api_key, api_secret = validate_credentials()

    raw_client = RequestsHttpClient()
    http_client = _build_measured_client(raw_client)

    config = BinanceFuturesPortConfig(
        mode=SafeMode.LIVE_TRADE,
        base_url=BINANCE_FUTURES_MAINNET_URL,
        api_key=api_key,
        api_secret=api_secret,
        symbol_whitelist=symbol_whitelist,
        dry_run=dry_run,
        allow_mainnet=True,
        max_notional_per_order=max_notional_per_order,
        max_orders_per_run=100,  # High limit, budget controls apply
        max_open_orders=50,
        target_leverage=1,  # Conservative default
        identity_config=identity_config,
    )

    return BinanceFuturesPort(http_client=http_client, config=config)


def sanity_check_port(port: BinanceFuturesPort) -> None:
    """Run a sanity check on the port before loop starts.

    Raises:
        ConfigError: If sanity check fails.
    """
    logger.info("Running port sanity check (get position mode)...")
    try:
        # Use a lightweight endpoint to verify connectivity + auth
        position_mode = port.get_position_mode()
        logger.info(f"Port sanity check PASSED (position_mode={position_mode})")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        raise ConfigError(f"Port sanity check FAILED: {e}") from e


# =============================================================================
# STDOUT CONTRACT
# =============================================================================


def print_startup_banner(
    config: ReconcileConfig,
    duration: int,
    interval_ms: int,
    metrics_port: int,
    artifact_paths: ArtifactPaths,
) -> None:
    """Print startup banner per stdout contract."""
    allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "0")

    strategy_display = (
        f"{len(config.remediation_strategy_allowlist)} items"
        if config.remediation_strategy_allowlist
        else "EMPTY"
    )
    symbol_display = (
        f"{len(config.remediation_symbol_allowlist)} items"
        if config.remediation_symbol_allowlist
        else "EMPTY"
    )

    print("=" * 60)
    print("  LIVE RECONCILE RUNNER (LC-18)")
    print("=" * 60)
    print("  Network:              mainnet")
    print(f"  Duration:             {duration}s")
    print(f"  Interval:             {interval_ms}ms")
    print(f"  Metrics port:         {metrics_port if metrics_port else 'DISABLED'}")
    print()
    print("  Artifacts:")
    print(f"    run_dir:            {artifact_paths.run_dir or 'DISABLED'}")
    print(f"    audit_out:          {artifact_paths.audit_out or 'DISABLED'}")
    print(f"    metrics_out:        {artifact_paths.metrics_out or 'DISABLED'}")
    print()
    print(f"  Remediation mode:     {config.remediation_mode.value}")
    print(f"  Allow mainnet trade:  {allow_mainnet}")
    print()
    print(f"  Strategy allowlist:   {strategy_display}")
    print(f"  Symbol allowlist:     {symbol_display}")
    print()
    print("  Budgets:")
    print(f"    max_calls_per_day:          {config.max_calls_per_day}")
    print(f"    max_notional_per_day:       {config.max_notional_per_day}")
    print(f"    max_calls_per_run:          {config.max_calls_per_run}")
    print(f"    max_notional_per_run:       {config.max_notional_per_run}")
    print(f"    flatten_max_notional_call:  {config.flatten_max_notional_per_call}")
    print()
    print(
        f"  Budget state path:    {config.budget_state_path if config.budget_state_path else 'NONE'}"
    )
    print("=" * 60)


def print_final_summary(
    runs_total: int,
    port_calls: int,
    planned_count: int,
    blocked_count: int,
    executed_count: int,
    budget_calls_used: int,
    budget_notional_used: Decimal,
    budget_calls_remaining: int,
    budget_notional_remaining: Decimal,
    audit_events: int,
    exit_code: int,
    artifact_paths: ArtifactPaths | None = None,
) -> None:
    """Print final summary per stdout contract."""
    print()
    print("=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print(f"  Runs total:     {runs_total}")
    print(f"  Port calls:     {port_calls}")
    print(f"  Planned:        {planned_count}")
    print(f"  Blocked:        {blocked_count}")
    print(f"  Executed:       {executed_count}")
    print()
    print("  Budget (today):")
    print(f"    calls_used_day:        {budget_calls_used}")
    print(f"    notional_used_day:     {budget_notional_used}")
    print(f"    calls_remaining_day:   {budget_calls_remaining}")
    print(f"    notional_remaining_day:{budget_notional_remaining}")
    print()
    if audit_events > 0:
        print(f"  Audit events:   {audit_events}")
    if artifact_paths and artifact_paths.run_dir:
        print()
        print("  Artifacts:")
        print(f"    run_dir:      {artifact_paths.run_dir}")
    print("=" * 60)
    print(f"EXIT CODE: {exit_code}")


def print_config_error(message: str) -> None:
    """Print config error per stdout contract."""
    print(f"CONFIG ERROR: {message}")
    print(f"EXIT CODE: {EXIT_CONFIG_ERROR}")


# =============================================================================
# CLI
# =============================================================================


def sync_budget_to_metrics(
    executor: RemediationExecutor,
    metrics: ReconcileMetrics,
) -> None:
    """Sync budget state from executor to metrics for final summary."""
    if executor.budget_tracker is not None:
        used = executor.budget_tracker.get_used()
        remaining = executor.budget_tracker.get_remaining()
        # Cast to expected types (dict values are int|Decimal union)
        metrics.set_budget_metrics(
            calls_used=int(used["calls_used_day"]),
            notional_used=Decimal(str(used["notional_used_day"])),
            calls_remaining=int(remaining["calls_remaining_day"]),
            notional_remaining=Decimal(str(remaining["notional_remaining_day"])),
        )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Live reconcile runner with env-configurable LC-18 settings"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=5000,
        help="Reconcile interval in ms (default: 5000)",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=8093,
        help="Metrics HTTP port (default: 8093)",
    )
    parser.add_argument(
        "--audit-out",
        type=str,
        default=None,
        help=(
            "Audit log output path. "
            "Not provided = auto (uses run-dir if GRINDER_ARTIFACTS_DIR set). "
            "Empty string = disabled. "
            "Path = use explicit path."
        ),
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="BTCUSDT",
        help="Comma-separated symbols for whitelist (default: BTCUSDT)",
    )
    parser.add_argument(
        "--use-fake-port",
        action="store_true",
        help="Use FakePort instead of real port (for testing)",
    )
    parser.add_argument(
        "--metrics-out",
        type=str,
        default=None,
        help=(
            "Path to write final metrics in Prometheus text format. "
            "Not provided = auto (uses run-dir if GRINDER_ARTIFACTS_DIR set). "
            "Empty string = disabled. "
            "Path = use explicit path."
        ),
    )
    parser.add_argument(
        "--reset-budget-state",
        action="store_true",
        help=(
            "Delete BUDGET_STATE_PATH file before starting. "
            "Use for first run or to start with clean budget. "
            "Logs: budget_state_reset=1 path=..."
        ),
    )
    return parser.parse_args()


# =============================================================================
# MAIN
# =============================================================================


def main() -> int:  # noqa: PLR0915, PLR0912
    """Run live reconcile with env-configured LC-18 settings."""
    args = parse_args()

    # Parse symbols
    symbol_whitelist = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # Load config from env
    try:
        config = load_reconcile_config_from_env()
        validate_safety_requirements(config)
    except ConfigError as e:
        print_config_error(str(e))
        return EXIT_CONFIG_ERROR

    # Handle budget state lifecycle (M4.2)
    # Reset must happen BEFORE anything else loads the budget state
    if args.reset_budget_state:
        reset_budget_state(config.budget_state_path)
    else:
        # Check for stale budget state file (default: 24h)
        check_budget_state_stale(config.budget_state_path)

    # Load artifact config and resolve paths (M4.1)
    # Three states for --audit-out / --metrics-out:
    #   None = not provided, can use run-dir auto
    #   "" = explicitly disabled (even with run-dir)
    #   "path" = explicit path
    audit_disabled = args.audit_out == ""
    metrics_disabled = args.metrics_out == ""
    explicit_audit = args.audit_out if args.audit_out else None
    explicit_metrics = args.metrics_out if args.metrics_out else None
    artifact_config = load_artifact_config_from_env(
        explicit_audit_out=explicit_audit,
        explicit_metrics_out=explicit_metrics,
        audit_disabled=audit_disabled,
        metrics_disabled=metrics_disabled,
    )
    artifact_paths = resolve_artifact_paths(artifact_config)

    # Run TTL cleanup only if run-dir is actually being used (P1 fix)
    if artifact_paths.run_dir and artifact_config.ttl_days > 0:
        cleanup_old_runs(artifact_config.base_dir, artifact_config.ttl_days)  # type: ignore[arg-type]

    # Create run-dir if needed
    if artifact_paths.run_dir:
        ensure_run_dir(artifact_paths)

    # Print startup banner
    print_startup_banner(
        config=config,
        duration=args.duration,
        interval_ms=args.interval_ms,
        metrics_port=args.metrics_port,
        artifact_paths=artifact_paths,
    )

    # Setup components
    # Use strategy allowlist from config for identity checking
    # This allows reconcile to detect orders from any allowed strategy
    # Short strategy_id "r" (reconcile) to fit Binance 36-char clientOrderId limit
    identity_config = OrderIdentityConfig(
        prefix="grinder_",
        strategy_id="r",  # Short ID for Binance 36-char limit
        allowed_strategies=config.remediation_strategy_allowlist or {"default"},
    )

    observed = ObservedStateStore()
    expected = ExpectedStateStore()
    metrics = get_reconcile_metrics()

    # Register expected positions for all symbols (expected = 0 for reconcile)
    ts_now = int(time.time() * 1000)
    for symbol in symbol_whitelist:
        expected.set_position(
            ExpectedPosition(
                symbol=symbol,
                expected_position_amt=Decimal("0"),
                ts_updated=ts_now,
            )
        )

    # Engine
    engine = ReconcileEngine(
        config=config,
        expected=expected,
        observed=observed,
        metrics=metrics,
        identity_config=identity_config,
    )

    # Port selection: real vs fake
    if args.use_fake_port:
        port: Any = FakePort()
        logger.info("Using FakePort (no real exchange calls)")
    else:
        # Real port - requires credentials
        try:
            # Determine dry_run based on mode
            is_execute_mode = config.remediation_mode in (
                RemediationMode.EXECUTE_CANCEL_ALL,
                RemediationMode.EXECUTE_FLATTEN,
            )
            port_dry_run = not is_execute_mode

            port = create_real_port(
                symbol_whitelist=symbol_whitelist,
                dry_run=port_dry_run,
                max_notional_per_order=config.flatten_max_notional_per_call,
                identity_config=identity_config,
            )
            logger.info(
                f"Using real BinanceFuturesPort (dry_run={port_dry_run}, "
                f"symbols={symbol_whitelist})"
            )

            # Sanity check
            sanity_check_port(port)

        except ConfigError as e:
            print_config_error(str(e))
            return EXIT_CONFIG_ERROR

    # Executor
    executor = RemediationExecutor(
        config=config,
        port=port,
        armed=not config.dry_run,
        symbol_whitelist=symbol_whitelist,
        identity_config=identity_config,
    )

    # Audit writer (if enabled)
    audit_writer: AuditWriter | None = None
    if artifact_paths.audit_out:
        audit_config = AuditConfig(
            enabled=True,
            path=str(artifact_paths.audit_out),
            flush_every=1,  # Immediate flush for safety
        )
        audit_writer = AuditWriter(config=audit_config)
        logger.info(f"Audit enabled: {artifact_paths.audit_out}")

    # --- Fill ingestion setup (Launch-06 PR2, health metrics PR3) ---
    fill_ingest_enabled = os.environ.get("FILL_INGEST_ENABLED", "") == "1"
    fill_cursor_path = os.environ.get("FILL_CURSOR_PATH", "").strip() or None
    fill_tracker = FillTracker()
    fill_cursor = FillCursor()
    fill_metrics = get_fill_metrics()

    # Set enabled gauge (PR3) — always, so /metrics reflects current state
    fill_metrics.set_ingest_enabled("reconcile", fill_ingest_enabled)

    if fill_ingest_enabled:
        if fill_cursor_path:
            fill_cursor = load_fill_cursor(
                fill_cursor_path,
                fill_metrics=fill_metrics,
            )
        logger.info(
            f"Fill ingestion ENABLED (cursor_path={fill_cursor_path}, "
            f"last_trade_id={fill_cursor.last_trade_id})"
        )
    else:
        logger.info("Fill ingestion DISABLED (set FILL_INGEST_ENABLED=1 to enable)")

    # Snapshot callback for REST polling (LC-19)
    def fetch_snapshot() -> None:
        """Fetch REST snapshot (orders + positions + fills) before each reconcile run."""
        if args.use_fake_port:
            return  # FakePort has no fetch_open_orders_raw

        ts = int(time.time() * 1000)
        all_orders: list[dict[str, Any]] = []
        all_positions: list[dict[str, Any]] = []

        for symbol in symbol_whitelist:
            try:
                orders = port.fetch_open_orders_raw(symbol)
                all_orders.extend(orders)
                logger.debug(f"Fetched {len(orders)} open orders for {symbol}")
            except (ConnectorNonRetryableError, ConnectorTransientError) as e:
                logger.warning(f"Failed to fetch orders for {symbol}: {e}")

            try:
                positions = port.fetch_positions_raw(symbol)
                all_positions.extend(positions)
                logger.debug(f"Fetched {len(positions)} positions for {symbol}")
            except (ConnectorNonRetryableError, ConnectorTransientError) as e:
                logger.warning(f"Failed to fetch positions for {symbol}: {e}")

        observed.update_from_rest_orders(all_orders, ts)
        observed.update_from_rest_positions(all_positions, ts)
        logger.debug(f"Snapshot: {len(all_orders)} orders, {len(all_positions)} positions")

        # --- Fill ingestion (Launch-06 PR2) ---
        if fill_ingest_enabled:
            _fetch_and_ingest_fills(ts)

    def _fetch_and_ingest_fills(ts: int) -> None:
        """Fetch userTrades and ingest into FillTracker (read-only)."""
        all_trades: list[dict[str, Any]] = []
        for symbol in symbol_whitelist:
            try:
                from_id = (fill_cursor.last_trade_id + 1) if fill_cursor.last_trade_id else None
                trades = port.fetch_user_trades_raw(
                    symbol,
                    from_id=from_id,
                    start_time_ms=fill_cursor.last_ts_ms if not from_id else None,
                )
                all_trades.extend(trades)
            except (ConnectorNonRetryableError, ConnectorTransientError) as e:
                logger.warning(f"Failed to fetch user trades for {symbol}: {e}")
                fill_metrics.inc_ingest_error("reconcile", "http")

        count = ingest_fills(
            all_trades,
            fill_tracker,
            fill_cursor,
            fill_metrics=fill_metrics,
        )
        if count > 0:
            push_tracker_to_metrics(fill_tracker, fill_metrics)
            if fill_cursor_path:
                save_fill_cursor(
                    fill_cursor_path,
                    fill_cursor,
                    ts,
                    fill_metrics=fill_metrics,
                )

    # Runner
    runner = ReconcileRunner(
        engine=engine,
        executor=executor,
        observed=observed,
        price_getter=lambda _: Decimal("50000.00"),
        audit_writer=audit_writer,
        pre_run_callback=fetch_snapshot if not args.use_fake_port else None,
    )

    # Loop config
    # detect_only=False for execute modes (LC-14b safety gate)
    is_execute_mode = config.remediation_mode in (
        RemediationMode.EXECUTE_CANCEL_ALL,
        RemediationMode.EXECUTE_FLATTEN,
    )
    loop_config = ReconcileLoopConfig(
        enabled=True,
        interval_ms=args.interval_ms,
        require_active_role=False,
        detect_only=not is_execute_mode,
    )

    # Create loop
    loop = ReconcileLoop(runner=runner, config=loop_config)

    # Setup shutdown handler
    shutdown_requested = False

    def signal_handler(_signum: int, _frame: Any) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        logger.info("Shutdown signal received")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start loop
    logger.info(f"Starting ReconcileLoop (duration={args.duration}s)")
    loop.start()

    # Run for duration
    start_time = time.time()
    while not shutdown_requested:
        elapsed = time.time() - start_time
        if elapsed >= args.duration:
            break
        time.sleep(1.0)

    # Stop loop
    logger.info("Stopping ReconcileLoop...")
    loop.stop()

    # Close audit writer and get event count
    audit_events = 0
    if audit_writer is not None:
        audit_events = audit_writer.event_count
        audit_writer.close()
        logger.info(f"Audit closed: {audit_events} events written")

    # Get stats
    stats = loop.stats
    port_calls = len(port.calls) if hasattr(port, "calls") else 0

    # Sync budget state from executor to metrics
    sync_budget_to_metrics(executor, metrics)

    # Get metrics
    executed_count = sum(metrics.action_executed_counts.values())
    blocked_count = sum(metrics.action_blocked_counts.values())
    planned_count = sum(metrics.action_planned_counts.values())

    # Print final summary
    print_final_summary(
        runs_total=stats.runs_total,
        port_calls=port_calls,
        planned_count=planned_count,
        blocked_count=blocked_count,
        executed_count=executed_count,
        budget_calls_used=metrics.budget_calls_used_day,
        budget_notional_used=metrics.budget_notional_used_day,
        budget_calls_remaining=metrics.budget_calls_remaining_day,
        budget_notional_remaining=metrics.budget_notional_remaining_day,
        audit_events=audit_events,
        exit_code=EXIT_SUCCESS,
        artifact_paths=artifact_paths,
    )

    # Save metrics to file if configured (Prometheus text format)
    if artifact_paths.metrics_out:
        # Get Prometheus lines from reconcile metrics + fill metrics
        prom_lines = metrics.to_prometheus_lines()
        prom_lines.extend(fill_metrics.to_prometheus_lines())
        # Add summary metrics as comments for debugging
        prom_lines.append("")
        prom_lines.append("# run_live_reconcile summary")
        prom_lines.append(f"# runs_total: {stats.runs_total}")
        prom_lines.append(f"# port_calls: {port_calls}")
        prom_lines.append(f"# mode: {config.remediation_mode.value}")
        prom_lines.append(f"# use_fake_port: {args.use_fake_port}")
        prom_lines.append(f"# exit_code: {EXIT_SUCCESS}")
        artifact_paths.metrics_out.parent.mkdir(parents=True, exist_ok=True)
        with artifact_paths.metrics_out.open("w") as f:
            f.write("\n".join(prom_lines) + "\n")
        logger.info(f"Metrics saved to {artifact_paths.metrics_out}")

    # Write stdout summary to run-dir (M4.1)
    if artifact_paths.stdout_log:
        config_summary = {
            "mode": config.remediation_mode.value,
            "duration": args.duration,
            "symbols": args.symbols,
            "max_calls_per_day": config.max_calls_per_day,
            "max_notional_per_day": str(config.max_notional_per_day),
            "runs_total": stats.runs_total,
            "executed_count": executed_count,
            "planned_count": planned_count,
            "blocked_count": blocked_count,
        }
        write_stdout_summary(
            path=artifact_paths.stdout_log,
            config_summary=config_summary,
            exit_code=EXIT_SUCCESS,
            paths=artifact_paths,
        )
        logger.info(f"Stdout summary saved to {artifact_paths.stdout_log}")

    # Copy budget state to run-dir (M4.1)
    if artifact_paths.budget_state and config.budget_state_path:
        copy_budget_state(config.budget_state_path, artifact_paths.budget_state)
        logger.info(f"Budget state copied to {artifact_paths.budget_state}")

    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
