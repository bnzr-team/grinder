"""LiveEngineV0: Live write-path wiring from PaperEngine to ExchangePort.

This module provides the integration point for live trading:
- Wraps PaperEngine for decision-making (grid plan → actions)
- Applies safety gates (arming, mode, kill-switch, symbol whitelist)
- Translates actions to intents for DrawdownGuardV1
- Executes orders via ExchangePort (with H3/H4 wrappers)

Key design (ADR-036):
    1. By default nothing writes (armed=False)
    2. Kill-switch blocks PLACE/REPLACE but allows CANCEL
    3. DrawdownGuardV1 blocks INCREASE_RISK in DRAWDOWN state
    4. Idempotency key created BEFORE retries (H3)
    5. Circuit breaker fast-fails degraded upstream (H4)

Usage:
    paper_engine = PaperEngine(...)
    port = IdempotentExchangePort(
        inner=BinanceExchangePort(...),
        breaker=CircuitBreaker(...),
    )
    live_engine = LiveEngineV0(paper_engine, port, config)

    output = live_engine.process_snapshot(snapshot)
    # output.live_actions contains execution results

See: ADR-036 for design decisions
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from grinder.account.evidence import write_evidence_bundle
from grinder.connectors.errors import (
    CircuitOpenError,
    ConnectorError,
    ConnectorNonRetryableError,
    ConnectorTransientError,
)
from grinder.connectors.live_connector import SafeMode
from grinder.connectors.retries import RetryPolicy, is_retryable
from grinder.core import SystemState
from grinder.env_parse import parse_bool, parse_csv, parse_enum, parse_int
from grinder.execution.fill_prob_evidence import maybe_emit_fill_prob_evidence
from grinder.execution.fill_prob_gate import (
    FillProbCircuitBreaker,
    FillProbVerdict,
    check_fill_prob,
)
from grinder.execution.smart_order_router import (
    ExchangeFilters,
    MarketSnapshot,
    RouterDecision,
    RouterInputs,
    route,
)
from grinder.execution.smart_order_router import (
    OrderIntent as SorOrderIntent,
)
from grinder.execution.sor_metrics import get_sor_metrics
from grinder.execution.types import ActionType, ExecutionAction
from grinder.ml.fill_model_loader import extract_online_features
from grinder.ml.threshold_resolver import (
    resolve_threshold_result,
    write_threshold_resolution_evidence,
)
from grinder.risk.drawdown_guard_v1 import DrawdownGuardV1
from grinder.risk.drawdown_guard_v1 import OrderIntent as RiskIntent
from grinder.risk.emergency_exit import EmergencyExitExecutor
from grinder.risk.emergency_exit_metrics import get_emergency_exit_metrics

if TYPE_CHECKING:
    from grinder.account.syncer import AccountSyncer
    from grinder.contracts import Snapshot
    from grinder.execution.port import ExchangePort
    from grinder.gating.toxicity_gate import ToxicityGate
    from grinder.live.config import LiveEngineConfig
    from grinder.live.fsm_driver import FsmDriver
    from grinder.ml.fill_model_v0 import FillModelV0
    from grinder.paper.engine import PaperEngine

logger = logging.getLogger(__name__)


class BlockReason(Enum):
    """Reason why an action was blocked at engine level."""

    NOT_ARMED = "NOT_ARMED"
    MODE_NOT_LIVE_TRADE = "MODE_NOT_LIVE_TRADE"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    SYMBOL_NOT_WHITELISTED = "SYMBOL_NOT_WHITELISTED"
    DRAWDOWN_BLOCKED = "DRAWDOWN_BLOCKED"
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"
    MAX_RETRIES_EXCEEDED = "MAX_RETRIES_EXCEEDED"
    NON_RETRYABLE_ERROR = "NON_RETRYABLE_ERROR"
    FSM_STATE_BLOCKED = "FSM_STATE_BLOCKED"
    ROUTER_BLOCKED = "ROUTER_BLOCKED"
    FILL_PROB_LOW = "FILL_PROB_LOW"


class LiveActionStatus(Enum):
    """Status of a live action execution."""

    EXECUTED = "EXECUTED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass
class LiveAction:
    """Result of attempting to execute an action on live exchange.

    Attributes:
        action: Original ExecutionAction from PaperEngine
        status: Execution status (EXECUTED/BLOCKED/SKIPPED/FAILED)
        block_reason: Why action was blocked (if status=BLOCKED)
        order_id: Exchange order ID (if EXECUTED)
        error: Error message (if FAILED)
        attempts: Number of attempts made
        intent: Risk intent classification (INCREASE_RISK/REDUCE_RISK/CANCEL)
    """

    action: ExecutionAction
    status: LiveActionStatus
    block_reason: BlockReason | None = None
    order_id: str | None = None
    error: str | None = None
    attempts: int = 1
    intent: RiskIntent | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "action": self.action.to_dict(),
            "status": self.status.value,
            "block_reason": self.block_reason.value if self.block_reason else None,
            "order_id": self.order_id,
            "error": self.error,
            "attempts": self.attempts,
            "intent": self.intent.value if self.intent else None,
        }


@dataclass
class LiveEngineOutput:
    """Output from LiveEngineV0.process_snapshot().

    Extends PaperOutput with live execution results.

    Attributes:
        paper_output: Original output from PaperEngine
        live_actions: List of LiveAction results
        armed: Whether engine was armed
        mode: SafeMode at time of processing
        kill_switch_active: Whether kill-switch was active
    """

    paper_output: Any  # PaperOutput
    live_actions: list[LiveAction] = field(default_factory=list)
    armed: bool = False
    mode: SafeMode = SafeMode.READ_ONLY
    kill_switch_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "paper_output": self.paper_output.to_dict()
            if hasattr(self.paper_output, "to_dict")
            else str(self.paper_output),
            "live_actions": [a.to_dict() for a in self.live_actions],
            "armed": self.armed,
            "mode": self.mode.value,
            "kill_switch_active": self.kill_switch_active,
        }


def classify_intent(action: ExecutionAction) -> RiskIntent:
    """Classify execution action into risk intent.

    Mapping (conservative approach):
        CANCEL → CANCEL (always allowed)
        PLACE → INCREASE_RISK (new order = potential exposure increase)
        REPLACE → INCREASE_RISK (order modification = exposure change)
        NOOP → CANCEL (no action, treated as safe)

    Args:
        action: ExecutionAction from PaperEngine

    Returns:
        RiskIntent for DrawdownGuardV1 evaluation
    """
    if action.action_type == ActionType.CANCEL:
        return RiskIntent.CANCEL
    elif action.action_type == ActionType.NOOP:
        return RiskIntent.CANCEL  # NOOP is safe, treat as CANCEL
    else:
        # PLACE and REPLACE are potentially risk-increasing
        return RiskIntent.INCREASE_RISK


class LiveEngineV0:
    """Live write-path engine wiring PaperEngine to real ExchangePort.

    This class provides the integration point for live trading:
    1. Calls PaperEngine.process_snapshot() to get trading decisions
    2. Applies safety gates (arming, mode, kill-switch, whitelist)
    3. Checks DrawdownGuardV1 for intent-based blocking
    4. Executes allowed actions via ExchangePort (with retries)

    Thread safety: NOT thread-safe. Use one instance per symbol/stream.

    Args:
        paper_engine: PaperEngine for decision-making
        exchange_port: ExchangePort (ideally wrapped with IdempotentExchangePort)
        config: LiveEngineConfig with safety settings
        drawdown_guard: Optional DrawdownGuardV1 for DD-based blocking
        retry_policy: Optional RetryPolicy for transient error retries
    """

    def __init__(
        self,
        paper_engine: PaperEngine,
        exchange_port: ExchangePort,
        config: LiveEngineConfig,
        drawdown_guard: DrawdownGuardV1 | None = None,
        retry_policy: RetryPolicy | None = None,
        fsm_driver: FsmDriver | None = None,
        exchange_filters: ExchangeFilters | None = None,
        account_syncer: AccountSyncer | None = None,
        fill_model: FillModelV0 | None = None,
        toxicity_gate: ToxicityGate | None = None,
    ) -> None:
        """Initialize LiveEngineV0.

        Args:
            paper_engine: Paper engine for grid plan generation
            exchange_port: Exchange port for order execution
            config: Engine configuration (arming, mode, kill-switch)
            drawdown_guard: Optional drawdown guard for intent blocking
            retry_policy: Optional retry policy for transient errors
            fsm_driver: Optional FSM driver for state-based intent gating (Launch-13)
            exchange_filters: Optional exchange filters for SOR (Launch-14)
            account_syncer: Optional account syncer for position/order sync (Launch-15)
            fill_model: Optional FillModelV0 for fill probability gating (PR-C5)
            toxicity_gate: Optional ToxicityGate for toxicity signal (PR-A1)
        """
        self._paper_engine = paper_engine
        self._exchange_port = exchange_port
        self._config = config
        self._drawdown_guard = drawdown_guard
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=3)
        self._fsm_driver = fsm_driver
        self._exchange_filters = exchange_filters
        self._account_syncer = account_syncer
        self._fill_model = fill_model
        self._toxicity_gate = toxicity_gate
        self._last_snapshot: Snapshot | None = None
        # Per-symbol feed staleness tracking (ms timestamps, PR-A1)
        self._prev_snapshot_ts: dict[str, int] = {}
        # Read GRINDER_SOR_ENABLED once at init (via env_parse SSOT)
        self._sor_env_override = parse_bool("GRINDER_SOR_ENABLED", default=False, strict=False)
        # Read GRINDER_ACCOUNT_SYNC_ENABLED once at init (Launch-15)
        self._account_sync_env_override = parse_bool(
            "GRINDER_ACCOUNT_SYNC_ENABLED", default=False, strict=False
        )
        # Read fill prob gate env vars once at init (PR-C5)
        self._fill_prob_enforce = parse_bool(
            "GRINDER_FILL_MODEL_ENFORCE", default=False, strict=False
        )
        self._fill_prob_min_bps: int = (
            parse_int(
                "GRINDER_FILL_PROB_MIN_BPS",
                default=2500,
                min_value=0,
                max_value=10000,
                strict=False,
            )
            or 2500
        )
        # Circuit breaker: trips when block rate exceeds threshold (PR-C8, ADR-073)
        self._fill_prob_cb = FillProbCircuitBreaker()
        # Symbol allowlist for canary rollout (PR-C2): uppercase-normalized
        raw_allowlist = parse_csv("GRINDER_FILL_PROB_ENFORCE_SYMBOLS")
        self._fill_prob_enforce_symbols: frozenset[str] | None = (
            frozenset(s.upper() for s in raw_allowlist) if raw_allowlist else None
        )
        # Set enforce_enabled metric at init (always emitted, default 0)
        sor_metrics = get_sor_metrics()
        sor_metrics.set_fill_prob_enforce_enabled(self._fill_prob_enforce)
        sor_metrics.set_fill_prob_enforce_allowlist_enabled(
            self._fill_prob_enforce_symbols is not None
        )

        # Auto-threshold resolution from eval report (PR-C9, ADR-074)
        self._resolve_auto_threshold()

        # PR-C4: Signal that engine init completed (observable via /metrics)
        sor_metrics.set_engine_initialized()

        # RISK-EE-1: Emergency exit (safe-by-default, opt-in)
        self._emergency_exit_enabled = parse_bool(
            "GRINDER_EMERGENCY_EXIT_ENABLED", default=False, strict=False
        )
        self._emergency_exit_executor: EmergencyExitExecutor | None = None
        self._emergency_exit_executed = False
        self._position_reduced = False
        if self._emergency_exit_enabled:
            # Duck-type check: port must have cancel_all_orders + place_market_order + get_positions
            port = self._exchange_port
            if (
                hasattr(port, "cancel_all_orders")
                and hasattr(port, "place_market_order")
                and hasattr(port, "get_positions")
            ):
                self._emergency_exit_executor = EmergencyExitExecutor(port)  # type: ignore[arg-type]
                logger.info("RISK-EE-1: EmergencyExitExecutor enabled")
            else:
                logger.warning(
                    "RISK-EE-1: GRINDER_EMERGENCY_EXIT_ENABLED=1 but port lacks "
                    "cancel_all_orders/place_market_order/get_positions — executor not created"
                )
        ee_metrics = get_emergency_exit_metrics()
        ee_metrics.set_enabled(self._emergency_exit_enabled)

    def _resolve_auto_threshold(self) -> None:
        """Resolve threshold from eval report at startup (PR-C9).

        Reads GRINDER_FILL_PROB_EVAL_DIR and GRINDER_FILL_PROB_AUTO_THRESHOLD.
        If eval_dir is unset, does nothing.  If set, resolves threshold.
        In auto-apply mode, overrides self._fill_prob_min_bps.
        In recommend-only mode (default), logs but does not override.
        Fail-open: any error -> keep configured threshold.
        """
        eval_dir = os.environ.get("GRINDER_FILL_PROB_EVAL_DIR", "").strip()
        if not eval_dir:
            return

        model_dir = os.environ.get("GRINDER_FILL_MODEL_DIR", "").strip()
        if not model_dir:
            logger.warning(
                "THRESHOLD_RESOLVE_SKIPPED reason=model_dir_unset eval_dir=%s",
                eval_dir,
            )
            return

        auto_apply = parse_bool("GRINDER_FILL_PROB_AUTO_THRESHOLD", default=False, strict=False)
        mode = "auto_apply" if auto_apply else "recommend_only"

        result = resolve_threshold_result(eval_dir, model_dir)
        if result.resolution is None:
            logger.warning(
                "FILL_PROB_THRESHOLD_RESOLUTION_FAILED reason_code=%s "
                "detail=%s eval_path=%s mode=%s configured_bps=%d",
                result.reason_code,
                result.detail,
                eval_dir,
                mode,
                self._fill_prob_min_bps,
            )
            return

        resolution = result.resolution
        configured_bps = self._fill_prob_min_bps
        if auto_apply:
            self._fill_prob_min_bps = resolution.threshold_bps
        effective_bps = self._fill_prob_min_bps

        logger.info(
            "FILL_PROB_THRESHOLD_RESOLUTION_OK mode=%s recommended_bps=%d "
            "configured_bps=%d effective_bps=%d provenance_ok=true",
            mode,
            resolution.threshold_bps,
            configured_bps,
            effective_bps,
        )

        # Set metric (visible to operator)
        get_sor_metrics().set_fill_prob_auto_threshold(resolution.threshold_bps)

        # Evidence artifact (gated on GRINDER_ARTIFACT_DIR, not GRINDER_FILL_PROB_EVIDENCE)
        write_threshold_resolution_evidence(
            resolution=resolution,
            configured_bps=configured_bps,
            mode=mode,
            effective_bps=effective_bps,
        )

    @property
    def config(self) -> LiveEngineConfig:
        """Get current configuration."""
        return self._config

    def update_config(self, config: LiveEngineConfig) -> None:
        """Update configuration (e.g., arm/disarm, change mode)."""
        self._config = config

    def process_snapshot(self, snapshot: Snapshot) -> LiveEngineOutput:
        """Process snapshot through paper engine and execute on live exchange.

        Flow:
            1. Call paper_engine.process_snapshot() → actions
            2. For each action:
                a. Classify intent (INCREASE_RISK/REDUCE_RISK/CANCEL)
                b. Check safety gates (arming, mode, kill-switch, whitelist)
                c. Check DrawdownGuardV1.allow(intent)
                d. Execute via exchange_port (with retries for transient errors)
            3. Return LiveEngineOutput with execution results

        Args:
            snapshot: Market data snapshot

        Returns:
            LiveEngineOutput with paper output and live action results
        """
        # Store snapshot for SOR market data (Launch-14 PR2)
        self._last_snapshot = snapshot

        # Record price for toxicity gate (needs history before check, PR-A1)
        if self._toxicity_gate is not None:
            self._toxicity_gate.record_price(snapshot.ts, snapshot.symbol, snapshot.mid_price)

        # Step 1: Get paper engine decisions
        paper_output = self._paper_engine.process_snapshot(snapshot)

        # FSM tick: update state before action processing (Launch-13 PR3)
        if self._fsm_driver is not None:
            self._tick_fsm(snapshot.ts, snapshot.symbol)

        # RISK-EE-1: Emergency exit trigger (after FSM tick, before action processing)
        if (
            self._emergency_exit_enabled
            and self._emergency_exit_executor is not None
            and not self._emergency_exit_executed
            and self._fsm_driver is not None
            and self._fsm_driver.state == SystemState.EMERGENCY
        ):
            self._execute_emergency_exit(snapshot.ts)

        # Account sync: read-only fetch + mismatch detection (Launch-15)
        if self._is_account_sync_enabled():
            self._tick_account_sync()

        # Step 2: Process actions
        live_actions: list[LiveAction] = []
        raw_actions = paper_output.actions if hasattr(paper_output, "actions") else []

        for raw_action in raw_actions:
            # PaperOutput.actions is list[dict], but tests may pass ExecutionAction directly
            if isinstance(raw_action, dict):
                action = ExecutionAction.from_dict(raw_action)
            else:
                action = raw_action
            live_action = self._process_action(action, snapshot.ts)
            live_actions.append(live_action)

        # Step 3: Build output
        return LiveEngineOutput(
            paper_output=paper_output,
            live_actions=live_actions,
            armed=self._config.armed,
            mode=self._config.mode,
            kill_switch_active=self._config.kill_switch_active,
        )

    def _tick_fsm(self, ts_ms: int, symbol: str) -> None:
        """Tick FSM driver with current runtime signals.

        Reads kill_switch, drawdown from existing guards.
        operator_override from GRINDER_OPERATOR_OVERRIDE env var.
        feed_gap_ms from per-symbol snapshot gap (PR-A2a: numeric, FSM owns threshold).
        spread_bps + toxicity_score_bps from snapshot + ToxicityGate (PR-A2a).

        Uses snapshot clock (ts_ms) for deterministic duration tracking.
        All timestamps in milliseconds (Snapshot.ts contract).
        """
        assert self._fsm_driver is not None  # caller guards

        # Signal: operator override from env var (via env_parse SSOT)
        override = parse_enum(
            "GRINDER_OPERATOR_OVERRIDE",
            allowed={"PAUSE", "EMERGENCY"},
            default=None,
            strict=False,
        )

        # Compute feed_gap_ms from per-symbol snapshot gap (PR-A2a)
        prev_ts = self._prev_snapshot_ts.get(symbol, 0)
        feed_gap_ms = (ts_ms - prev_ts) if prev_ts > 0 else 0
        self._prev_snapshot_ts[symbol] = ts_ms

        # Compute spread_bps + toxicity_score_bps (PR-A2a: raw numerics, FSM owns thresholds)
        spread_bps = 0.0
        toxicity_score_bps = 0.0
        if self._last_snapshot is not None:
            spread_bps = self._last_snapshot.spread_bps
        if self._toxicity_gate is not None and self._last_snapshot is not None:
            snap = self._last_snapshot
            toxicity_score_bps = self._toxicity_gate.price_impact_bps(
                ts_ms, snap.symbol, snap.mid_price
            )

        self._fsm_driver.step(
            ts_ms=ts_ms,
            kill_switch_active=self._config.kill_switch_active,
            drawdown_pct=(
                self._drawdown_guard.current_drawdown_pct
                if self._drawdown_guard is not None
                else 0.0
            ),
            feed_gap_ms=feed_gap_ms,
            spread_bps=spread_bps,
            toxicity_score_bps=toxicity_score_bps,
            position_reduced=self._position_reduced,  # RISK-EE-1
            operator_override=override,
        )

    def _execute_emergency_exit(self, ts_ms: int) -> None:
        """Execute emergency exit sequence (RISK-EE-1, § 10.6).

        Determines target symbols from config whitelist or open positions.
        Calls EmergencyExitExecutor.execute() and sets _position_reduced flag.
        Runs at most once (latch: _emergency_exit_executed).
        """
        assert self._emergency_exit_executor is not None  # caller guards

        # Determine target symbols: whitelist > positions-derived
        symbols = list(self._config.symbol_whitelist)
        if not symbols:
            # No whitelist: derive symbols from open positions
            try:
                positions = self._exchange_port.fetch_positions()
                symbols = list({p.symbol for p in positions if hasattr(p, "symbol")})
            except Exception:
                logger.exception("Failed to derive symbols from positions for emergency exit")

        if not symbols:
            logger.critical(
                "EMERGENCY EXIT: no symbols to process (whitelist empty, no positions found)"
            )
            self._emergency_exit_executed = True
            self._position_reduced = True  # no positions = effectively reduced
            return

        result = self._emergency_exit_executor.execute(
            ts_ms=ts_ms,
            reason="fsm_emergency",
            symbols=symbols,
        )
        self._position_reduced = result.success
        self._emergency_exit_executed = True

        get_emergency_exit_metrics().record_exit(result)

        logger.critical(
            "EMERGENCY EXIT %s: cancelled=%d market=%d remaining=%d",
            "SUCCESS" if result.success else "PARTIAL",
            result.orders_cancelled,
            result.market_orders_placed,
            result.positions_remaining,
        )

    def _is_account_sync_enabled(self) -> bool:
        """Check if account sync is active.

        Requires: feature flag (config or env) AND syncer instance.
        """
        flag_on = self._config.account_sync_enabled or self._account_sync_env_override
        if not flag_on:
            return False
        if self._account_syncer is None:
            logger.debug("Account sync flag ON but no syncer instance, skipping")
            return False
        return True

    def _tick_account_sync(self) -> None:
        """Run one account sync cycle (read-only).

        Fetches snapshot, detects mismatches, records metrics.
        Evidence writing is delegated to evidence.py (env-gated).
        """
        assert self._account_syncer is not None  # caller guards

        result = self._account_syncer.sync()

        if result.error is not None:
            logger.warning("Account sync failed: %s", result.error)
            return

        if result.snapshot is not None and result.mismatches:
            logger.warning(
                "Account sync mismatches detected: %d",
                len(result.mismatches),
            )
            for m in result.mismatches:
                logger.warning("  [%s] %s", m.rule, m.detail)

        # Evidence writing (env-gated, safe-by-default)
        if result.snapshot is not None:
            evidence_dir = write_evidence_bundle(result.snapshot, result.mismatches)
            if evidence_dir is not None:
                logger.info("Account sync evidence written to %s", evidence_dir)

    def _process_action(self, action: ExecutionAction, ts: int) -> LiveAction:  # noqa: PLR0911
        """Process single action through safety gates and execute.

        Args:
            action: ExecutionAction from PaperEngine
            ts: Current timestamp

        Returns:
            LiveAction with execution result
        """
        intent = classify_intent(action)

        # Gate 1: Arming check
        if not self._config.armed:
            logger.debug("Action blocked: NOT_ARMED (action=%s)", action.action_type.value)
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.NOT_ARMED,
                intent=intent,
            )

        # Gate 2: Mode check
        if self._config.mode != SafeMode.LIVE_TRADE:
            logger.debug(
                "Action blocked: MODE_NOT_LIVE_TRADE (mode=%s, action=%s)",
                self._config.mode.value,
                action.action_type.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.MODE_NOT_LIVE_TRADE,
                intent=intent,
            )

        # Gate 3: Kill-switch (blocks PLACE/REPLACE, allows CANCEL)
        if self._config.kill_switch_active and intent != RiskIntent.CANCEL:
            logger.warning(
                "Action blocked: KILL_SWITCH_ACTIVE (intent=%s, action=%s)",
                intent.value,
                action.action_type.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.KILL_SWITCH_ACTIVE,
                intent=intent,
            )
        # Note: CANCEL allowed even with kill-switch active

        # Gate 4: Symbol whitelist
        if action.symbol and not self._config.is_symbol_allowed(action.symbol):
            logger.warning(
                "Action blocked: SYMBOL_NOT_WHITELISTED (symbol=%s)",
                action.symbol,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.SYMBOL_NOT_WHITELISTED,
                intent=intent,
            )

        # Gate 5: DrawdownGuardV1 (if configured)
        if self._drawdown_guard is not None:
            allow_decision = self._drawdown_guard.allow(intent, symbol=action.symbol or None)
            if not allow_decision.allowed:
                logger.warning(
                    "Action blocked: DRAWDOWN_BLOCKED (intent=%s, reason=%s)",
                    intent.value,
                    allow_decision.reason.value,
                )
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.BLOCKED,
                    block_reason=BlockReason.DRAWDOWN_BLOCKED,
                    intent=intent,
                )

        # Gate 6: FSM state permission (Launch-13)
        if self._fsm_driver is not None and not self._fsm_driver.check_intent(intent):
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.FSM_STATE_BLOCKED,
                intent=intent,
            )

        # Gate 7: Fill probability gate (PR-C5, PLACE/REPLACE only)
        if self._fill_model is not None and action.action_type in (
            ActionType.PLACE,
            ActionType.REPLACE,
        ):
            fill_result = self._check_fill_prob(action, intent)
            if fill_result is not None:
                return fill_result

        # SOR routing (Launch-14 PR2): after all safety gates, before execution
        if self._is_sor_enabled() and action.action_type in (
            ActionType.PLACE,
            ActionType.REPLACE,
        ):
            sor_result = self._apply_sor(action, ts, intent)
            if sor_result is not None:
                return sor_result

        # All gates passed - execute action
        return self._execute_action(action, ts, intent)

    def _is_sor_enabled(self) -> bool:
        """Check if SOR routing is active.

        Requires all of: feature flag (config or env), exchange filters, and snapshot.
        """
        flag_on = self._config.sor_enabled or self._sor_env_override
        if not flag_on:
            return False
        if self._exchange_filters is None:
            logger.debug("SOR flag ON but exchange_filters missing, skipping SOR")
            return False
        if self._last_snapshot is None:
            logger.debug("SOR flag ON but no snapshot available, skipping SOR")
            return False
        return True

    def _apply_sor(
        self, action: ExecutionAction, _ts: int, intent: RiskIntent
    ) -> LiveAction | None:
        """Apply SmartOrderRouter to decide execution method.

        Returns LiveAction for BLOCK/NOOP, None to continue with normal execution
        (CANCEL_REPLACE falls through to standard _execute_action).

        Args:
            action: PLACE or REPLACE action from PaperEngine
            ts: Current timestamp
            intent: Risk intent classification

        Returns:
            LiveAction if SOR blocks/skips, None to continue normal execution.
        """
        assert self._exchange_filters is not None  # caller guards via _is_sor_enabled
        assert self._last_snapshot is not None  # caller guards via _is_sor_enabled
        assert action.price is not None
        assert action.quantity is not None
        assert action.side is not None

        router_inputs = RouterInputs(
            intent=SorOrderIntent(
                price=action.price,
                qty=action.quantity,
                side=action.side.value,
            ),
            existing=None,  # PR2: no order state tracking yet
            market=MarketSnapshot(
                best_bid=self._last_snapshot.bid_price,
                best_ask=self._last_snapshot.ask_price,
            ),
            filters=self._exchange_filters,
            drawdown_breached=False,  # Already handled by Gate 5
        )

        result = route(router_inputs)

        # Normalize AMEND to CANCEL_REPLACE before recording metrics (P1-1)
        decision = result.decision
        reason = result.reason
        if decision == RouterDecision.AMEND:
            logger.warning(
                "SOR returned AMEND with existing=None (unreachable), normalizing to CANCEL_REPLACE"
            )
            decision = RouterDecision.CANCEL_REPLACE
            reason = "AMEND_NORMALIZED_TO_CANCEL_REPLACE"

        # Record metric (single call, after normalization)
        get_sor_metrics().record_decision(decision.value, reason)

        if decision == RouterDecision.BLOCK:
            logger.info(
                "SOR blocked action: reason=%s, action=%s",
                reason,
                action.action_type.value,
            )
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.ROUTER_BLOCKED,
                intent=intent,
            )

        if decision == RouterDecision.NOOP:
            logger.debug("SOR NOOP: reason=%s", reason)
            return LiveAction(
                action=action,
                status=LiveActionStatus.SKIPPED,
                intent=intent,
            )

        # CANCEL_REPLACE: fall through to normal execution
        return None

    def _check_fill_prob(self, action: ExecutionAction, intent: RiskIntent) -> LiveAction | None:
        """Check fill probability gate for a PLACE/REPLACE action.

        Returns LiveAction on BLOCK, None to continue normal processing.
        Circuit breaker (PR-C8): if tripped, bypass gate → ALLOW.

        Args:
            action: PLACE or REPLACE action from PaperEngine.
            intent: Risk intent classification.

        Returns:
            LiveAction if gate blocks, None to proceed.
        """
        # Circuit breaker: if tripped, bypass gate entirely (fail-open)
        if self._fill_prob_cb.is_tripped():
            get_sor_metrics().record_cb_trip()
            return None

        # Symbol allowlist (PR-C2): if set and symbol not in list, skip gate (ALLOW)
        if (
            self._fill_prob_enforce_symbols is not None
            and action.symbol.upper() not in self._fill_prob_enforce_symbols
        ):
            return None

        assert action.price is not None
        assert action.quantity is not None
        assert action.side is not None

        direction = "long" if action.side.value == "BUY" else "short"
        notional = float(action.price * action.quantity)
        features = extract_online_features(direction=direction, notional=notional)

        result = check_fill_prob(
            model=self._fill_model,
            features=features,
            threshold_bps=self._fill_prob_min_bps,
            enforce=self._fill_prob_enforce,
        )

        # Record verdict in circuit breaker (no-op in shadow mode)
        self._fill_prob_cb.record(result.verdict, enforce=self._fill_prob_enforce)

        # Record metrics
        sor_metrics = get_sor_metrics()

        # Evidence: emit on BLOCK/SHADOW (log + optional artifact)
        if result.verdict in (FillProbVerdict.BLOCK, FillProbVerdict.SHADOW):
            action_meta = {
                "action_type": action.action_type.value,
                "symbol": action.symbol,
                "side": action.side.value,
                "price": str(action.price),
                "qty": str(action.quantity),
            }
            maybe_emit_fill_prob_evidence(
                result=result,
                features=features,
                model=self._fill_model,
                action_meta=action_meta,
            )

        if result.verdict == FillProbVerdict.BLOCK:
            sor_metrics.record_fill_prob_block()
            return LiveAction(
                action=action,
                status=LiveActionStatus.BLOCKED,
                block_reason=BlockReason.FILL_PROB_LOW,
                intent=intent,
            )

        # ALLOW or SHADOW: continue normal processing
        return None

    def _execute_action(self, action: ExecutionAction, ts: int, intent: RiskIntent) -> LiveAction:
        """Execute action on exchange port with retries.

        Args:
            action: ExecutionAction to execute
            ts: Current timestamp
            intent: Risk intent classification

        Returns:
            LiveAction with execution result
        """
        if action.action_type == ActionType.NOOP:
            return LiveAction(
                action=action,
                status=LiveActionStatus.SKIPPED,
                intent=intent,
            )

        max_attempts = self._retry_policy.max_attempts
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                order_id = self._execute_single(action, ts)
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.EXECUTED,
                    order_id=order_id,
                    attempts=attempt,
                    intent=intent,
                )
            except ConnectorNonRetryableError as e:
                # Non-retryable: fail immediately
                logger.error(
                    "Non-retryable error on %s: %s",
                    action.action_type.value,
                    str(e),
                )
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.FAILED,
                    block_reason=BlockReason.NON_RETRYABLE_ERROR,
                    error=str(e),
                    attempts=attempt,
                    intent=intent,
                )
            except ConnectorTransientError as e:
                # Transient: retry with backoff
                last_error = e
                if attempt < max_attempts:
                    delay_ms = self._retry_policy.compute_delay_ms(attempt)
                    logger.warning(
                        "Transient error on %s (attempt %d/%d), retrying in %dms: %s",
                        action.action_type.value,
                        attempt,
                        max_attempts,
                        delay_ms,
                        str(e),
                    )
                    time.sleep(delay_ms / 1000.0)
            except CircuitOpenError as e:
                # Circuit breaker is OPEN: fail immediately (non-retryable)
                logger.warning(
                    "Circuit breaker OPEN for %s: %s",
                    action.action_type.value,
                    str(e),
                )
                return LiveAction(
                    action=action,
                    status=LiveActionStatus.FAILED,
                    block_reason=BlockReason.CIRCUIT_BREAKER_OPEN,
                    error=str(e),
                    attempts=attempt,
                    intent=intent,
                )
            except ConnectorError as e:
                # Other connector errors: check if retryable
                if is_retryable(e, self._retry_policy):
                    last_error = e
                    if attempt < max_attempts:
                        delay_ms = self._retry_policy.compute_delay_ms(attempt)
                        time.sleep(delay_ms / 1000.0)
                else:
                    return LiveAction(
                        action=action,
                        status=LiveActionStatus.FAILED,
                        block_reason=BlockReason.NON_RETRYABLE_ERROR,
                        error=str(e),
                        attempts=attempt,
                        intent=intent,
                    )

        # All retries exhausted
        logger.error(
            "Max retries exceeded for %s: %s",
            action.action_type.value,
            str(last_error),
        )
        return LiveAction(
            action=action,
            status=LiveActionStatus.FAILED,
            block_reason=BlockReason.MAX_RETRIES_EXCEEDED,
            error=str(last_error) if last_error else "Unknown error",
            attempts=max_attempts,
            intent=intent,
        )

    def _execute_single(self, action: ExecutionAction, ts: int) -> str | None:
        """Execute single action on exchange port (no retries).

        Args:
            action: ExecutionAction to execute
            ts: Current timestamp

        Returns:
            Order ID (str for PLACE/REPLACE, None for CANCEL)

        Raises:
            ConnectorError: On execution failure
        """
        if action.action_type == ActionType.PLACE:
            assert action.side is not None, "PLACE requires side"
            assert action.price is not None, "PLACE requires price"
            assert action.quantity is not None, "PLACE requires quantity"
            return self._exchange_port.place_order(
                symbol=action.symbol,
                side=action.side,
                price=action.price,
                quantity=action.quantity,
                level_id=action.level_id,
                ts=ts,
            )
        elif action.action_type == ActionType.CANCEL:
            assert action.order_id is not None, "CANCEL requires order_id"
            success = self._exchange_port.cancel_order(action.order_id)
            return action.order_id if success else None
        elif action.action_type == ActionType.REPLACE:
            assert action.order_id is not None, "REPLACE requires order_id"
            assert action.price is not None, "REPLACE requires new price"
            assert action.quantity is not None, "REPLACE requires new quantity"
            return self._exchange_port.replace_order(
                order_id=action.order_id,
                new_price=action.price,
                new_quantity=action.quantity,
                ts=ts,
            )
        else:
            # NOOP - should not reach here
            return None

    def reset(self) -> None:
        """Reset engine state (for testing)."""
        if hasattr(self._exchange_port, "reset"):
            self._exchange_port.reset()
