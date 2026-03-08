"""LiveCycleLayerV1: fill detection + TP order generation + replenish (PR-INV-3/4).

Detects grid order fills by comparing AccountSync snapshots. When a grinder
grid order disappears without us cancelling it, generates a reduce-only TP
PLACE action on the opposite side with grinder_tp_ clientOrderId namespace.

PR-INV-4 additions:
- Replenish: after fill, restore grid level at next level further from center.
  Replenish = INCREASE_RISK, safe via gate chain (Gate 5 max-position, INV-2
  suppress_increase in non-ACTIVE states).
- Safe-by-default: GRINDER_LIVE_REPLENISH_ENABLED=0
- Fail-closed: non-numeric level_id or level+1 > max_levels -> skip replenish
- mid_price <= 0 -> skip replenish (no center reference)

Invariants:
- Only grid orders (strategy_id != "tp") are fill candidates
- Pending cancels excluded (TTL-based, 30s default)
- TP-on-TP impossible (strategy_id="tp" excluded from candidates)
- Idempotent: deterministic LRU dedup cache (OrderedDict, bounded)
- reduce_only=True is semantic invariant for TPs (Binance prevents position increase)
- V1 limitation: single tick_size for all symbols (BTCUSDT only in current C4)
- Contract: non-numeric level_id (e.g., "cleanup") -> TP level_id=0, no replenish

PR-INV-3b additions:
- TP expiry: TPs older than tp_ttl_ms are cancelled (GRINDER_TP_TTL_MS env var)
- Metrics: CycleMetrics singleton tracks tp_generated, tp_expired, fill_candidates
- tp_created_ts cleanup: entries removed when TP disappears from open_orders
- Stale tp_created_ts cleanup: time-based eviction for rejected TPs (never on exchange)
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.cycle_metrics import get_cycle_metrics
from grinder.reconcile.identity import (
    DEFAULT_PREFIX,
    DEFAULT_STRATEGY_ID,
    TP_STRATEGY_ID,
    OrderIdentityConfig,
    generate_client_order_id,
    is_tp_order,
    parse_client_order_id,
)

if TYPE_CHECKING:
    from grinder.account.contracts import OpenOrderSnap

logger = logging.getLogger(__name__)

_MAX_DEDUP_ENTRIES = 1000
_CANCEL_TTL_MS = 30_000  # 30s = ~6 AccountSync cycles
_STALE_TP_CREATED_FALLBACK_MS = 600_000  # 10min fallback when tp_ttl_ms disabled
_STALE_TP_CREATED_MIN_MS = 60_000  # floor for stale cleanup TTL


@dataclass
class LiveCycleConfig:
    """Configuration for the live cycle layer.

    Attributes:
        spacing_bps: Price offset in basis points (used for TP and replenish grid step).
        tick_size: Tick size for price rounding (None = no rounding).
        tp_ttl_ms: TP order TTL in milliseconds. None or 0 = disabled (no expiry).
            Default: 300_000 (5 minutes).
        replenish_enabled: Enable replenish after fill (default: False, safe-by-default).
        replenish_max_levels: Max grid level for replenish. 0 = disabled (fail-closed).
        tp_renew_enabled: Auto-renew TP on expiry when position open (default: False).
        tp_renew_cooldown_ms: Minimum interval between renewals per symbol (ms).
        tp_renew_max_attempts: Max consecutive renew failures before degrading to plain cancel.
        tp_qty_mode: TP quantity mode — "full" | "one_level" | "pct" (default: "full").
        tp_qty_pct: Percentage of position for pct mode (int 1-100, default 100).
        per_level_qty: Per-level quantity from paper config (Decimal, None = unknown).
        step_size: Lot size step for qty rounding (Decimal, None = no rounding).
    """

    spacing_bps: float = 10.0
    tick_size: Decimal | None = None
    tp_ttl_ms: int | None = 300_000
    replenish_enabled: bool = False
    replenish_max_levels: int = 0
    tp_renew_enabled: bool = False
    tp_renew_cooldown_ms: int = 60_000
    tp_renew_max_attempts: int = 3
    tp_qty_mode: str = "full"
    tp_qty_pct: int = 100
    per_level_qty: Decimal | None = None
    step_size: Decimal | None = None


class LiveCycleLayerV1:
    """Detect grid order fills and generate reduce-only TP PLACE actions.

    Fill detection: compare consecutive AccountSync snapshots. An order
    present in prev but absent in current (and not in pending_cancels)
    is treated as a fill candidate.

    TP generation: opposite side, reduce_only=True, grinder_tp_ namespace.
    """

    def __init__(self, config: LiveCycleConfig) -> None:
        self._config = config
        self._prev_orders: dict[str, OpenOrderSnap] = {}
        # TTL-based pending cancels: order_id -> ts_ms when registered
        self._pending_cancels: dict[str, int] = {}
        # Deterministic LRU dedup: OrderedDict preserves insertion order
        self._generated_tp_ids: OrderedDict[str, int] = OrderedDict()
        self._tp_identity = OrderIdentityConfig(
            prefix=DEFAULT_PREFIX,
            strategy_id=TP_STRATEGY_ID,
            require_strategy_allowlist=False,
        )
        self._grid_identity = OrderIdentityConfig(
            prefix=DEFAULT_PREFIX,
            strategy_id=DEFAULT_STRATEGY_ID,
            require_strategy_allowlist=False,
        )
        self._tp_seq = 0
        self._replenish_seq = 0
        # PR-INV-3b: Track TP creation timestamps for expiry
        self._tp_created_ts: dict[str, int] = {}
        # TP auto-renew state (PR-TP-RENEW)
        self._tp_source_price: dict[
            str, tuple[Decimal, str]
        ] = {}  # tp_id -> (fill_price, fill_side)
        self._tp_renew_last_ts: dict[str, int] = {}  # symbol -> last renew ts_ms
        self._tp_renew_inflight: dict[str, bool] = {}  # symbol -> renew in progress
        self._tp_renew_attempts: dict[str, int] = {}  # symbol -> consecutive failures
        self._metrics = get_cycle_metrics()

    def register_cancels(self, actions: list[ExecutionAction], ts_ms: int) -> None:
        """Register CANCEL actions with timestamp for TTL expiry.

        Args:
            actions: Execution actions (only CANCELs are registered).
            ts_ms: Current timestamp in milliseconds (MUST be real, not 0).
        """
        for a in actions:
            if a.action_type == ActionType.CANCEL and a.order_id:
                self._pending_cancels[a.order_id] = ts_ms

    def unregister_pending_cancel(self, order_id: str) -> None:
        """Remove pending cancel entry (engine calls when CANCEL skipped).

        Prevents false fill-suppression on next tick if the order fills
        naturally after the CANCEL was not executed.
        """
        self._pending_cancels.pop(order_id, None)

    def _cleanup_pending_cancels(self, ts_ms: int) -> None:
        """Remove expired pending cancel entries (TTL-based)."""
        expired = [
            oid for oid, reg_ts in self._pending_cancels.items() if ts_ms - reg_ts > _CANCEL_TTL_MS
        ]
        for oid in expired:
            del self._pending_cancels[oid]

    def _dedup_add(self, oid: str, ts_ms: int) -> None:
        """Add to dedup cache with deterministic LRU eviction."""
        self._generated_tp_ids[oid] = ts_ms
        # Evict oldest (first inserted) when over limit
        while len(self._generated_tp_ids) > _MAX_DEDUP_ENTRIES:
            self._generated_tp_ids.popitem(last=False)  # FIFO eviction

    def on_snapshot(  # noqa: PLR0912, PLR0915
        self,
        *,
        symbol: str,
        open_orders: tuple[OpenOrderSnap, ...],
        mid_price: Decimal,
        ts_ms: int,
        pos_qty: Decimal | None = None,
    ) -> list[ExecutionAction]:
        """Detect fills, generate TP PLACEs, replenish grid, and expire stale TPs.

        Args:
            symbol: Trading symbol to process.
            open_orders: All open orders for this symbol from AccountSync.
            mid_price: Current mid price (used for replenish center pricing).
            ts_ms: Current timestamp in milliseconds.
            pos_qty: Absolute position quantity (None = unknown, 0 = flat).
                When pos_qty != 0 and tp_renew_enabled, expired TPs are renewed
                instead of just cancelled.

        Returns:
            List of ExecutionActions: TP PLACEs + replenish PLACEs + TP expiry
            CANCELs (may be empty).
        """
        # Cleanup expired pending cancels
        self._cleanup_pending_cancels(ts_ms)

        # Build current map: only parseable grinder orders for this symbol
        current: dict[str, OpenOrderSnap] = {}
        for o in open_orders:
            if o.symbol != symbol:
                continue
            parsed = parse_client_order_id(o.order_id)
            if parsed is not None:
                current[o.order_id] = o

        actions: list[ExecutionAction] = []
        # Collect fill info for Phase 4 (replenish)
        fills: list[tuple[str, OpenOrderSnap, int]] = []  # (oid, snap, source_level_id)
        # PR-ROLL-2: Track grid orders claimed for slot takeover (multi-fill dedup)
        claimed_for_takeover: set[str] = set()

        # --- Phase 1: Fill detection → TP generation ---
        for oid, snap in self._prev_orders.items():
            if oid in current:
                continue  # still open

            # Skip pending cancels (we initiated the removal)
            if oid in self._pending_cancels:
                del self._pending_cancels[oid]  # consumed
                self._metrics.record_fill_candidate(symbol, "skipped_pending_cancel")
                continue

            # Skip TP orders (TP disappearance = filled/expired, no action)
            if is_tp_order(oid):
                # PR-INV-3b: Clean up tp_created_ts when TP disappears
                self._tp_created_ts.pop(oid, None)
                self._tp_source_price.pop(oid, None)
                self._metrics.record_fill_candidate(symbol, "skipped_tp_order")
                continue

            # Idempotency: don't generate TP twice for same source
            if oid in self._generated_tp_ids:
                self._metrics.record_fill_candidate(symbol, "skipped_dedup")
                continue
            self._dedup_add(oid, ts_ms)

            # Parse source order for level_id
            # Contract (P1-2): non-numeric level_id (e.g., "cleanup") -> TP level_id=0
            parsed = parse_client_order_id(oid)
            raw_level_id = parsed.level_id if parsed else ""
            source_level_id: int = int(raw_level_id) if raw_level_id.isdigit() else 0

            # Generate TP on opposite side
            tp_side = OrderSide.SELL if snap.side.upper() == "BUY" else OrderSide.BUY
            tp_price = self._compute_tp_price(snap.price, snap.side)

            # PR-TP-PARTIAL: compute TP qty (may be partial)
            tp_qty = self._compute_tp_qty(snap.qty, pos_qty)
            if tp_qty <= 0:
                self._metrics.record_fill_candidate(symbol, "tp_qty_too_small")
                logger.warning(
                    "TP_SKIPPED_QTY_TOO_SMALL src=%s mode=%s",
                    oid,
                    self._config.tp_qty_mode,
                )
                continue

            # Pre-generate clientOrderId with TP namespace
            self._tp_seq += 1
            tp_client_id = generate_client_order_id(
                config=self._tp_identity,
                symbol=symbol,
                level_id=source_level_id,
                ts=ts_ms,
                seq=self._tp_seq,
            )

            actions.append(
                ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=symbol,
                    side=tp_side,
                    price=tp_price,
                    quantity=tp_qty,
                    level_id=source_level_id,
                    reason="TP_CLOSE",
                    reduce_only=True,
                    client_order_id=tp_client_id,
                    correlation_id=tp_client_id,
                )
            )

            # PR-INV-3b: Track TP creation time for expiry
            self._tp_created_ts[tp_client_id] = ts_ms
            # PR-TP-RENEW: Store source fill price for potential renewal
            self._tp_source_price[tp_client_id] = (snap.price, snap.side)
            self._metrics.record_tp_generated(symbol)
            self._metrics.record_fill_candidate(symbol, "tp_generated")

            logger.info(
                "TP generated: src=%s side=%s price=%s qty=%s -> tp_id=%s",
                oid,
                tp_side.value,
                tp_price,
                tp_qty,
                tp_client_id,
            )

            # PR-ROLL-2: TP_SLOT_TAKEOVER — cancel farthest same-side grid order
            tp_side_str = tp_side.value
            same_side_grid: list[OpenOrderSnap] = []
            for o in open_orders:
                if o.symbol != symbol or o.side.upper() != tp_side_str:
                    continue
                if o.order_id in claimed_for_takeover:
                    continue
                po = parse_client_order_id(o.order_id)
                if po is None or po.strategy_id != DEFAULT_STRATEGY_ID:
                    continue
                same_side_grid.append(o)
            if same_side_grid:
                if tp_side == OrderSide.SELL:
                    farthest = max(same_side_grid, key=lambda x: x.price)
                else:
                    farthest = min(same_side_grid, key=lambda x: x.price)
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        order_id=farthest.order_id,
                        symbol=symbol,
                        reason="TP_SLOT_TAKEOVER",
                        correlation_id=tp_client_id,
                    )
                )
                self._pending_cancels[farthest.order_id] = ts_ms
                claimed_for_takeover.add(farthest.order_id)
                logger.info(
                    "TP_SLOT_TAKEOVER symbol=%s side=%s tp_id=%s removed_grid_id=%s removed_price=%s",
                    symbol,
                    tp_side_str,
                    tp_client_id,
                    farthest.order_id,
                    farthest.price,
                )
            else:
                logger.info(
                    "TP_SLOT_TAKEOVER_SKIP reason=no_same_side_grid symbol=%s side=%s",
                    symbol,
                    tp_side_str,
                )

            # Collect fill for replenish (Phase 4)
            # Only numeric level_id qualifies for replenish (fail-closed)
            if raw_level_id.isdigit():
                fills.append((oid, snap, source_level_id))

        # --- Phase 2: TP expiry → CANCEL stale TPs (+ auto-renew if enabled) ---
        expiry_actions = self._handle_tp_expiry(current, symbol, ts_ms, pos_qty)
        actions.extend(expiry_actions)

        # --- Phase 3: Clean up tp_created_ts for TPs no longer in open_orders ---
        # (not expired, just gone — e.g., filled by exchange, or rejected/stale)
        self._cleanup_tp_created_ts(current, ts_ms)

        # --- Phase 4: Replenish — restore filled grid levels (PR-INV-4) ---
        replenish_actions = self._generate_replenish(fills, symbol, mid_price, ts_ms)
        actions.extend(replenish_actions)

        # --- Phase 5: Update tp_active gauge ---
        has_position = pos_qty is not None and pos_qty != 0
        has_tp = any(is_tp_order(oid) for oid in current)
        self._metrics.set_tp_active(symbol, has_position and has_tp)

        self._prev_orders = current
        return actions

    def _handle_tp_expiry(
        self,
        current: dict[str, OpenOrderSnap],
        symbol: str,
        ts_ms: int,
        pos_qty: Decimal | None,
    ) -> list[ExecutionAction]:
        """Handle TP orders that exceed TTL: cancel or auto-renew (PR-INV-3b + PR-TP-RENEW).

        When tp_renew_enabled and pos_qty != 0: expired TP is cancelled and
        immediately replaced with a new TP at the same price (cancel+place).

        When renew disabled or pos_qty is None/0: plain cancel (legacy behavior).

        Guards against churn:
        - Cooldown: minimum interval between renewals per symbol.
        - Inflight latch: prevents concurrent renewals for same symbol.
        - Retry budget: max consecutive failures before degrading to plain cancel.
        """
        ttl = self._config.tp_ttl_ms
        if not ttl or ttl <= 0:
            return []

        has_position = pos_qty is not None and pos_qty != 0
        renew_enabled = self._config.tp_renew_enabled and has_position

        # PR-P0-TP-RENEW-OVERALLOC-GUARD: compute total TP qty for overalloc check
        tp_sum_qty = Decimal("0")
        if renew_enabled:
            for _oid, _snap in current.items():
                if _snap.symbol == symbol and is_tp_order(_oid):
                    tp_sum_qty += _snap.qty

        actions: list[ExecutionAction] = []
        for oid, snap in current.items():
            if snap.symbol != symbol:
                continue
            if not is_tp_order(oid):
                continue
            created_ts = self._tp_created_ts.get(oid)
            if created_ts is None:
                continue  # Not ours (fail-closed)
            if ts_ms - created_ts <= ttl:
                continue  # Not expired yet

            age_ms = ts_ms - created_ts
            self._metrics.record_tp_expired(symbol)

            if renew_enabled:
                renew_actions = self._try_renew_tp(
                    oid, snap, symbol, ts_ms, age_ms, pos_qty, tp_sum_qty
                )
                actions.extend(renew_actions)
            else:
                # Legacy: plain cancel
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        symbol=symbol,
                        order_id=oid,
                        reason="TP_EXPIRED",
                    )
                )
                self._tp_created_ts.pop(oid, None)
                self._tp_source_price.pop(oid, None)
                logger.info(
                    "TP expired: order_id=%s age_ms=%d ttl_ms=%d",
                    oid,
                    age_ms,
                    ttl,
                )
        return actions

    def _try_renew_tp(
        self,
        old_tp_id: str,
        snap: OpenOrderSnap,
        symbol: str,
        ts_ms: int,
        age_ms: int,
        pos_qty: Decimal | None = None,
        tp_sum_qty: Decimal = Decimal("0"),
    ) -> list[ExecutionAction]:
        """Attempt to renew an expired TP (cancel old + place new).

        PR-P0-TP-RENEW-OVERALLOC-GUARD: When multiple TPs exist and adding
        a new TP would exceed abs(pos_qty) (reduceOnly budget), use cancel-first
        order instead of place-first to avoid temporary over-allocation that
        causes Binance to auto-expire a different TP.

        Returns [PLACE, CANCEL] or [CANCEL, PLACE] on success, [CANCEL] on degradation.
        """
        ttl = self._config.tp_ttl_ms or 0

        # Guard 1: Inflight latch — don't start concurrent renewals
        if self._tp_renew_inflight.get(symbol, False):
            self._metrics.record_tp_renew(symbol, "inflight")
            logger.info(
                "TP_RENEW_SKIPPED_INFLIGHT symbol=%s tp_id=%s",
                symbol,
                old_tp_id,
            )
            return []

        # Guard 2: Cooldown — don't spam renewals
        last_renew = self._tp_renew_last_ts.get(symbol, 0)
        if ts_ms - last_renew < self._config.tp_renew_cooldown_ms:
            remaining = self._config.tp_renew_cooldown_ms - (ts_ms - last_renew)
            self._metrics.record_tp_renew(symbol, "cooldown")
            logger.info(
                "TP_RENEW_SKIPPED_COOLDOWN symbol=%s remaining_ms=%d",
                symbol,
                remaining,
            )
            return []

        # Guard 3: Retry budget — degrade after max attempts
        attempts = self._tp_renew_attempts.get(symbol, 0)
        if attempts >= self._config.tp_renew_max_attempts:
            self._metrics.record_tp_renew(symbol, "failed")
            logger.warning(
                "TP_RENEW_FAILED symbol=%s attempts_exhausted=%d — degrading to plain cancel",
                symbol,
                attempts,
            )
            self._tp_created_ts.pop(old_tp_id, None)
            self._tp_source_price.pop(old_tp_id, None)
            return [
                ExecutionAction(
                    action_type=ActionType.CANCEL,
                    symbol=symbol,
                    order_id=old_tp_id,
                    reason="TP_EXPIRED",
                )
            ]

        # Retrieve source fill price for renewal
        source = self._tp_source_price.get(old_tp_id)
        if source is None:
            # No source price — can't renew, plain cancel
            logger.warning(
                "TP_RENEW_FAILED symbol=%s tp_id=%s — no source price, plain cancel",
                symbol,
                old_tp_id,
            )
            self._tp_created_ts.pop(old_tp_id, None)
            return [
                ExecutionAction(
                    action_type=ActionType.CANCEL,
                    symbol=symbol,
                    order_id=old_tp_id,
                    reason="TP_EXPIRED",
                )
            ]

        fill_price, fill_side = source

        # Set inflight latch
        self._tp_renew_inflight[symbol] = True
        self._metrics.record_tp_renew(symbol, "started")
        logger.info(
            "TP_RENEW_STARTED symbol=%s old_tp_id=%s age_ms=%d ttl_ms=%d mode=cancel_place",
            symbol,
            old_tp_id,
            age_ms,
            ttl,
        )

        # Step 1: CANCEL old TP
        cancel_action = ExecutionAction(
            action_type=ActionType.CANCEL,
            symbol=symbol,
            order_id=old_tp_id,
            reason="TP_RENEW",
        )

        # Step 2: PLACE new TP at same price
        tp_side = OrderSide.SELL if fill_side.upper() == "BUY" else OrderSide.BUY
        tp_price = self._compute_tp_price(fill_price, fill_side)
        self._tp_seq += 1
        # Parse old TP for level_id
        parsed = parse_client_order_id(old_tp_id)
        level_id = int(parsed.level_id) if parsed and parsed.level_id.isdigit() else 0
        new_tp_id = generate_client_order_id(
            config=self._tp_identity,
            symbol=symbol,
            level_id=level_id,
            ts=ts_ms,
            seq=self._tp_seq,
        )

        place_action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol=symbol,
            side=tp_side,
            price=tp_price,
            quantity=snap.qty,
            level_id=level_id,
            reason="TP_RENEW",
            reduce_only=True,
            client_order_id=new_tp_id,
        )

        # Update tracking: transfer source price to new TP, clean old
        self._tp_created_ts.pop(old_tp_id, None)
        self._tp_source_price.pop(old_tp_id, None)
        self._tp_created_ts[new_tp_id] = ts_ms
        self._tp_source_price[new_tp_id] = (fill_price, fill_side)
        self._tp_renew_last_ts[symbol] = ts_ms
        # Clear inflight — actions emitted, execution pipeline handles them
        self._tp_renew_inflight[symbol] = False
        # Increment attempt counter (tracks total renewals, not consecutive failures)
        self._tp_renew_attempts[symbol] = self._tp_renew_attempts.get(symbol, 0) + 1

        # PR-P0-TP-RENEW-OVERALLOC-GUARD: decide action order.
        # With multiple TPs, place-first can cause temporary reduceOnly
        # over-allocation → Binance auto-expires a different TP (Run #16 bug).
        # cancel-first is safe when other TPs cover the gap.
        # Single TP: place-first with PR-367 guard (engine skips CANCEL on -2022).
        pos_abs = abs(pos_qty) if pos_qty is not None else Decimal("0")
        place_first_overalloc = tp_sum_qty + snap.qty > pos_abs
        other_tps_exist = tp_sum_qty > snap.qty

        mode = "cancel_first" if place_first_overalloc and other_tps_exist else "place_first"

        self._metrics.record_tp_renew(symbol, "renewed")
        logger.info(
            "TP_RENEWED symbol=%s old=%s new=%s price=%s attempt=%d mode=%s",
            symbol,
            old_tp_id,
            new_tp_id,
            tp_price,
            self._tp_renew_attempts[symbol],
            mode,
        )

        if mode == "cancel_first":
            return [cancel_action, place_action]
        # place-first: engine skips CANCEL if PLACE was blocked (PR-367)
        return [place_action, cancel_action]

    def _cleanup_tp_created_ts(self, current: dict[str, OpenOrderSnap], ts_ms: int) -> None:
        """Remove tp_created_ts entries for TPs no longer in open_orders.

        Two cleanup paths:
        1. Primary: TP was previously seen in open_orders (prev_orders) but
           disappeared — filled by exchange.
        2. Secondary (stale): TP was generated but never appeared in open_orders
           (e.g., exchange rejected the order). Time-based eviction prevents
           unbounded growth of _tp_created_ts.

        Stale TTL formula:
        - tp_ttl_ms enabled: max(2 * tp_ttl_ms, 60_000)
        - tp_ttl_ms disabled (None/0): 600_000 (10 minutes)
        """
        stale_ttl = self._stale_tp_created_ttl_ms()
        to_delete: list[str] = []
        for oid, created_ts in self._tp_created_ts.items():
            if oid in current:
                continue  # still on exchange, keep tracking
            # Path 1: previously seen in open_orders, now gone (filled)
            if oid in self._prev_orders:
                to_delete.append(oid)
                continue
            # Path 2: never appeared in open_orders, stale by time
            age_ms = ts_ms - created_ts
            if age_ms > stale_ttl:
                logger.info(
                    "TP_CREATED_STALE_CLEANUP: tp_id=%s age_ms=%d stale_ttl_ms=%d",
                    oid,
                    age_ms,
                    stale_ttl,
                )
                to_delete.append(oid)
        for oid in to_delete:
            del self._tp_created_ts[oid]
            self._tp_source_price.pop(oid, None)

    def _stale_tp_created_ttl_ms(self) -> int:
        """Compute stale TTL for tp_created_ts cleanup.

        Returns:
            max(2 * tp_ttl_ms, 60_000) if TTL enabled, else 600_000.
        """
        ttl = self._config.tp_ttl_ms
        if not ttl or ttl <= 0:
            return _STALE_TP_CREATED_FALLBACK_MS
        return max(2 * ttl, _STALE_TP_CREATED_MIN_MS)

    def _generate_replenish(
        self,
        fills: list[tuple[str, OpenOrderSnap, int]],
        symbol: str,
        mid_price: Decimal,
        ts_ms: int,
    ) -> list[ExecutionAction]:
        """Generate replenish PLACE actions for filled grid levels (PR-INV-4).

        Restores grid depth by placing a new order on the same side at the next
        level further from center. Replenish = INCREASE_RISK, blocked by gate
        chain in non-ACTIVE states (defense in depth).

        Conditions (fail-closed):
        - replenish_enabled in config
        - replenish_max_levels > 0
        - mid_price > 0 (need center reference)
        - source level_id is numeric (already filtered in Phase 1)
        - level_id + 1 <= max_levels

        Args:
            fills: List of (source_oid, source_snap, source_level_id) from Phase 1.
            symbol: Trading symbol.
            mid_price: Current mid price (center for level pricing).
            ts_ms: Current timestamp in milliseconds.

        Returns:
            List of replenish PLACE ExecutionActions.
        """
        if not self._config.replenish_enabled:
            return []
        max_levels = self._config.replenish_max_levels
        if max_levels <= 0:
            return []
        if mid_price <= 0:
            return []

        replenish_actions: list[ExecutionAction] = []
        for _source_oid, snap, source_level_id in fills:
            next_level = source_level_id + 1
            if next_level > max_levels:
                logger.debug(
                    "Replenish skipped: level %d+1=%d > max_levels=%d",
                    source_level_id,
                    next_level,
                    max_levels,
                )
                continue

            # Same side as filled order (restore that side of the grid)
            replenish_side = OrderSide.BUY if snap.side.upper() == "BUY" else OrderSide.SELL
            replenish_price = self._compute_replenish_price(mid_price, replenish_side, next_level)

            self._replenish_seq += 1
            replenish_client_id = generate_client_order_id(
                config=self._grid_identity,
                symbol=symbol,
                level_id=next_level,
                ts=ts_ms,
                seq=self._replenish_seq,
            )

            replenish_actions.append(
                ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=symbol,
                    side=replenish_side,
                    price=replenish_price,
                    quantity=snap.qty,
                    level_id=next_level,
                    reason="REPLENISH",
                    reduce_only=False,
                    client_order_id=replenish_client_id,
                )
            )
            self._metrics.record_replenish_generated(symbol)

            logger.info(
                "Replenish generated: src_level=%d side=%s price=%s qty=%s -> id=%s",
                source_level_id,
                replenish_side.value,
                replenish_price,
                snap.qty,
                replenish_client_id,
            )

        return replenish_actions

    def _compute_replenish_price(self, mid_price: Decimal, side: OrderSide, level: int) -> Decimal:
        """Compute replenish price at given level from center.

        BUY level k: mid_price * (1 - k * spacing_bps/10000)
        SELL level k: mid_price * (1 + k * spacing_bps/10000)
        """
        spacing = Decimal(str(self._config.spacing_bps)) / Decimal("10000")
        level_offset = spacing * Decimal(str(level))
        if side == OrderSide.BUY:
            raw = mid_price * (Decimal("1") - level_offset)
        else:
            raw = mid_price * (Decimal("1") + level_offset)
        tick = self._config.tick_size
        if tick and tick > 0:
            return (raw / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick
        return raw

    def _compute_tp_price(self, fill_price: Decimal, fill_side: str) -> Decimal:
        """Compute TP price offset from fill price.

        BUY fill -> SELL TP above fill price.
        SELL fill -> BUY TP below fill price.
        """
        spacing = Decimal(str(self._config.spacing_bps)) / Decimal("10000")
        if fill_side.upper() == "BUY":
            raw = fill_price * (Decimal("1") + spacing)
        else:
            raw = fill_price * (Decimal("1") - spacing)
        tick = self._config.tick_size
        if tick and tick > 0:
            return (raw / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick
        return raw

    def _compute_tp_qty(self, fill_qty: Decimal, pos_qty: Decimal | None) -> Decimal:
        """Compute TP quantity based on tp_qty_mode (PR-TP-PARTIAL).

        Modes:
        - full: fill_qty (current behavior, default)
        - one_level: per_level_qty (capped at abs(pos_qty) if known)
        - pct: abs(pos_qty) * tp_qty_pct / 100 (capped at abs(pos_qty))

        Invariants:
        - result <= abs(pos_qty) if pos_qty is known
        - result >= step_size (else return 0 — too small, caller skips TP)
        - result rounded down to step_size
        - All arithmetic is Decimal-only (no floats)

        Returns:
            Decimal > 0 on success, Decimal("0") when partial qty too small.
        """
        mode = self._config.tp_qty_mode
        step = self._config.step_size

        if mode == "one_level":
            plq = self._config.per_level_qty
            if plq is None or plq <= 0:
                return fill_qty  # fallback to full — missing config
            raw = plq
        elif mode == "pct":
            if pos_qty is None or pos_qty == 0:
                return fill_qty  # fallback — can't compute pct of unknown/zero
            pct = Decimal(self._config.tp_qty_pct) / Decimal("100")
            raw = abs(pos_qty) * pct
        else:
            return fill_qty  # mode="full"

        # Cap at abs(pos_qty)
        if pos_qty is not None and pos_qty != 0:
            raw = min(raw, abs(pos_qty))

        # Round down to step_size
        if step and step > 0:
            raw = (raw / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step

        # Floor: must be >= step_size (else too small to place)
        if step and step > 0 and raw < step:
            return Decimal("0")  # too small — caller skips TP

        return raw
