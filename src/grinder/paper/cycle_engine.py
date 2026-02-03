"""CycleEngine: Fill → TP + replenishment for grid cycles.

CycleEngine converts fills into take-profit and replenishment orders:
- BUY fill at p_fill with qty → SELL TP at p_fill * (1 + step_pct) for same qty
- SELL fill at p_fill with qty → BUY TP at p_fill * (1 - step_pct) for same qty
- Replenishment: place new order further out to maintain grid levels (only if adds_allowed=True)

Determinism:
- Intent IDs are deterministic based on fill data
- Ordering is stable (fills processed in order, TPs before replenishment)

See: §17.12.2 in docs/17_ADAPTIVE_SMART_GRID_V1.md
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grinder.paper.fills import Fill


@dataclass(frozen=True)
class CycleIntent:
    """Intent to place a take-profit or replenishment order.

    Attributes:
        intent_type: "TP" for take-profit, "REPLENISH" for grid refill
        side: "BUY" or "SELL"
        price: Target price for the order
        quantity: Order quantity
        symbol: Trading symbol
        source_fill_id: Reference to the fill that triggered this intent
        intent_id: Deterministic ID for deduplication and tracking
    """

    intent_type: str
    side: str
    price: Decimal
    quantity: Decimal
    symbol: str
    source_fill_id: str
    intent_id: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "intent_type": self.intent_type,
            "side": self.side,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "symbol": self.symbol,
            "source_fill_id": self.source_fill_id,
            "intent_id": self.intent_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CycleIntent:
        """Create from dict."""
        return cls(
            intent_type=d["intent_type"],
            side=d["side"],
            price=Decimal(d["price"]),
            quantity=Decimal(d["quantity"]),
            symbol=d["symbol"],
            source_fill_id=d["source_fill_id"],
            intent_id=d["intent_id"],
        )


@dataclass
class CycleResult:
    """Result from CycleEngine processing.

    Attributes:
        intents: List of CycleIntent objects (TPs + replenishments)
        fills_processed: Number of fills processed
        tps_generated: Number of TP intents generated
        replenishments_generated: Number of replenishment intents generated
    """

    intents: list[CycleIntent]
    fills_processed: int
    tps_generated: int
    replenishments_generated: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "intents": [i.to_dict() for i in self.intents],
            "fills_processed": self.fills_processed,
            "tps_generated": self.tps_generated,
            "replenishments_generated": self.replenishments_generated,
        }


class CycleEngine:
    """Grid cycle engine: converts fills into TP and replenishment intents.

    The CycleEngine implements the core grid cycling logic:
    - Each fill triggers a take-profit order on the opposite side
    - If adds_allowed, a replenishment order is placed to maintain grid depth

    Thread-safe: No. Designed for single-threaded paper trading.
    Determinism: Yes. Intent IDs and ordering are deterministic.
    """

    def __init__(
        self,
        step_pct: Decimal = Decimal("0.001"),
        replenish_offset_pct: Decimal | None = None,
        price_precision: int = 2,
        quantity_precision: int = 3,
    ) -> None:
        """Initialize CycleEngine.

        Args:
            step_pct: Step percentage for TP placement (default 0.1% = 10 bps)
            replenish_offset_pct: Offset for replenishment orders (default: same as step_pct)
            price_precision: Decimal places for price rounding
            quantity_precision: Decimal places for quantity rounding
        """
        self._step_pct = step_pct
        self._replenish_offset_pct = (
            replenish_offset_pct if replenish_offset_pct is not None else step_pct
        )
        self._price_precision = price_precision
        self._quantity_precision = quantity_precision

    def _round_price(self, price: Decimal) -> Decimal:
        """Round price to configured precision."""
        quantize_str = "0." + "0" * self._price_precision
        return price.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)

    def _round_quantity(self, qty: Decimal) -> Decimal:
        """Round quantity to configured precision."""
        quantize_str = "0." + "0" * self._quantity_precision
        return qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)

    def _generate_intent_id(
        self,
        intent_type: str,
        source_fill_id: str,
        side: str,
        price: Decimal,
    ) -> str:
        """Generate deterministic intent ID.

        Format: cycle_{intent_type}_{source_fill_id}_{side}_{price}
        This ensures uniqueness and reproducibility.
        """
        return f"cycle_{intent_type}_{source_fill_id}_{side}_{price}"

    def _compute_tp_price(self, fill_price: Decimal, fill_side: str) -> Decimal:
        """Compute take-profit price based on fill side.

        BUY fill → SELL TP at fill_price * (1 + step_pct)
        SELL fill → BUY TP at fill_price * (1 - step_pct)
        """
        if fill_side == "BUY":
            # BUY fill → place SELL TP above fill price
            tp_price = fill_price * (Decimal("1") + self._step_pct)
        else:
            # SELL fill → place BUY TP below fill price
            tp_price = fill_price * (Decimal("1") - self._step_pct)
        return self._round_price(tp_price)

    def _compute_replenish_price(self, fill_price: Decimal, fill_side: str) -> Decimal:
        """Compute replenishment price (further out from fill).

        BUY fill → replenish BUY further below
        SELL fill → replenish SELL further above
        """
        if fill_side == "BUY":
            # BUY fill → place BUY replenishment below fill price
            replenish_price = fill_price * (Decimal("1") - self._replenish_offset_pct)
        else:
            # SELL fill → place SELL replenishment above fill price
            replenish_price = fill_price * (Decimal("1") + self._replenish_offset_pct)
        return self._round_price(replenish_price)

    def process_fills(
        self,
        fills: list[Fill],
        adds_allowed: bool = True,
    ) -> CycleResult:
        """Process fills and generate TP + replenishment intents.

        Args:
            fills: List of Fill objects from fill simulation
            adds_allowed: If True, generate replenishment intents; if False, TP only

        Returns:
            CycleResult with all generated intents

        Determinism notes:
        - Fills are processed in order
        - For each fill, TP intent is generated first, then replenishment (if allowed)
        - Intent IDs are deterministic based on fill data
        """
        intents: list[CycleIntent] = []
        tps_generated = 0
        replenishments_generated = 0

        for fill in fills:
            # Generate TP intent
            tp_side = "SELL" if fill.side == "BUY" else "BUY"
            tp_price = self._compute_tp_price(fill.price, fill.side)
            tp_quantity = self._round_quantity(fill.quantity)

            tp_intent_id = self._generate_intent_id("TP", fill.order_id, tp_side, tp_price)
            tp_intent = CycleIntent(
                intent_type="TP",
                side=tp_side,
                price=tp_price,
                quantity=tp_quantity,
                symbol=fill.symbol,
                source_fill_id=fill.order_id,
                intent_id=tp_intent_id,
            )
            intents.append(tp_intent)
            tps_generated += 1

            # Generate replenishment intent if adds_allowed
            if adds_allowed:
                replenish_side = fill.side  # Same side as fill
                replenish_price = self._compute_replenish_price(fill.price, fill.side)
                replenish_quantity = self._round_quantity(fill.quantity)

                replenish_intent_id = self._generate_intent_id(
                    "REPLENISH", fill.order_id, replenish_side, replenish_price
                )
                replenish_intent = CycleIntent(
                    intent_type="REPLENISH",
                    side=replenish_side,
                    price=replenish_price,
                    quantity=replenish_quantity,
                    symbol=fill.symbol,
                    source_fill_id=fill.order_id,
                    intent_id=replenish_intent_id,
                )
                intents.append(replenish_intent)
                replenishments_generated += 1

        return CycleResult(
            intents=intents,
            fills_processed=len(fills),
            tps_generated=tps_generated,
            replenishments_generated=replenishments_generated,
        )

    def process_single_fill(
        self,
        fill: Fill,
        adds_allowed: bool = True,
    ) -> list[CycleIntent]:
        """Process a single fill and return intents.

        Convenience method for processing one fill at a time.

        Args:
            fill: Single Fill object
            adds_allowed: If True, include replenishment intent

        Returns:
            List of CycleIntent objects (1 or 2 depending on adds_allowed)
        """
        result = self.process_fills([fill], adds_allowed=adds_allowed)
        return result.intents
