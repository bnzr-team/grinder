"""Wire ConsecutiveLossGuard into the live reconciliation pipeline (PR-C3b/C3c/C3d).

Side-effect layer: env config, Binance trade conversion, evidence writing,
operator PAUSE action, metrics push, state persistence, per-symbol routing.
The guard itself stays pure.

Integration point: ``scripts/run_live_reconcile.py`` only.
Other entrypoints do NOT activate this guard.

PR-C3c additions:
- Per-symbol independent streak tracking (dict[symbol, ConsecutiveLossGuard])
- Persistent state to JSON + sha256 sidecar (atomic write, monotonicity guard)

PR-C3d additions:
- RoundtripTracker persisted in state v2 (in-flight recovery)
- Backward compat: v1 files load without tracker (warning logged)

SSOT: this module.  ADR-070 in docs/DECISIONS.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
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
    ConsecutiveLossState,
)

logger = logging.getLogger(__name__)

ARTIFACT_VERSION = "consecutive_loss_evidence_v1"
STATE_FILE_VERSION = "consecutive_loss_state_v2"
STATE_FILE_VERSION_V1 = "consecutive_loss_state_v1"
ENV_ENABLE = "GRINDER_CONSEC_LOSS_ENABLED"
ENV_THRESHOLD = "GRINDER_CONSEC_LOSS_THRESHOLD"
ENV_EVIDENCE = "GRINDER_CONSEC_LOSS_EVIDENCE"
ENV_ARTIFACT_DIR = "GRINDER_ARTIFACT_DIR"
ENV_STATE_PATH = "GRINDER_CONSEC_LOSS_STATE_PATH"


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


# --- State persistence (PR-C3c) -------------------------------------------


@dataclass(frozen=True)
class PersistedServiceState:
    """Full persisted state envelope for the consecutive loss service.

    v2 adds ``tracker`` field for RoundtripTracker persistence.
    v1 files are accepted with ``tracker=None`` (backward compat).
    """

    version: str
    guards: dict[str, dict[str, Any]]  # {symbol: ConsecutiveLossState.to_dict()}
    last_trade_id: int
    trip_count: int
    updated_at_ms: int
    tracker: dict[str, Any] | None  # RoundtripTracker.to_state_dict() or None (v1)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict."""
        d: dict[str, Any] = {
            "version": self.version,
            "guards": self.guards,
            "last_trade_id": self.last_trade_id,
            "trip_count": self.trip_count,
            "updated_at_ms": self.updated_at_ms,
        }
        if self.tracker is not None:
            d["tracker"] = self.tracker
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersistedServiceState:
        """Strict parsing.  Raises ValueError on invalid data.

        Accepts both v1 (no tracker) and v2 (with tracker).
        """
        version = data.get("version", "")
        if version not in (STATE_FILE_VERSION, STATE_FILE_VERSION_V1):
            raise ValueError(f"unsupported state version: {version!r}")

        raw_guards = data.get("guards", {})
        if not isinstance(raw_guards, dict):
            raise ValueError(f"guards must be dict, got {type(raw_guards).__name__}")
        for sym, gdata in raw_guards.items():
            if not isinstance(sym, str):
                raise ValueError(f"guard key must be str, got {type(sym).__name__}")
            if not isinstance(gdata, dict):
                raise ValueError(
                    f"guard value for {sym!r} must be dict, got {type(gdata).__name__}"
                )

        last_trade_id = data.get("last_trade_id", 0)
        if not isinstance(last_trade_id, int) or last_trade_id < 0:
            raise ValueError(f"last_trade_id must be int >= 0, got {last_trade_id!r}")

        trip_count = data.get("trip_count", 0)
        if not isinstance(trip_count, int) or trip_count < 0:
            raise ValueError(f"trip_count must be int >= 0, got {trip_count!r}")

        updated_at_ms = data.get("updated_at_ms", 0)
        if not isinstance(updated_at_ms, int) or updated_at_ms < 0:
            raise ValueError(f"updated_at_ms must be int >= 0, got {updated_at_ms!r}")

        # v2: tracker field; v1: None
        raw_tracker = data.get("tracker")
        if raw_tracker is not None and not isinstance(raw_tracker, dict):
            raise ValueError(f"tracker must be dict | None, got {type(raw_tracker).__name__}")

        return cls(
            version=version,
            guards=raw_guards,
            last_trade_id=last_trade_id,
            trip_count=trip_count,
            updated_at_ms=updated_at_ms,
            tracker=raw_tracker,
        )


def load_consec_loss_state(path: str) -> PersistedServiceState | None:
    """Load persisted state from disk.  Returns None if missing/corrupt."""
    p = Path(path)
    if not p.exists():
        logger.info("CONSEC_LOSS_STATE_NOT_FOUND", extra={"path": path})
        return None
    try:
        raw_text = p.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        state = PersistedServiceState.from_dict(data)

        # SHA256 sidecar validation
        sha_path = p.with_suffix(".sha256")
        if sha_path.exists():
            expected_digest = sha_path.read_text(encoding="utf-8").split()[0]
            actual_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            if expected_digest != actual_digest:
                logger.warning(
                    "CONSEC_LOSS_STATE_SHA_MISMATCH",
                    extra={"path": path},
                )
                return None

        logger.info(
            "CONSEC_LOSS_STATE_LOADED",
            extra={
                "path": path,
                "symbols": list(state.guards.keys()),
                "last_trade_id": state.last_trade_id,
                "trip_count": state.trip_count,
            },
        )
        return state
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning(
            "CONSEC_LOSS_STATE_LOAD_ERROR",
            extra={"path": path, "error": str(exc)},
        )
        return None


def save_consec_loss_state(
    path: str,
    state: PersistedServiceState,
) -> None:
    """Persist state to disk.  Atomic write + sha256 sidecar.

    Monotonicity guard (P0-2):
    - Missing file → write
    - Corrupt file → log CONSEC_LOSS_STATE_EXISTING_CORRUPT_OVERWRITE, write
    - new.last_trade_id < old.last_trade_id → reject (non-monotonic)
    - new.last_trade_id == old.last_trade_id → skip (idempotent)
    - new.last_trade_id > old.last_trade_id → write
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        try:
            existing_data = json.loads(p.read_text(encoding="utf-8"))
            existing_trade_id = int(existing_data.get("last_trade_id", 0))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            logger.warning(
                "CONSEC_LOSS_STATE_EXISTING_CORRUPT_OVERWRITE",
                extra={"path": path},
            )
            existing_trade_id = -1  # force write

        if existing_trade_id >= 0:
            if state.last_trade_id < existing_trade_id:
                logger.warning(
                    "CONSEC_LOSS_STATE_REJECTED_NON_MONOTONIC",
                    extra={
                        "path": path,
                        "existing_trade_id": existing_trade_id,
                        "new_trade_id": state.last_trade_id,
                    },
                )
                return
            if state.last_trade_id == existing_trade_id:
                # In normal flow this shouldn't happen: _state_dirty is only
                # set when _last_trade_id advances (dedup skips equal IDs).
                # This branch is a safety net against double-save or external
                # callers — avoids rewriting the same state with a new timestamp.
                return

    text = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    _atomic_write_text(p, text)
    sha_path = p.with_suffix(".sha256")
    _atomic_write_text(sha_path, f"{digest}  {p.name}\n")


# --- Service ---------------------------------------------------------------


class ConsecutiveLossService:
    """Wires ConsecutiveLossGuard to the live reconciliation pipeline.

    Processes raw Binance userTrade dicts, tracks roundtrips via
    ``RoundtripTracker``, and updates per-symbol guards on each
    completed roundtrip.

    On guard trip: writes evidence, sets GRINDER_OPERATOR_OVERRIDE=PAUSE.

    PR-C3c: per-symbol independent streaks + persistent state.
    """

    def __init__(self, config: ConsecutiveLossConfig | None = None) -> None:
        self._config = config if config is not None else load_consecutive_loss_config()
        self._guards: dict[str, ConsecutiveLossGuard] = {}
        self.tracker = RoundtripTracker(source="live")
        self.trip_count: int = 0
        self._last_trade_id: int = 0
        self._state_path: str | None = os.environ.get(ENV_STATE_PATH, "").strip() or None
        self._state_dirty: bool = False

        # Load persisted state if path is configured
        if self._state_path:
            persisted = load_consec_loss_state(self._state_path)
            if persisted is not None:
                self._last_trade_id = persisted.last_trade_id
                self.trip_count = persisted.trip_count
                for symbol, guard_data in persisted.guards.items():
                    guard_state = ConsecutiveLossState.from_dict(guard_data)
                    self._guards[symbol] = ConsecutiveLossGuard.from_state(
                        self._config,
                        guard_state,
                    )
                # Staleness warning (informational)
                age_h = (int(time.time() * 1000) - persisted.updated_at_ms) / 3_600_000
                if age_h > 24:
                    logger.warning(
                        "CONSEC_LOSS_STATE_STALE",
                        extra={
                            "path": self._state_path,
                            "age_hours": round(age_h, 1),
                        },
                    )
                # Restore tracker (v2) or log limitation (v1)
                if persisted.tracker is not None:
                    try:
                        self.tracker = RoundtripTracker.from_state_dict(
                            persisted.tracker,
                        )
                        logger.info(
                            "CONSEC_LOSS_TRACKER_RESTORED",
                            extra={
                                "open_positions": len(self.tracker.open_positions),
                                "last_trade_id": self._last_trade_id,
                            },
                        )
                    except (ValueError, KeyError, TypeError) as exc:
                        logger.warning(
                            "CONSEC_LOSS_TRACKER_RESTORE_FAILED",
                            extra={
                                "error": str(exc),
                                "last_trade_id": self._last_trade_id,
                            },
                        )
                        # Keep fresh tracker — in-flight roundtrips lost
                else:
                    logger.warning(
                        "CONSEC_LOSS_TRACKER_NOT_IN_STATE",
                        extra={
                            "version": persisted.version,
                            "symbol_count": len(self._guards),
                            "last_trade_id": self._last_trade_id,
                        },
                    )

    @property
    def guard(self) -> ConsecutiveLossGuard:
        """Backward-compat: returns first symbol guard or a default."""
        if self._guards:
            return next(iter(self._guards.values()))
        return ConsecutiveLossGuard(self._config)

    @property
    def enabled(self) -> bool:
        """Whether the guard is enabled."""
        return self._config.enabled

    def _get_guard(self, symbol: str) -> ConsecutiveLossGuard:
        """Get or create a per-symbol guard."""
        if symbol not in self._guards:
            self._guards[symbol] = ConsecutiveLossGuard(self._config)
        return self._guards[symbol]

    def process_trades(self, raw_trades: list[dict[str, Any]]) -> None:
        """Process raw Binance userTrade dicts through per-symbol guards.

        * Sorts by trade ID for monotonic dedup (handles out-of-order).
        * Deduplicates via ``_last_trade_id``.
        * Converts to Fill + fee → RoundtripTracker → per-symbol guard.
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
                self._state_dirty = True
                continue

            # Record in RoundtripTracker
            outcome_row = self.tracker.record(fill, fee)

            # Update _last_trade_id after successful processing
            self._last_trade_id = trade_id
            self._state_dirty = True

            # If a roundtrip closed, update the per-symbol guard
            if outcome_row is not None:
                symbol = outcome_row.symbol
                guard = self._get_guard(symbol)
                tripped = guard.update(
                    outcome_row.outcome,
                    row_id=outcome_row.row_id,
                    ts_ms=outcome_row.exit_ts,
                )

                if tripped:
                    self.trip_count += 1
                    logger.warning(
                        "CONSECUTIVE_LOSS_GUARD_TRIPPED",
                        extra={
                            "symbol": symbol,
                            "count": guard.count,
                            "threshold": guard.config.threshold,
                            "row_id": outcome_row.row_id,
                            "outcome": outcome_row.outcome,
                        },
                    )
                    write_trip_evidence(
                        guard,
                        outcome_row,
                        outcome_row.exit_ts,
                        guard.config,
                    )
                    set_operator_pause(
                        guard.count,
                        guard.config.threshold,
                    )

    def get_metrics_state(self) -> tuple[int, int]:
        """Return (max_consecutive_loss_count, total_trip_count) for metrics.

        Count is max across all symbol guards (label-free).
        """
        if not self._guards:
            return 0, self.trip_count
        max_count = max(g.count for g in self._guards.values())
        return max_count, self.trip_count

    def save_state_if_dirty(self) -> None:
        """Persist state to disk if any state changed since last save."""
        if not self._state_path or not self._state_dirty:
            return
        now_ms = int(time.time() * 1000)
        guards_dict = {symbol: guard.state.to_dict() for symbol, guard in self._guards.items()}
        persisted = PersistedServiceState(
            version=STATE_FILE_VERSION,
            guards=guards_dict,
            last_trade_id=self._last_trade_id,
            trip_count=self.trip_count,
            updated_at_ms=now_ms,
            tracker=self.tracker.to_state_dict(),
        )
        save_consec_loss_state(self._state_path, persisted)
        self._state_dirty = False


def _extract_trade_id_for_sort(raw: dict[str, Any]) -> int:
    """Extract trade ID for sorting.  Returns 0 if missing/invalid."""
    try:
        return int(raw["id"])
    except (KeyError, ValueError, TypeError):
        return 0
