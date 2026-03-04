"""LiveCycleLayerV1: fill detection + TP order generation (PR-INV-3).

Detects grid order fills by comparing AccountSync snapshots. When a grinder
grid order disappears without us cancelling it, generates a reduce-only TP
PLACE action on the opposite side with grinder_tp_ clientOrderId namespace.

Invariants:
- Only grid orders (strategy_id != "tp") are fill candidates
- Pending cancels excluded (TTL-based, 30s default)
- TP-on-TP impossible (strategy_id="tp" excluded from candidates)
- Idempotent: deterministic LRU dedup cache (OrderedDict, bounded)
- reduce_only=True is semantic invariant for TPs (Binance prevents position increase)
- V1 limitation: single tick_size for all symbols (BTCUSDT only in current C4)
- Contract: non-numeric level_id (e.g., "cleanup") -> TP level_id=0

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
        spacing_bps: TP price offset from fill price in basis points.
        tick_size: Tick size for price rounding (None = no rounding).
        tp_ttl_ms: TP order TTL in milliseconds. None or 0 = disabled (no expiry).
            Default: 300_000 (5 minutes).
    """

    spacing_bps: float = 10.0
    tick_size: Decimal | None = None
    tp_ttl_ms: int | None = 300_000


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
        self._tp_seq = 0
        # PR-INV-3b: Track TP creation timestamps for expiry
        self._tp_created_ts: dict[str, int] = {}
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

    def on_snapshot(
        self,
        *,
        symbol: str,
        open_orders: tuple[OpenOrderSnap, ...],
        mid_price: Decimal,  # noqa: ARG002 - reserved for future use
        ts_ms: int,
    ) -> list[ExecutionAction]:
        """Detect fills, generate TP PLACEs, and expire stale TPs.

        Args:
            symbol: Trading symbol to process.
            open_orders: All open orders for this symbol from AccountSync.
            mid_price: Current mid price (reserved for future TP pricing modes).
            ts_ms: Current timestamp in milliseconds.

        Returns:
            List of ExecutionActions: TP PLACEs + TP expiry CANCELs (may be empty).
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
            source_level_id: int = (
                int(parsed.level_id) if parsed and parsed.level_id.isdigit() else 0
            )

            # Generate TP on opposite side
            tp_side = OrderSide.SELL if snap.side.upper() == "BUY" else OrderSide.BUY
            tp_price = self._compute_tp_price(snap.price, snap.side)

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
                    quantity=snap.qty,
                    level_id=source_level_id,
                    reason="TP_CLOSE",
                    reduce_only=True,
                    client_order_id=tp_client_id,
                )
            )

            # PR-INV-3b: Track TP creation time for expiry
            self._tp_created_ts[tp_client_id] = ts_ms
            self._metrics.record_tp_generated(symbol)
            self._metrics.record_fill_candidate(symbol, "tp_generated")

            logger.info(
                "TP generated: src=%s side=%s price=%s qty=%s -> tp_id=%s",
                oid,
                tp_side.value,
                tp_price,
                snap.qty,
                tp_client_id,
            )

        # --- Phase 2: TP expiry → CANCEL stale TPs ---
        expiry_cancels = self._expire_stale_tps(current, symbol, ts_ms)
        actions.extend(expiry_cancels)

        # --- Phase 3: Clean up tp_created_ts for TPs no longer in open_orders ---
        # (not expired, just gone — e.g., filled by exchange, or rejected/stale)
        self._cleanup_tp_created_ts(current, ts_ms)

        self._prev_orders = current
        return actions

    def _expire_stale_tps(
        self,
        current: dict[str, OpenOrderSnap],
        symbol: str,
        ts_ms: int,
    ) -> list[ExecutionAction]:
        """Cancel TP orders that exceed TTL (PR-INV-3b).

        Only cancels TPs that:
        1. Have is_tp_order() == True
        2. Are tracked in _tp_created_ts (we created them)
        3. Have exceeded tp_ttl_ms

        Returns list of CANCEL ExecutionActions.
        """
        ttl = self._config.tp_ttl_ms
        if not ttl or ttl <= 0:
            return []

        cancels: list[ExecutionAction] = []
        for oid, snap in current.items():
            if snap.symbol != symbol:
                continue
            if not is_tp_order(oid):
                continue
            created_ts = self._tp_created_ts.get(oid)
            if created_ts is None:
                continue  # Not ours (fail-closed)
            if ts_ms - created_ts > ttl:
                cancels.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        symbol=symbol,
                        order_id=oid,
                        reason="TP_EXPIRED",
                    )
                )
                self._tp_created_ts.pop(oid, None)
                self._metrics.record_tp_expired(symbol)
                logger.info(
                    "TP expired: order_id=%s age_ms=%d ttl_ms=%d",
                    oid,
                    ts_ms - created_ts,
                    ttl,
                )
        return cancels

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

    def _stale_tp_created_ttl_ms(self) -> int:
        """Compute stale TTL for tp_created_ts cleanup.

        Returns:
            max(2 * tp_ttl_ms, 60_000) if TTL enabled, else 600_000.
        """
        ttl = self._config.tp_ttl_ms
        if not ttl or ttl <= 0:
            return _STALE_TP_CREATED_FALLBACK_MS
        return max(2 * ttl, _STALE_TP_CREATED_MIN_MS)

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
