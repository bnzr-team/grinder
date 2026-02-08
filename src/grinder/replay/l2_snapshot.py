"""L2 Snapshot JSONL v0 parser and validator.

Implements the SSOT contract defined in:
  docs/smart_grid/SPEC_V2_0.md, Addendum B: L2 Snapshot JSONL v0 Protocol

Design:
- Frozen dataclass for immutability
- Decimal parsing for determinism (no float drift)
- Strict invariant validation with non-retryable errors
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# === Constants (from SPEC B.2) ===

QTY_REF_BASELINE = Decimal("0.003")
"""Reference quantity for impact calculation (BTC equivalent)."""

IMPACT_INSUFFICIENT_DEPTH_BPS = 500
"""Sentinel value when depth exhausted before filling qty_ref."""


# === Exceptions ===


class L2ParseError(Exception):
    """Non-retryable error during L2 snapshot parsing/validation.

    Raised when:
    - JSON is malformed
    - Required fields missing
    - Schema version unsupported
    - Invariants violated (sorting, qty > 0, depth mismatch)
    """

    pass


# === BookLevel type ===


@dataclass(frozen=True)
class BookLevel:
    """Single level in the order book."""

    price: Decimal
    qty: Decimal

    def to_tuple(self) -> tuple[str, str]:
        """Convert to JSONL format [price_str, qty_str]."""
        return (str(self.price), str(self.qty))


# === L2Snapshot dataclass ===


@dataclass(frozen=True)
class L2Snapshot:
    """L2 order book snapshot (JSONL v0).

    Immutable representation of a depth snapshot for deterministic replay.

    Attributes:
        ts_ms: Timestamp in milliseconds since epoch
        symbol: Trading pair (e.g., "BTCUSDT")
        venue: Exchange identifier (e.g., "binance_futures_usdtm")
        depth: Number of levels on each side
        bids: Bid levels sorted descending by price
        asks: Ask levels sorted ascending by price
        meta: Optional metadata dict

    Invariants (enforced at parse time):
        - bids: prices strictly descending
        - asks: prices strictly ascending
        - All quantities > 0
        - len(bids) == len(asks) == depth
    """

    ts_ms: int
    symbol: str
    venue: str
    depth: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    # Fixed schema fields
    type: str = field(default="l2_snapshot", repr=False)
    v: int = field(default=0, repr=False)

    @property
    def best_bid(self) -> Decimal | None:
        """Best bid price, or None if no bids."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        """Best ask price, or None if no asks."""
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Decimal | None:
        """Mid price, or None if no bids/asks."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict (JSONL v0 format)."""
        return {
            "type": self.type,
            "v": self.v,
            "ts_ms": self.ts_ms,
            "symbol": self.symbol,
            "venue": self.venue,
            "depth": self.depth,
            "bids": [level.to_tuple() for level in self.bids],
            "asks": [level.to_tuple() for level in self.asks],
            "meta": self.meta,
        }

    def to_json(self) -> str:
        """Serialize to JSONL line (compact, no trailing newline)."""
        return json.dumps(self.to_dict(), separators=(",", ":"))


# === Parser ===


def _parse_levels(raw_levels: list[list[str]], side: str) -> tuple[BookLevel, ...]:
    """Parse raw [[price, qty], ...] into BookLevel tuple.

    Args:
        raw_levels: List of [price_str, qty_str] pairs
        side: "bids" or "asks" (for error messages)

    Returns:
        Tuple of BookLevel objects

    Raises:
        L2ParseError: If parsing fails
    """
    levels = []
    for i, level in enumerate(raw_levels):
        if not isinstance(level, list) or len(level) != 2:
            raise L2ParseError(f"{side}[{i}]: expected [price, qty], got {level!r}")
        price_str, qty_str = level
        try:
            price = Decimal(price_str)
            qty = Decimal(qty_str)
        except (InvalidOperation, TypeError) as e:
            raise L2ParseError(
                f"{side}[{i}]: invalid decimal - price={price_str!r}, qty={qty_str!r}: {e}"
            ) from e
        levels.append(BookLevel(price=price, qty=qty))
    return tuple(levels)


def _validate_sorting(levels: tuple[BookLevel, ...], side: str) -> None:
    """Validate that levels are properly sorted.

    Args:
        levels: Parsed levels
        side: "bids" (descending) or "asks" (ascending)

    Raises:
        L2ParseError: If sorting invariant violated
    """
    if len(levels) < 2:
        return

    for i in range(1, len(levels)):
        prev_price = levels[i - 1].price
        curr_price = levels[i].price

        if side == "bids":
            # Bids must be strictly descending
            if curr_price >= prev_price:
                raise L2ParseError(
                    f"bids: prices not strictly descending at [{i - 1}]→[{i}]: "
                    f"{prev_price} → {curr_price}"
                )
        elif curr_price <= prev_price:
            # Asks must be strictly ascending
            raise L2ParseError(
                f"asks: prices not strictly ascending at [{i - 1}]→[{i}]: "
                f"{prev_price} → {curr_price}"
            )


def _validate_quantities(levels: tuple[BookLevel, ...], side: str) -> None:
    """Validate that all quantities are positive.

    Args:
        levels: Parsed levels
        side: "bids" or "asks" (for error messages)

    Raises:
        L2ParseError: If any quantity <= 0
    """
    for i, level in enumerate(levels):
        if level.qty <= 0:
            raise L2ParseError(f"{side}[{i}]: qty must be > 0, got {level.qty}")


def _validate_header(data: dict[str, Any]) -> None:
    """Validate type and version fields."""
    record_type = data.get("type")
    if record_type != "l2_snapshot":
        raise L2ParseError(f"Expected type='l2_snapshot', got {record_type!r}")

    version = data.get("v")
    if version != 0:
        raise L2ParseError(f"Unsupported schema version: v={version}, expected v=0")


def _validate_field_types(data: dict[str, Any]) -> None:
    """Validate types of required fields."""
    ts_ms = data["ts_ms"]
    symbol = data["symbol"]
    venue = data["venue"]
    depth = data["depth"]
    raw_bids = data["bids"]
    raw_asks = data["asks"]

    type_checks: list[tuple[Any, type, str]] = [
        (ts_ms, int, "ts_ms"),
        (symbol, str, "symbol"),
        (venue, str, "venue"),
        (depth, int, "depth"),
        (raw_bids, list, "bids"),
        (raw_asks, list, "asks"),
    ]

    for value, expected_type, field_name in type_checks:
        if not isinstance(value, expected_type):
            raise L2ParseError(
                f"{field_name} must be {expected_type.__name__}, got {type(value).__name__}"
            )


def parse_l2_snapshot_line(line: str) -> L2Snapshot:
    """Parse a single JSONL line into L2Snapshot.

    Performs full schema validation and invariant checks.

    Args:
        line: JSON string (single line, no newline)

    Returns:
        Validated L2Snapshot

    Raises:
        L2ParseError: If parsing or validation fails
    """
    # Parse JSON
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        raise L2ParseError(f"Invalid JSON: {e}") from e

    if not isinstance(data, dict):
        raise L2ParseError(f"Expected JSON object, got {type(data).__name__}")

    # Validate header (type, version)
    _validate_header(data)

    # Check required fields exist
    required = ["ts_ms", "symbol", "venue", "depth", "bids", "asks"]
    for key in required:
        if key not in data:
            raise L2ParseError(f"Missing required field: {key!r}")

    # Validate field types
    _validate_field_types(data)

    # Parse levels
    bids = _parse_levels(data["bids"], "bids")
    asks = _parse_levels(data["asks"], "asks")
    depth = data["depth"]

    # Validate depth consistency
    if len(bids) != depth:
        raise L2ParseError(f"depth mismatch: depth={depth} but len(bids)={len(bids)}")
    if len(asks) != depth:
        raise L2ParseError(f"depth mismatch: depth={depth} but len(asks)={len(asks)}")

    # Validate book invariants
    _validate_sorting(bids, "bids")
    _validate_sorting(asks, "asks")
    _validate_quantities(bids, "bids")
    _validate_quantities(asks, "asks")

    # Extract optional meta
    meta = data.get("meta", {})
    if not isinstance(meta, dict):
        raise L2ParseError(f"meta must be dict, got {type(meta).__name__}")

    return L2Snapshot(
        ts_ms=data["ts_ms"],
        symbol=data["symbol"],
        venue=data["venue"],
        depth=depth,
        bids=bids,
        asks=asks,
        meta=meta,
    )


def load_l2_fixtures(path: str) -> list[L2Snapshot]:
    """Load L2 snapshots from a JSONL file.

    Args:
        path: Path to JSONL file

    Returns:
        List of L2Snapshot objects

    Raises:
        L2ParseError: If any line fails validation
        FileNotFoundError: If file doesn't exist
    """
    snapshots = []
    with Path(path).open(encoding="utf-8") as f:
        for line_num, raw_line in enumerate(f, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                snapshot = parse_l2_snapshot_line(stripped)
                snapshots.append(snapshot)
            except L2ParseError as e:
                raise L2ParseError(f"Line {line_num}: {e}") from e
    return snapshots
