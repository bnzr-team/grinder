"""Persistent fill cursor for deduplication across restarts (Launch-06 PR2).

Stores the last seen Binance trade ID so that the fill ingestion loop
does not re-read the same trades after a container restart.

Persistence format (JSON):
{
    "last_trade_id": 123456,
    "last_ts_ms": 1705320000000,
    "updated_at_ms": 1705320005000
}

File path is configurable via ``FILL_CURSOR_PATH`` env var.
If the file does not exist or is corrupt, the cursor starts fresh
(all trades in the lookback window are ingested).

Follows the BudgetTracker persistence pattern (budget.py).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class FillCursor:
    """Cursor state for fill ingestion.

    Attributes:
        last_trade_id: Binance trade ``id`` of the most recent ingested trade.
            Zero means "no cursor" — use lookback window.
        last_ts_ms: Timestamp (ms) of the most recent ingested trade.
        updated_at_ms: When this cursor was last persisted.
    """

    last_trade_id: int = 0
    last_ts_ms: int = 0
    updated_at_ms: int = 0


def load_fill_cursor(path: str) -> FillCursor:
    """Load cursor from disk.  Returns fresh cursor if file missing/corrupt."""
    p = Path(path)
    if not p.exists():
        logger.info("FILL_CURSOR_NOT_FOUND", extra={"path": path})
        return FillCursor()

    try:
        data = json.loads(p.read_text())
        cursor = FillCursor(
            last_trade_id=int(data.get("last_trade_id", 0)),
            last_ts_ms=int(data.get("last_ts_ms", 0)),
            updated_at_ms=int(data.get("updated_at_ms", 0)),
        )
        logger.info(
            "FILL_CURSOR_LOADED",
            extra={
                "path": path,
                "last_trade_id": cursor.last_trade_id,
                "last_ts_ms": cursor.last_ts_ms,
            },
        )
        return cursor
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        logger.warning("FILL_CURSOR_LOAD_ERROR", extra={"path": path, "error": str(e)})
        return FillCursor()


def save_fill_cursor(path: str, cursor: FillCursor, now_ms: int) -> None:
    """Persist cursor to disk (atomic-ish: write then close)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_trade_id": cursor.last_trade_id,
            "last_ts_ms": cursor.last_ts_ms,
            "updated_at_ms": now_ms,
        }
        p.write_text(json.dumps(payload, indent=2))
    except OSError as e:
        logger.error("FILL_CURSOR_SAVE_ERROR", extra={"path": path, "error": str(e)})
