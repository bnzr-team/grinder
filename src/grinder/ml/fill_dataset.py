"""Fill outcome dataset v1 (Track C, PR-C1).

Transforms a sequence of paper Fill objects into a table of completed
roundtrip outcomes.  Each row captures one entry-to-exit cycle for a
(symbol, direction) pair, with fields designed for downstream
fill-probability and consecutive-loss-limit models.

Roundtrip rules
---------------
* Group fills by (symbol, side-direction).
  - BUY fills increase long position; SELL fills increase short position.
* Entry: open_qty transitions from 0 to non-zero.
* Exit: open_qty transitions from non-zero back to 0.
* Partial adds/reduces do NOT emit a row; only full close does.
* PnL follows the Ledger pattern: (exit_price - avg_entry_price) * qty * direction.

Determinism
-----------
* ``row_id`` = sha1 of canonical pipe-separated fields.
* Parquet written with ``write_statistics=False``, ``compression="snappy"``.
* Manifest includes sha256 of ``data.parquet``.

SSOT: this module.  ADR-068 in docs/DECISIONS.md.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from grinder.paper.fills import Fill

# --- FillOutcomeRow --------------------------------------------------------


@dataclass(frozen=True)
class FillOutcomeRow:
    """One completed roundtrip (entry -> exit).

    Identification
    """

    row_id: str
    symbol: str
    direction: str  # "long" or "short"

    # Entry
    entry_ts: int  # first fill ts (ms)
    entry_price: Decimal  # weighted avg entry price
    entry_qty: Decimal  # total entry quantity
    entry_fee: Decimal  # sum of fees on entry fills
    entry_fill_count: int  # number of fills that built the position

    # Exit
    exit_ts: int  # last fill ts (ms)
    exit_price: Decimal  # weighted avg exit price
    exit_qty: Decimal  # total exit quantity (should == entry_qty)
    exit_fee: Decimal  # sum of fees on exit fills
    exit_fill_count: int  # number of fills that closed the position

    # PnL
    realized_pnl: Decimal  # (exit - entry) * qty * direction_sign
    net_pnl: Decimal  # realized_pnl - entry_fee - exit_fee
    pnl_bps: int  # net_pnl / notional * 10_000, rounded

    # Context
    holding_time_ms: int  # exit_ts - entry_ts
    notional: Decimal  # entry_price * entry_qty
    outcome: str  # "win", "loss", or "breakeven"

    # Metadata
    source: str  # "paper" (for now)
    dataset_version: str  # "v1"

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict (Decimal -> str)."""
        return {
            "row_id": self.row_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_ts": self.entry_ts,
            "entry_price": str(self.entry_price),
            "entry_qty": str(self.entry_qty),
            "entry_fee": str(self.entry_fee),
            "entry_fill_count": self.entry_fill_count,
            "exit_ts": self.exit_ts,
            "exit_price": str(self.exit_price),
            "exit_qty": str(self.exit_qty),
            "exit_fee": str(self.exit_fee),
            "exit_fill_count": self.exit_fill_count,
            "realized_pnl": str(self.realized_pnl),
            "net_pnl": str(self.net_pnl),
            "pnl_bps": self.pnl_bps,
            "holding_time_ms": self.holding_time_ms,
            "notional": str(self.notional),
            "outcome": self.outcome,
            "source": self.source,
            "dataset_version": self.dataset_version,
        }


# --- Deterministic row_id --------------------------------------------------


def _compute_row_id(
    symbol: str,
    direction: str,
    entry_ts: int,
    exit_ts: int,
    entry_price: Decimal,
    exit_price: Decimal,
    qty: Decimal,
) -> str:
    """Compute deterministic row_id as sha1 of canonical fields."""
    canonical = f"{symbol}|{direction}|{entry_ts}|{exit_ts}|{entry_price}|{exit_price}|{qty}"
    return hashlib.sha1(canonical.encode()).hexdigest()


# --- RoundtripTracker -------------------------------------------------------


@dataclass
class _OpenPosition:
    """Accumulator for an in-progress position."""

    direction: str  # "long" or "short"
    qty: Decimal = field(default_factory=lambda: Decimal("0"))
    cost: Decimal = field(default_factory=lambda: Decimal("0"))  # sum(price * qty)
    fee: Decimal = field(default_factory=lambda: Decimal("0"))
    fill_count: int = 0
    first_ts: int = 0

    # Exit accumulator (for partial closes that build up to full close)
    exit_qty: Decimal = field(default_factory=lambda: Decimal("0"))
    exit_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    exit_fee: Decimal = field(default_factory=lambda: Decimal("0"))
    exit_fill_count: int = 0
    last_exit_ts: int = 0


class RoundtripTracker:
    """Tracks fill events and emits FillOutcomeRow on position close.

    Usage::

        tracker = RoundtripTracker()
        for fill in fills:
            row = tracker.record(fill)
            if row is not None:
                rows.append(row)
        # Also flush any open positions at the end if desired
    """

    def __init__(self, source: str = "paper") -> None:
        self._positions: dict[tuple[str, str], _OpenPosition] = {}
        self._source = source

    def record(self, fill: Fill, fee: Decimal | None = None) -> FillOutcomeRow | None:
        """Record a fill.  Returns a FillOutcomeRow if the position closes.

        Args:
            fill: A paper Fill object (ts, symbol, side, price, quantity).
            fee: Optional fee for this fill (default 0).

        Returns:
            FillOutcomeRow if position went from non-zero to zero, else None.
        """
        if fee is None:
            fee = Decimal("0")

        # Determine direction: BUY opens long, SELL opens short
        # But a BUY can also *close* a short position
        symbol = fill.symbol
        side = fill.side.upper()

        # Check if this fill closes an opposite position
        if side == "BUY":
            close_key = (symbol, "short")
            open_key = (symbol, "long")
        else:  # SELL
            close_key = (symbol, "long")
            open_key = (symbol, "short")

        # Try to close existing opposite position first
        if close_key in self._positions:
            pos = self._positions[close_key]
            pos.exit_qty += fill.quantity
            pos.exit_cost += fill.price * fill.quantity
            pos.exit_fee += fee
            pos.exit_fill_count += 1
            pos.last_exit_ts = fill.ts

            if pos.exit_qty >= pos.qty:
                # Position fully closed -> emit row
                row = self._emit_row(pos, symbol)
                del self._positions[close_key]
                return row
            return None

        # Otherwise, open or add to same-direction position
        direction = "long" if side == "BUY" else "short"
        key = open_key

        if key not in self._positions:
            self._positions[key] = _OpenPosition(
                direction=direction,
                qty=fill.quantity,
                cost=fill.price * fill.quantity,
                fee=fee,
                fill_count=1,
                first_ts=fill.ts,
            )
        else:
            pos = self._positions[key]
            pos.qty += fill.quantity
            pos.cost += fill.price * fill.quantity
            pos.fee += fee
            pos.fill_count += 1
            if pos.first_ts == 0:
                pos.first_ts = fill.ts

        return None

    def _emit_row(self, pos: _OpenPosition, symbol: str) -> FillOutcomeRow:
        """Build a FillOutcomeRow from a fully closed position."""
        entry_price = pos.cost / pos.qty if pos.qty else Decimal("0")
        exit_price = pos.exit_cost / pos.exit_qty if pos.exit_qty else Decimal("0")

        direction_sign = Decimal("1") if pos.direction == "long" else Decimal("-1")
        realized_pnl = (exit_price - entry_price) * pos.qty * direction_sign
        net_pnl = realized_pnl - pos.fee - pos.exit_fee

        notional = entry_price * pos.qty
        if notional != Decimal("0"):
            pnl_bps = int((net_pnl / notional * 10000).to_integral_value())
        else:
            pnl_bps = 0

        holding_time_ms = pos.last_exit_ts - pos.first_ts

        if net_pnl > 0:
            outcome = "win"
        elif net_pnl < 0:
            outcome = "loss"
        else:
            outcome = "breakeven"

        row_id = _compute_row_id(
            symbol=symbol,
            direction=pos.direction,
            entry_ts=pos.first_ts,
            exit_ts=pos.last_exit_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=pos.qty,
        )

        return FillOutcomeRow(
            row_id=row_id,
            symbol=symbol,
            direction=pos.direction,
            entry_ts=pos.first_ts,
            entry_price=entry_price,
            entry_qty=pos.qty,
            entry_fee=pos.fee,
            entry_fill_count=pos.fill_count,
            exit_ts=pos.last_exit_ts,
            exit_price=exit_price,
            exit_qty=pos.exit_qty,
            exit_fee=pos.exit_fee,
            exit_fill_count=pos.exit_fill_count,
            realized_pnl=realized_pnl,
            net_pnl=net_pnl,
            pnl_bps=pnl_bps,
            holding_time_ms=holding_time_ms,
            notional=notional,
            outcome=outcome,
            source=self._source,
            dataset_version="v1",
        )

    @property
    def open_positions(self) -> dict[tuple[str, str], _OpenPosition]:
        """Read-only view of currently open positions (for diagnostics)."""
        return dict(self._positions)


# --- Dataset builder --------------------------------------------------------

# Column order for parquet (deterministic)
FILL_OUTCOME_COLUMNS: tuple[str, ...] = (
    "row_id",
    "symbol",
    "direction",
    "entry_ts",
    "entry_price",
    "entry_qty",
    "entry_fee",
    "entry_fill_count",
    "exit_ts",
    "exit_price",
    "exit_qty",
    "exit_fee",
    "exit_fill_count",
    "realized_pnl",
    "net_pnl",
    "pnl_bps",
    "holding_time_ms",
    "notional",
    "outcome",
    "source",
    "dataset_version",
)

# Column type categories for parquet schema
_FLOAT64_COLS: frozenset[str] = frozenset(
    {
        "entry_price",
        "entry_qty",
        "entry_fee",
        "exit_price",
        "exit_qty",
        "exit_fee",
        "realized_pnl",
        "net_pnl",
        "notional",
    }
)
_INT64_COLS: frozenset[str] = frozenset(
    {
        "entry_ts",
        "exit_ts",
        "holding_time_ms",
    }
)
_INT32_COLS: frozenset[str] = frozenset(
    {
        "entry_fill_count",
        "exit_fill_count",
        "pnl_bps",
    }
)


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _rows_to_table(
    rows: list[FillOutcomeRow],
) -> Any:
    """Convert FillOutcomeRow list to a pyarrow Table."""
    import pyarrow as pa  # noqa: PLC0415

    if not rows:
        schema = pa.schema([(col, _pa_type_for_col(col, pa)) for col in FILL_OUTCOME_COLUMNS])
        return pa.table(
            {col: pa.array([], type=schema.field(col).type) for col in FILL_OUTCOME_COLUMNS},
            schema=schema,
        )

    columns: dict[str, list[Any]] = {col: [] for col in FILL_OUTCOME_COLUMNS}
    for row in rows:
        d = row.to_dict()
        for col in FILL_OUTCOME_COLUMNS:
            columns[col].append(d[col])

    pa_columns: dict[str, pa.Array] = {}
    for col in FILL_OUTCOME_COLUMNS:
        vals = columns[col]
        pa_type = _pa_type_for_col(col, pa)
        if col in _FLOAT64_COLS:
            pa_columns[col] = pa.array([float(v) for v in vals], type=pa_type)
        else:
            pa_columns[col] = pa.array(vals, type=pa_type)

    return pa.table(pa_columns)


def _pa_type_for_col(col: str, pa: Any) -> Any:
    """Return the pyarrow type for a column name."""
    if col in _FLOAT64_COLS:
        return pa.float64()
    if col in _INT64_COLS:
        return pa.int64()
    if col in _INT32_COLS:
        return pa.int32()
    return pa.string()


def build_fill_dataset_v1(
    rows: list[FillOutcomeRow],
    out_dir: Path,
    *,
    dataset_id: str = "fill_outcomes_v1",
    force: bool = False,
    created_at_utc: str | None = None,
) -> Path:
    """Write fill outcome rows to parquet + manifest.

    Args:
        rows: List of FillOutcomeRow objects.
        out_dir: Parent directory (e.g. ``ml/datasets/fill_outcomes/v1``).
        dataset_id: Identifier for the dataset.
        force: Overwrite existing directory.
        created_at_utc: Override timestamp (for deterministic tests).

    Returns:
        Path to the created dataset directory.

    Raises:
        FileExistsError: If directory exists and force=False.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415

    dataset_dir = out_dir / dataset_id

    if dataset_dir.exists():
        if not force:
            raise FileExistsError(
                f"Dataset directory already exists: {dataset_dir} (use --force to overwrite)"
            )
        shutil.rmtree(dataset_dir)

    dataset_dir.mkdir(parents=True)

    table = _rows_to_table(rows)

    # Write parquet (deterministic settings)
    data_path = dataset_dir / "data.parquet"
    pq.write_table(table, data_path, compression="snappy", write_statistics=False)

    # Build manifest
    if created_at_utc is None:
        created_at_utc = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest: dict[str, object] = {
        "schema_version": "fill_outcomes_v1",
        "dataset_id": dataset_id,
        "created_at_utc": created_at_utc,
        "source": "paper",
        "row_count": len(rows),
        "columns": list(FILL_OUTCOME_COLUMNS),
        "sha256": {
            "data.parquet": _sha256_file(data_path),
        },
    }

    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    return dataset_dir
