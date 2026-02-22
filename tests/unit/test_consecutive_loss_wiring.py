"""Tests for ConsecutiveLossService wiring (PR-C3b/C3c/C3d).

Covers: config loading, fill conversion, service end-to-end, metrics,
operator override, evidence, error handling, disabled guard, sort stability,
persistence (PR-C3c), per-symbol guards (PR-C3c), state restore (PR-C3c),
tracker persistence + restart recovery (PR-C3d).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from grinder.observability.metrics_builder import (
    get_consecutive_loss_metrics,
    reset_consecutive_loss_metrics,
    set_consecutive_loss_metrics,
)
from grinder.paper.fills import Fill
from grinder.risk.consecutive_loss_guard import (
    ConsecutiveLossAction,
    ConsecutiveLossConfig,
    ConsecutiveLossState,
)
from grinder.risk.consecutive_loss_wiring import (
    STATE_FILE_VERSION,
    STATE_FILE_VERSION_V1,
    ConsecutiveLossService,
    PersistedServiceState,
    binance_trade_fee,
    binance_trade_to_fill,
    load_consec_loss_state,
    load_consecutive_loss_config,
    save_consec_loss_state,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trade(
    *,
    trade_id: int,
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    price: str = "50000.0",
    qty: str = "0.1",
    commission: str = "0.01",
    time: int = 1000000,
    order_id: int = 1,
) -> dict[str, object]:
    """Build a Binance userTrade dict."""
    return {
        "id": trade_id,
        "symbol": symbol,
        "side": side,
        "price": price,
        "qty": qty,
        "commission": commission,
        "time": time,
        "orderId": order_id,
    }


def _make_roundtrip_trades(
    symbol: str,
    entry_price: str,
    exit_price: str,
    qty: str,
    base_time: int,
    base_id: int,
) -> list[dict[str, object]]:
    """Build BUY+SELL pair as Binance trade dicts (one roundtrip)."""
    return [
        _make_trade(
            trade_id=base_id,
            symbol=symbol,
            side="BUY",
            price=entry_price,
            qty=qty,
            time=base_time,
            order_id=base_id,
        ),
        _make_trade(
            trade_id=base_id + 1,
            symbol=symbol,
            side="SELL",
            price=exit_price,
            qty=qty,
            time=base_time + 1000,
            order_id=base_id + 1,
        ),
    ]


# ---------------------------------------------------------------------------
# W-001: Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    """W-001: Config loading from environment."""

    def test_default_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default config: disabled, threshold=5, action=PAUSE."""
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_ENABLED", raising=False)
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_THRESHOLD", raising=False)
        config = load_consecutive_loss_config()
        assert config.enabled is False
        assert config.threshold == 5
        assert config.action == ConsecutiveLossAction.PAUSE

    def test_enabled_custom_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enabled with custom threshold."""
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_ENABLED", "1")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_THRESHOLD", "3")
        config = load_consecutive_loss_config()
        assert config.enabled is True
        assert config.threshold == 3
        assert config.action == ConsecutiveLossAction.PAUSE


# ---------------------------------------------------------------------------
# W-002: Fill conversion
# ---------------------------------------------------------------------------


class TestFillConversion:
    """W-002: Binance trade → Fill conversion."""

    def test_happy_path(self) -> None:
        """Convert valid Binance trade to Fill."""
        raw = _make_trade(
            trade_id=100,
            symbol="ETHUSDT",
            side="SELL",
            price="3000.50",
            qty="1.5",
            commission="0.05",
            time=1700000000,
            order_id=42,
        )
        fill = binance_trade_to_fill(raw)
        assert fill == Fill(
            ts=1700000000,
            symbol="ETHUSDT",
            side="SELL",
            price=Decimal("3000.50"),
            quantity=Decimal("1.5"),
            order_id="42",
        )
        fee = binance_trade_fee(raw)
        assert fee == Decimal("0.05")

    def test_missing_field(self) -> None:
        """Missing required field raises KeyError."""
        raw = {"id": 1, "symbol": "BTCUSDT"}  # missing many fields
        with pytest.raises(KeyError):
            binance_trade_to_fill(raw)


# ---------------------------------------------------------------------------
# W-003: Service end-to-end
# ---------------------------------------------------------------------------


class TestServiceEndToEnd:
    """W-003: Full service flow."""

    def test_single_buy_no_trip(self) -> None:
        """Single BUY doesn't close a roundtrip → no guard update."""
        config = ConsecutiveLossConfig(enabled=True, threshold=3)
        svc = ConsecutiveLossService(config)
        trades = [_make_trade(trade_id=1, side="BUY")]
        svc.process_trades(trades)
        assert svc.guard.count == 0
        assert svc.trip_count == 0

    def test_losing_roundtrip_increments_count(self) -> None:
        """BUY at 100, SELL at 90 → loss → count=1."""
        config = ConsecutiveLossConfig(enabled=True, threshold=3)
        svc = ConsecutiveLossService(config)
        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)
        assert svc.guard.count == 1
        assert svc.trip_count == 0

    def test_trip_at_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """3 consecutive losses with threshold=3 → trip."""
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_EVIDENCE", raising=False)
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        config = ConsecutiveLossConfig(enabled=True, threshold=3)
        svc = ConsecutiveLossService(config)

        for i in range(3):
            base_id = i * 2 + 1
            trades = _make_roundtrip_trades(
                "BTCUSDT", "100.0", "90.0", "1.0", 1000 + i * 2000, base_id
            )
            svc.process_trades(trades)

        assert svc.guard.count == 3
        assert svc.guard.is_tripped is True
        assert svc.trip_count == 1
        assert os.environ.get("GRINDER_OPERATOR_OVERRIDE") == "PAUSE"

    def test_win_resets_count(self) -> None:
        """Loss then win → count resets to 0."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        # Loss
        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)
        assert svc.guard.count == 1

        # Win
        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "110.0", "1.0", 5000, 3)
        svc.process_trades(trades)
        assert svc.guard.count == 0

    def test_dedup_same_trades_twice(self) -> None:
        """Feeding same trades twice doesn't double-count."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)
        assert svc.guard.count == 1

        # Feed same trades again
        svc.process_trades(trades)
        assert svc.guard.count == 1  # No change


# ---------------------------------------------------------------------------
# W-004: Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    """W-004: Metrics setter/getter."""

    def setup_method(self) -> None:
        reset_consecutive_loss_metrics()

    def test_initial_zero(self) -> None:
        """Initial state is (0, 0)."""
        assert get_consecutive_loss_metrics() == (0, 0)

    def test_after_update(self) -> None:
        """set_consecutive_loss_metrics updates state."""
        set_consecutive_loss_metrics(3, 1)
        assert get_consecutive_loss_metrics() == (3, 1)


# ---------------------------------------------------------------------------
# W-005: Operator override
# ---------------------------------------------------------------------------


class TestOperatorOverride:
    """W-005: PAUSE action sets env var."""

    def test_trip_sets_pause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Guard trip sets GRINDER_OPERATOR_OVERRIDE=PAUSE."""
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_EVIDENCE", raising=False)
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        config = ConsecutiveLossConfig(enabled=True, threshold=1)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)

        assert os.environ.get("GRINDER_OPERATOR_OVERRIDE") == "PAUSE"


# ---------------------------------------------------------------------------
# W-006: Evidence
# ---------------------------------------------------------------------------


class TestEvidence:
    """W-006: Evidence artifacts on trip."""

    def test_evidence_written_on_trip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With evidence enabled, trip writes JSON + sha256."""
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_EVIDENCE", "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        config = ConsecutiveLossConfig(enabled=True, threshold=1)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)

        risk_dir = tmp_path / "risk"
        json_files = list(risk_dir.glob("consecutive_loss_trip_*.json"))
        sha_files = list(risk_dir.glob("consecutive_loss_trip_*.sha256"))

        assert len(json_files) == 1
        assert len(sha_files) == 1

        # Verify JSON structure
        payload = json.loads(json_files[0].read_text())
        assert payload["artifact_version"] == "consecutive_loss_evidence_v1"
        assert payload["guard_state"]["tripped"] is True
        assert payload["trigger_row"]["outcome"] == "loss"

    def test_evidence_disabled_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With evidence disabled, no files written."""
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_EVIDENCE", raising=False)
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        config = ConsecutiveLossConfig(enabled=True, threshold=1)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)

        risk_dir = tmp_path / "risk"
        assert not risk_dir.exists() or not list(risk_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# W-007: Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """W-007: Graceful error handling."""

    def test_bad_trade_skipped(self) -> None:
        """Trade with invalid price is skipped gracefully."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        bad_trade = _make_trade(trade_id=1)
        bad_trade["price"] = "not_a_number"

        svc.process_trades([bad_trade])
        assert svc.guard.count == 0
        assert svc._last_trade_id == 1  # Still advanced past bad trade

    def test_mixed_valid_invalid(self) -> None:
        """Valid trades processed even when mixed with invalid ones."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        bad_trade = _make_trade(trade_id=1)
        bad_trade["price"] = "not_a_number"

        good_trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 2)

        svc.process_trades([bad_trade, *good_trades])
        assert svc.guard.count == 1  # Roundtrip processed
        assert svc._last_trade_id == 3

    def test_missing_id_skipped(self) -> None:
        """Trade without 'id' field is skipped with warning."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        trade_no_id = {"symbol": "BTCUSDT", "side": "BUY", "price": "100.0"}
        svc.process_trades([trade_no_id])
        assert svc.guard.count == 0
        assert svc._last_trade_id == 0


# ---------------------------------------------------------------------------
# W-008: Disabled guard
# ---------------------------------------------------------------------------


class TestDisabledGuard:
    """W-008: Guard disabled = no-op."""

    def test_process_trades_noop(self) -> None:
        """process_trades returns immediately when disabled."""
        config = ConsecutiveLossConfig(enabled=False, threshold=3)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)

        assert svc.guard.count == 0
        assert svc._last_trade_id == 0


# ---------------------------------------------------------------------------
# W-009: Sort stability
# ---------------------------------------------------------------------------


class TestSortStability:
    """W-009: Out-of-order trades processed correctly."""

    def test_out_of_order_trades(self) -> None:
        """Trades with out-of-order IDs are sorted before processing."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        # Create roundtrip but feed in reverse ID order
        buy = _make_trade(
            trade_id=2,
            side="BUY",
            price="100.0",
            qty="1.0",
            time=1000,
            order_id=2,
        )
        sell = _make_trade(
            trade_id=1,  # Lower ID but should be processed first after sort
            side="SELL",
            price="90.0",
            qty="1.0",
            time=999,
            order_id=1,
        )

        # Feed in wrong order: sell first (id=1), buy second (id=2)
        # After sort by ID: sell(id=1), buy(id=2)
        # sell opens short, buy closes short → roundtrip
        svc.process_trades([buy, sell])

        # Should have processed both (sorted by ID)
        assert svc._last_trade_id == 2


# ---------------------------------------------------------------------------
# W-010: Persistence (PR-C3c)
# ---------------------------------------------------------------------------


class TestPersistence:
    """W-010: State persistence save/load roundtrip."""

    def _make_state(
        self,
        *,
        last_trade_id: int = 100,
        trip_count: int = 1,
        guards: dict[str, dict[str, object]] | None = None,
        tracker: dict[str, object] | None = None,
    ) -> PersistedServiceState:
        """Build a PersistedServiceState for tests."""
        if guards is None:
            guards = {
                "BTCUSDT": {
                    "count": 2,
                    "tripped": False,
                    "last_row_id": "row-1",
                    "last_ts_ms": 1700000000,
                },
            }
        return PersistedServiceState(
            version=STATE_FILE_VERSION,
            guards=guards,
            last_trade_id=last_trade_id,
            trip_count=trip_count,
            updated_at_ms=int(time.time() * 1000),
            tracker=tracker,
        )

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        """Save then load produces identical state."""
        state = self._make_state()
        path = str(tmp_path / "state.json")
        save_consec_loss_state(path, state)

        loaded = load_consec_loss_state(path)
        assert loaded is not None
        assert loaded.version == state.version
        assert loaded.guards == state.guards
        assert loaded.last_trade_id == state.last_trade_id
        assert loaded.trip_count == state.trip_count
        assert loaded.updated_at_ms == state.updated_at_ms

    def test_load_missing_file_returns_none(self, tmp_path: Path) -> None:
        """Graceful on missing file."""
        result = load_consec_loss_state(str(tmp_path / "no_such_file.json"))
        assert result is None

    def test_load_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        """Graceful on bad JSON."""
        p = tmp_path / "corrupt.json"
        p.write_text("{not valid json", encoding="utf-8")
        result = load_consec_loss_state(str(p))
        assert result is None

    def test_load_bad_version_returns_none(self, tmp_path: Path) -> None:
        """Rejects unknown version."""
        p = tmp_path / "bad_version.json"
        data = {"version": "unknown_v99", "guards": {}, "last_trade_id": 0}
        p.write_text(json.dumps(data), encoding="utf-8")
        result = load_consec_loss_state(str(p))
        assert result is None

    def test_load_invalid_types_returns_none(self, tmp_path: Path) -> None:
        """Rejects invalid types (P0-1: string tripped, negative count)."""
        p = tmp_path / "bad_types.json"
        data = {
            "version": STATE_FILE_VERSION,
            "guards": {
                "BTCUSDT": {"count": -1, "tripped": "false"},
            },
            "last_trade_id": 0,
            "trip_count": 0,
            "updated_at_ms": 0,
        }
        p.write_text(json.dumps(data), encoding="utf-8")
        # The load itself may succeed (guards are dicts),
        # but service restore will fail when from_dict validates guard fields.
        # At the PersistedServiceState level, guards are stored as raw dicts.
        loaded = load_consec_loss_state(str(p))
        # State loads (guards are raw dicts at this level)
        assert loaded is not None
        # But the guard values inside should be validated when restoring:
        with pytest.raises(ValueError, match="count must be int >= 0"):
            ConsecutiveLossState.from_dict(loaded.guards["BTCUSDT"])

    def test_sha256_mismatch_returns_none(self, tmp_path: Path) -> None:
        """Rejects tampered file (sha256 sidecar mismatch)."""
        state = self._make_state()
        path = str(tmp_path / "state.json")
        save_consec_loss_state(path, state)

        # Tamper with the JSON file
        p = tmp_path / "state.json"
        content = p.read_text(encoding="utf-8")
        p.write_text(content.replace('"trip_count": 1', '"trip_count": 999'))

        result = load_consec_loss_state(path)
        assert result is None

    def test_monotonicity_rejects_backward(self, tmp_path: Path) -> None:
        """P0-2: won't overwrite newer cursor with older one."""
        path = str(tmp_path / "state.json")

        # Save state with trade_id=100
        state_new = self._make_state(last_trade_id=100)
        save_consec_loss_state(path, state_new)

        # Try to save state with trade_id=50 → rejected
        state_old = self._make_state(last_trade_id=50)
        save_consec_loss_state(path, state_old)

        # File still has trade_id=100
        loaded = load_consec_loss_state(path)
        assert loaded is not None
        assert loaded.last_trade_id == 100

    def test_monotonicity_allows_forward(self, tmp_path: Path) -> None:
        """P0-2: writes when new > old."""
        path = str(tmp_path / "state.json")

        state_v1 = self._make_state(last_trade_id=100)
        save_consec_loss_state(path, state_v1)

        state_v2 = self._make_state(last_trade_id=200, trip_count=2)
        save_consec_loss_state(path, state_v2)

        loaded = load_consec_loss_state(path)
        assert loaded is not None
        assert loaded.last_trade_id == 200
        assert loaded.trip_count == 2

    def test_save_to_missing_dir_creates_parents(self, tmp_path: Path) -> None:
        """mkdir -p behavior for nested directories."""
        path = str(tmp_path / "deep" / "nested" / "state.json")
        state = self._make_state()
        save_consec_loss_state(path, state)

        loaded = load_consec_loss_state(path)
        assert loaded is not None
        assert loaded.last_trade_id == 100


# ---------------------------------------------------------------------------
# W-011: Per-symbol guards (PR-C3c)
# ---------------------------------------------------------------------------


class TestPerSymbol:
    """W-011: Per-symbol independent streak tracking."""

    def test_independent_symbol_streaks(self) -> None:
        """BTC losses don't affect ETH count."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        # 2 BTC losses
        for i in range(2):
            base_id = i * 2 + 1
            trades = _make_roundtrip_trades(
                "BTCUSDT", "100.0", "90.0", "1.0", 1000 + i * 2000, base_id
            )
            svc.process_trades(trades)

        # 1 ETH loss
        trades = _make_roundtrip_trades("ETHUSDT", "3000.0", "2900.0", "1.0", 10000, 10)
        svc.process_trades(trades)

        assert svc._guards["BTCUSDT"].count == 2
        assert svc._guards["ETHUSDT"].count == 1

    def test_one_symbol_trips_sets_pause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Trip on any symbol triggers PAUSE."""
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_EVIDENCE", raising=False)
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        config = ConsecutiveLossConfig(enabled=True, threshold=2)
        svc = ConsecutiveLossService(config)

        # 2 BTC losses → trip
        for i in range(2):
            base_id = i * 2 + 1
            trades = _make_roundtrip_trades(
                "BTCUSDT", "100.0", "90.0", "1.0", 1000 + i * 2000, base_id
            )
            svc.process_trades(trades)

        assert svc._guards["BTCUSDT"].is_tripped is True
        assert os.environ.get("GRINDER_OPERATOR_OVERRIDE") == "PAUSE"
        assert svc.trip_count == 1

    def test_metrics_returns_max_count(self) -> None:
        """get_metrics_state returns max across symbols."""
        config = ConsecutiveLossConfig(enabled=True, threshold=10)
        svc = ConsecutiveLossService(config)

        # 3 BTC losses
        for i in range(3):
            base_id = i * 2 + 1
            trades = _make_roundtrip_trades(
                "BTCUSDT", "100.0", "90.0", "1.0", 1000 + i * 2000, base_id
            )
            svc.process_trades(trades)

        # 1 ETH loss
        trades = _make_roundtrip_trades("ETHUSDT", "3000.0", "2900.0", "1.0", 20000, 20)
        svc.process_trades(trades)

        count, trips = svc.get_metrics_state()
        assert count == 3  # max(BTC=3, ETH=1)
        assert trips == 0  # no trips yet

    def test_new_symbol_creates_fresh_guard(self) -> None:
        """First trade for unknown symbol creates new guard with count=0."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        assert "SOLUSDT" not in svc._guards

        trades = _make_roundtrip_trades("SOLUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)

        assert "SOLUSDT" in svc._guards
        assert svc._guards["SOLUSDT"].count == 1


# ---------------------------------------------------------------------------
# W-012: State restore (PR-C3c)
# ---------------------------------------------------------------------------


class TestServiceStateRestore:
    """W-012: Service state restoration across restarts."""

    def test_restore_continues_count(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Load count=2, one more loss -> count=3."""
        state_path = str(tmp_path / "state.json")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_STATE_PATH", state_path)

        # Pre-seed state with count=2 for BTCUSDT
        pre_state = PersistedServiceState(
            version=STATE_FILE_VERSION,
            guards={
                "BTCUSDT": {
                    "count": 2,
                    "tripped": False,
                    "last_row_id": "row-prev",
                    "last_ts_ms": 1700000000,
                },
            },
            last_trade_id=10,
            trip_count=0,
            updated_at_ms=int(time.time() * 1000),
            tracker=None,
        )
        save_consec_loss_state(state_path, pre_state)

        # Create service (will load state)
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        assert svc._guards["BTCUSDT"].count == 2
        assert svc._last_trade_id == 10

        # One more loss → count=3
        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 2000, 11)
        svc.process_trades(trades)
        assert svc._guards["BTCUSDT"].count == 3

    def test_restore_last_trade_id_dedup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Restored cursor prevents re-processing old trades."""
        state_path = str(tmp_path / "state.json")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_STATE_PATH", state_path)

        pre_state = PersistedServiceState(
            version=STATE_FILE_VERSION,
            guards={},
            last_trade_id=50,
            trip_count=0,
            updated_at_ms=int(time.time() * 1000),
            tracker=None,
        )
        save_consec_loss_state(state_path, pre_state)

        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        # Feed trades with IDs <= 50 → all skipped
        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 49)
        svc.process_trades(trades)
        assert len(svc._guards) == 0  # Nothing processed
        assert svc._last_trade_id == 50

    def test_restore_trip_count(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """trip_count is cumulative across restarts."""
        state_path = str(tmp_path / "state.json")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_STATE_PATH", state_path)
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_EVIDENCE", raising=False)
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        pre_state = PersistedServiceState(
            version=STATE_FILE_VERSION,
            guards={
                "BTCUSDT": {
                    "count": 2,
                    "tripped": False,
                    "last_row_id": "row-prev",
                    "last_ts_ms": 1700000000,
                },
            },
            last_trade_id=10,
            trip_count=3,
            updated_at_ms=int(time.time() * 1000),
            tracker=None,
        )
        save_consec_loss_state(state_path, pre_state)

        config = ConsecutiveLossConfig(enabled=True, threshold=3)
        svc = ConsecutiveLossService(config)
        assert svc.trip_count == 3

        # One more loss -> trip (count 2->3, threshold=3)
        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 2000, 11)
        svc.process_trades(trades)
        assert svc.trip_count == 4  # 3 + 1


# ---------------------------------------------------------------------------
# W-013: Tracker persistence (PR-C3d)
# ---------------------------------------------------------------------------


class TestTrackerPersistence:
    """W-013: RoundtripTracker state persisted in v2."""

    def test_save_includes_tracker_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """save_state_if_dirty writes tracker open positions to state file."""
        state_path = str(tmp_path / "state.json")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_STATE_PATH", state_path)

        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        # Open a BTC long position (no close yet)
        entry = _make_trade(trade_id=1, side="BUY", price="50000", qty="0.1", time=1000)
        svc.process_trades([entry])
        svc.save_state_if_dirty()

        # Verify state file has tracker data
        loaded = load_consec_loss_state(state_path)
        assert loaded is not None
        assert loaded.version == STATE_FILE_VERSION
        assert loaded.tracker is not None
        assert "BTCUSDT|long" in loaded.tracker["positions"]

    def test_v1_file_loads_without_tracker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """v1 state file (no tracker field) loads with tracker=None + warning."""
        state_path = str(tmp_path / "state.json")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_STATE_PATH", state_path)

        # Write a v1 state file
        v1_data = {
            "version": STATE_FILE_VERSION_V1,
            "guards": {
                "BTCUSDT": {
                    "count": 2,
                    "tripped": False,
                    "last_row_id": "row-1",
                    "last_ts_ms": 1700000000,
                },
            },
            "last_trade_id": 10,
            "trip_count": 1,
            "updated_at_ms": int(time.time() * 1000),
        }
        text = json.dumps(v1_data, indent=2, sort_keys=True) + "\n"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        p = tmp_path / "state.json"
        p.write_text(text, encoding="utf-8")
        p.with_suffix(".sha256").write_text(f"{digest}  state.json\n", encoding="utf-8")

        # Load via service -- should restore guards but not tracker
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        assert svc._guards["BTCUSDT"].count == 2
        assert svc._last_trade_id == 10
        assert svc.trip_count == 1
        # Tracker is fresh (not restored from v1)
        assert len(svc.tracker.open_positions) == 0

    def test_restart_entry_then_exit_closes_roundtrip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """P0-4: entry before restart + exit after restart = closed roundtrip.

        1. Service A: BUY (entry) -> save
        2. Service B: loads state -> SELL (exit) -> roundtrip emitted -> guard updated
        """
        state_path = str(tmp_path / "state.json")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_STATE_PATH", state_path)

        # --- Service A: entry fill ---
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc_a = ConsecutiveLossService(config)

        entry = _make_trade(trade_id=1, side="BUY", price="50000", qty="0.1", time=1000)
        svc_a.process_trades([entry])

        # Verify position is open
        assert ("BTCUSDT", "long") in svc_a.tracker.open_positions
        assert len(svc_a._guards) == 0  # No roundtrip closed yet

        svc_a.save_state_if_dirty()

        # --- Service B: loads state, receives exit fill ---
        svc_b = ConsecutiveLossService(config)

        # Verify tracker was restored
        assert ("BTCUSDT", "long") in svc_b.tracker.open_positions
        assert svc_b._last_trade_id == 1

        # Exit fill -- closes the roundtrip (loss: 50000 -> 49000)
        exit_fill = _make_trade(trade_id=2, side="SELL", price="49000", qty="0.1", time=2000)
        svc_b.process_trades([exit_fill])

        # Roundtrip closed -- guard updated
        assert "BTCUSDT" in svc_b._guards
        assert svc_b._guards["BTCUSDT"].count == 1  # One loss
        assert len(svc_b.tracker.open_positions) == 0  # Position closed

    def test_restart_preserves_partial_exits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Partial exit before restart is preserved -- remaining exit after restart closes."""
        state_path = str(tmp_path / "state.json")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_STATE_PATH", state_path)

        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc_a = ConsecutiveLossService(config)

        # Entry: buy 0.2 BTC
        entry = _make_trade(trade_id=1, side="BUY", price="50000", qty="0.2", time=1000)
        svc_a.process_trades([entry])

        # Partial exit: sell 0.1 BTC (position still open)
        partial = _make_trade(trade_id=2, side="SELL", price="49500", qty="0.1", time=1500)
        svc_a.process_trades([partial])
        assert ("BTCUSDT", "long") in svc_a.tracker.open_positions

        svc_a.save_state_if_dirty()

        # Service B: loads state, sends remaining exit
        svc_b = ConsecutiveLossService(config)
        assert ("BTCUSDT", "long") in svc_b.tracker.open_positions

        remaining = _make_trade(trade_id=3, side="SELL", price="49000", qty="0.1", time=2000)
        svc_b.process_trades([remaining])

        # Roundtrip closed
        assert "BTCUSDT" in svc_b._guards
        assert svc_b._guards["BTCUSDT"].count == 1  # Loss
        assert len(svc_b.tracker.open_positions) == 0

    def test_restart_win_after_entry(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Entry before restart + profitable exit after restart = win (count stays 0)."""
        state_path = str(tmp_path / "state.json")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_STATE_PATH", state_path)

        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc_a = ConsecutiveLossService(config)

        entry = _make_trade(trade_id=1, side="BUY", price="50000", qty="0.1", time=1000)
        svc_a.process_trades([entry])
        svc_a.save_state_if_dirty()

        svc_b = ConsecutiveLossService(config)
        exit_fill = _make_trade(trade_id=2, side="SELL", price="51000", qty="0.1", time=2000)
        svc_b.process_trades([exit_fill])

        assert "BTCUSDT" in svc_b._guards
        assert svc_b._guards["BTCUSDT"].count == 0  # Win resets count

    def test_save_v2_then_load_has_tracker(self, tmp_path: Path) -> None:
        """v2 state file roundtrip preserves tracker field."""
        tracker_data = {
            "source": "live",
            "positions": {
                "BTCUSDT|long": {
                    "direction": "long",
                    "qty": "0.1",
                    "cost": "5000.0",
                    "fee": "0.01",
                    "fill_count": 1,
                    "first_ts": 1000,
                    "exit_qty": "0",
                    "exit_cost": "0",
                    "exit_fee": "0",
                    "exit_fill_count": 0,
                    "last_exit_ts": 0,
                },
            },
        }
        state = PersistedServiceState(
            version=STATE_FILE_VERSION,
            guards={},
            last_trade_id=5,
            trip_count=0,
            updated_at_ms=int(time.time() * 1000),
            tracker=tracker_data,
        )
        path = str(tmp_path / "state.json")
        save_consec_loss_state(path, state)

        loaded = load_consec_loss_state(path)
        assert loaded is not None
        assert loaded.tracker is not None
        assert loaded.tracker["positions"]["BTCUSDT|long"]["qty"] == "0.1"
