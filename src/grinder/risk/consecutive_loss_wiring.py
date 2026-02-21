"""Wire ConsecutiveLossGuard into the live reconciliation pipeline (PR-C3b).

Side-effect layer: env config, Binance trade conversion, evidence writing,
operator PAUSE action, and metrics push.  The guard itself stays pure.

Integration point: ``scripts/run_live_reconcile.py`` only.
Other entrypoints do NOT activate this guard.

SSOT: this module.  ADR-070 in docs/DECISIONS.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from grinder.env_parse import parse_bool, parse_int
from grinder.ml.fill_dataset import FillOutcomeRow, RoundtripTracker
from grinder.paper.fills import Fill
from grinder.risk.consecutive_loss_guard import (
    ConsecutiveLossAction,
    ConsecutiveLossConfig,
    ConsecutiveLossGuard,
)

logger = logging.getLogger(__name__)

ARTIFACT_VERSION = "consecutive_loss_evidence_v1"
ENV_ENABLE = "GRINDER_CONSEC_LOSS_ENABLED"
ENV_THRESHOLD = "GRINDER_CONSEC_LOSS_THRESHOLD"
ENV_EVIDENCE = "GRINDER_CONSEC_LOSS_EVIDENCE"
ENV_ARTIFACT_DIR = "GRINDER_ARTIFACT_DIR"


# --- Config ----------------------------------------------------------------


def load_consecutive_loss_config() -> ConsecutiveLossConfig:
    """Load guard config from environment variables.

    * ``GRINDER_CONSEC_LOSS_ENABLED`` (bool, default False)
    * ``GRINDER_CONSEC_LOSS_THRESHOLD`` (int, default 5, min 1)
    * Action is hardcoded PAUSE (no env var).
    """
    enabled = parse_bool(ENV_ENABLE, default=False, strict=False)
    threshold = parse_int(ENV_THRESHOLD, default=5, min_value=1, strict=False)
    assert threshold is not None  # default=5 guarantees non-None
    return ConsecutiveLossConfig(
        enabled=enabled,
        threshold=threshold,
        action=ConsecutiveLossAction.PAUSE,
    )


# --- Binance trade conversion ----------------------------------------------


def binance_trade_to_fill(raw: dict[str, Any]) -> Fill:
    """Convert a Binance userTrade dict to a paper Fill object.

    Raises KeyError / InvalidOperation on missing or invalid fields.
    """
    return Fill(
        ts=int(raw["time"]),
        symbol=raw["symbol"],
        side=raw["side"].upper(),
        price=Decimal(raw["price"]),
        quantity=Decimal(raw["qty"]),
        order_id=str(raw["orderId"]),
    )


def binance_trade_fee(raw: dict[str, Any]) -> Decimal:
    """Extract fee from a Binance userTrade dict."""
    return Decimal(raw["commission"])


# --- Evidence --------------------------------------------------------------


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically: tmp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def write_trip_evidence(
    guard: ConsecutiveLossGuard,
    outcome_row: FillOutcomeRow,
    ts_ms: int,
    config: ConsecutiveLossConfig,
) -> tuple[Path, Path] | None:
    """Write evidence artifact for a guard trip event.

    Only called on guard trip (not every update).
    Env-gated: ``GRINDER_CONSEC_LOSS_EVIDENCE`` (default False).

    Returns (json_path, sha_path) or None if disabled/failed.
    """
    if not parse_bool(ENV_EVIDENCE, default=False, strict=False):
        return None

    raw_dir = os.environ.get(ENV_ARTIFACT_DIR, "artifacts")
    out_dir = Path(raw_dir) / "risk"

    payload = {
        "artifact_version": ARTIFACT_VERSION,
        "ts_ms": ts_ms,
        "guard_state": guard.state.to_dict(),
        "config": {
            "enabled": config.enabled,
            "threshold": config.threshold,
            "action": config.action.value,
        },
        "trigger_row": outcome_row.to_dict(),
    }

    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    json_path = out_dir / f"consecutive_loss_trip_{ts_ms}.json"
    sha_path = out_dir / f"consecutive_loss_trip_{ts_ms}.sha256"

    try:
        _atomic_write_text(json_path, text)
        _atomic_write_text(sha_path, f"{digest}  {json_path.name}\n")
        return json_path, sha_path
    except OSError:
        logger.warning(
            "Failed to write consecutive loss evidence",
            extra={"out_dir": str(out_dir), "ts_ms": ts_ms},
            exc_info=True,
        )
        return None


# --- Operator PAUSE --------------------------------------------------------


def set_operator_pause(count: int, threshold: int) -> None:
    """Set GRINDER_OPERATOR_OVERRIDE=PAUSE to trigger FSM pause.

    Args:
        count: Current consecutive loss count (for logging).
        threshold: Configured threshold (for logging).
    """
    os.environ["GRINDER_OPERATOR_OVERRIDE"] = "PAUSE"
    logger.warning(
        "CONSECUTIVE_LOSS_TRIP: setting operator override to PAUSE",
        extra={"count": count, "threshold": threshold},
    )


# --- Service ---------------------------------------------------------------


class ConsecutiveLossService:
    """Wires ConsecutiveLossGuard to the live reconciliation pipeline.

    Processes raw Binance userTrade dicts, tracks roundtrips via
    ``RoundtripTracker``, and updates the guard on each completed roundtrip.

    On guard trip: writes evidence, sets GRINDER_OPERATOR_OVERRIDE=PAUSE.
    """

    def __init__(self, config: ConsecutiveLossConfig | None = None) -> None:
        if config is None:
            config = load_consecutive_loss_config()
        self.guard = ConsecutiveLossGuard(config)
        self.tracker = RoundtripTracker(source="live")
        self.trip_count: int = 0
        self._last_trade_id: int = 0

    @property
    def enabled(self) -> bool:
        """Whether the guard is enabled."""
        return self.guard.config.enabled

    def process_trades(self, raw_trades: list[dict[str, Any]]) -> None:
        """Process raw Binance userTrade dicts through the guard.

        * Sorts by trade ID for monotonic dedup (handles out-of-order).
        * Deduplicates via ``_last_trade_id``.
        * Converts to Fill + fee → RoundtripTracker → guard.
        * On trip: evidence + operator PAUSE.

        Safe when disabled: returns immediately.
        """
        if not self.enabled:
            return

        # Sort by trade ID for stable monotonic processing (P0-3)
        sorted_trades = sorted(raw_trades, key=_extract_trade_id_for_sort)

        for raw in sorted_trades:
            # Extract trade ID — skip if missing (P0-3)
            raw_id = raw.get("id")
            if raw_id is None:
                logger.warning(
                    "CONSEC_LOSS_SKIP_NO_ID",
                    extra={"raw_keys": list(raw.keys())},
                )
                continue

            try:
                trade_id = int(raw_id)
            except (ValueError, TypeError):
                logger.warning(
                    "CONSEC_LOSS_SKIP_BAD_ID",
                    extra={"raw_id": raw_id},
                )
                continue

            # Dedup: skip already-processed trades
            if trade_id <= self._last_trade_id:
                continue

            # Convert to Fill + fee
            try:
                fill = binance_trade_to_fill(raw)
                fee = binance_trade_fee(raw)
            except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
                logger.warning(
                    "CONSEC_LOSS_PARSE_ERROR",
                    extra={"trade_id": trade_id, "error": str(exc)},
                )
                # Update _last_trade_id even on parse error to avoid
                # retrying the same broken trade forever
                self._last_trade_id = trade_id
                continue

            # Record in RoundtripTracker
            outcome_row = self.tracker.record(fill, fee)

            # Update _last_trade_id after successful processing
            self._last_trade_id = trade_id

            # If a roundtrip closed, update the guard
            if outcome_row is not None:
                tripped = self.guard.update(
                    outcome_row.outcome,
                    row_id=outcome_row.row_id,
                    ts_ms=outcome_row.exit_ts,
                )

                if tripped:
                    self.trip_count += 1
                    logger.warning(
                        "CONSECUTIVE_LOSS_GUARD_TRIPPED",
                        extra={
                            "count": self.guard.count,
                            "threshold": self.guard.config.threshold,
                            "row_id": outcome_row.row_id,
                            "outcome": outcome_row.outcome,
                        },
                    )
                    write_trip_evidence(
                        self.guard,
                        outcome_row,
                        outcome_row.exit_ts,
                        self.guard.config,
                    )
                    set_operator_pause(
                        self.guard.count,
                        self.guard.config.threshold,
                    )

    def get_metrics_state(self) -> tuple[int, int]:
        """Return (consecutive_loss_count, trip_count) for metrics."""
        return self.guard.count, self.trip_count


def _extract_trade_id_for_sort(raw: dict[str, Any]) -> int:
    """Extract trade ID for sorting.  Returns 0 if missing/invalid."""
    try:
        return int(raw["id"])
    except (KeyError, ValueError, TypeError):
        return 0
