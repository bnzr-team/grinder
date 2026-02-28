"""Safety envelope contract tests (TRD-1).

Lock down mainnet safety guarantees as regression contracts:
- Dry-run defaults: armed=False, mode=READ_ONLY → 0 write ops
- Gate ordering: earlier gate prevents later evaluation (semantic, not line-based)
- Hard gates: kill-switch, drawdown, CLG each independently block
- NoOpExchangePort: no network I/O, only in-memory tracking

These are CONTRACT tests — if they break, a safety invariant changed.
See docs/20_SAFETY_ENVELOPE.md for the normative specification.
See ADR-076 in docs/DECISIONS.md for the design decision.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide
from grinder.execution.port import NoOpExchangePort
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import (
    BlockReason,
    LiveAction,
    LiveActionStatus,
    LiveEngineConfig,
    LiveEngineOutput,
    LiveEngineV0,
)
from grinder.live.fsm_driver import FsmDriver
from grinder.risk.consecutive_loss_guard import (
    ConsecutiveLossAction,
    ConsecutiveLossConfig,
    ConsecutiveLossGuard,
)
from grinder.risk.consecutive_loss_wiring import set_operator_pause
from grinder.risk.drawdown_guard_v1 import (
    DrawdownGuardV1,
    GuardState,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_paper() -> MagicMock:
    """PaperEngine mock returning configurable actions."""
    engine = MagicMock()
    engine.process_snapshot.return_value = MagicMock(actions=[])
    return engine


@pytest.fixture()
def noop_port() -> NoOpExchangePort:
    return NoOpExchangePort()


@pytest.fixture()
def tracking_port() -> MagicMock:
    """Mock port that records every call."""
    port = MagicMock()
    port.calls = []  # list of (op_name, kwargs) tuples

    def _place(**kw: object) -> str:
        port.calls.append(("place_order", kw))
        return f"ORDER_{len(port.calls)}"

    def _cancel(order_id: str) -> bool:
        port.calls.append(("cancel_order", {"order_id": order_id}))
        return True

    def _replace(**kw: object) -> str:
        port.calls.append(("replace_order", kw))
        return f"ORDER_{len(port.calls)}"

    port.place_order.side_effect = _place
    port.cancel_order.side_effect = _cancel
    port.replace_order.side_effect = _replace
    return port


@pytest.fixture()
def snapshot() -> Snapshot:
    return Snapshot(
        ts=1_000_000,
        symbol="BTCUSDT",
        bid_price=Decimal("50000"),
        ask_price=Decimal("50001"),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        last_price=Decimal("50000.5"),
        last_qty=Decimal("0.5"),
    )


@pytest.fixture()
def place_action() -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        price=Decimal("49000"),
        quantity=Decimal("0.01"),
        level_id=1,
        reason="GRID_ENTRY",
    )


@pytest.fixture()
def cancel_action() -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.CANCEL,
        order_id="ORDER_123",
        symbol="BTCUSDT",
        reason="GRID_EXIT",
    )


@pytest.fixture()
def replace_action() -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.REPLACE,
        order_id="ORDER_123",
        symbol="BTCUSDT",
        price=Decimal("49500"),
        quantity=Decimal("0.02"),
        level_id=2,
        reason="GRID_ADJUST",
    )


def _run_single(
    paper: MagicMock,
    port: MagicMock | NoOpExchangePort,
    config: LiveEngineConfig,
    snapshot: Snapshot,
    action: ExecutionAction,
    *,
    drawdown_guard: DrawdownGuardV1 | None = None,
    fsm_driver: FsmDriver | None = None,
) -> tuple[LiveEngineOutput, LiveAction]:
    """Helper: feed one action through LiveEngineV0, return (output, live_action)."""
    paper.process_snapshot.return_value = MagicMock(actions=[action])
    engine = LiveEngineV0(
        paper,
        port,
        config,
        drawdown_guard=drawdown_guard,
        fsm_driver=fsm_driver,
    )
    output = engine.process_snapshot(snapshot)
    return output, output.live_actions[0]


# ===========================================================================
# A) Dry-run contract (defaults = 0 writes)
# ===========================================================================


class TestDryRunContract:
    """Verify that default configuration produces zero write operations.

    SSOT formula (docs/20_SAFETY_ENVELOPE.md):
        Writes impossible unless armed=True AND mode=LIVE_TRADE AND port=futures.
        Defaults: armed=False, mode=READ_ONLY, port=noop.
    """

    def test_default_config_is_safe(self) -> None:
        """LiveEngineConfig() defaults to armed=False, mode=READ_ONLY."""
        cfg = LiveEngineConfig()
        assert cfg.armed is False
        assert cfg.mode == SafeMode.READ_ONLY
        assert cfg.kill_switch_active is False
        assert cfg.can_write() is False

    def test_armed_false_blocks_all_with_not_armed(
        self,
        mock_paper: MagicMock,
        tracking_port: MagicMock,
        snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """armed=False → BlockReason.NOT_ARMED, 0 port calls."""
        config = LiveEngineConfig(armed=False, mode=SafeMode.LIVE_TRADE)
        _, la = _run_single(mock_paper, tracking_port, config, snapshot, place_action)

        assert la.status == LiveActionStatus.BLOCKED
        assert la.block_reason == BlockReason.NOT_ARMED
        assert len(tracking_port.calls) == 0

    def test_read_only_blocks_all_with_mode_reason(
        self,
        mock_paper: MagicMock,
        tracking_port: MagicMock,
        snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """armed=True + mode=READ_ONLY → BlockReason.MODE_NOT_LIVE_TRADE, 0 port calls."""
        config = LiveEngineConfig(armed=True, mode=SafeMode.READ_ONLY)
        _, la = _run_single(mock_paper, tracking_port, config, snapshot, place_action)

        assert la.status == LiveActionStatus.BLOCKED
        assert la.block_reason == BlockReason.MODE_NOT_LIVE_TRADE
        assert len(tracking_port.calls) == 0

    def test_noop_port_no_network_io(
        self,
        noop_port: NoOpExchangePort,
    ) -> None:
        """NoOpExchangePort: place/cancel/replace store in memory, no HTTP.

        Contract: noop port has no http_client attribute (it does no network I/O).
        """
        order_id = noop_port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1_000_000,
        )
        assert isinstance(order_id, str)
        assert len(order_id) > 0

        # NoOpExchangePort has no http_client — it cannot do network I/O
        assert not hasattr(noop_port, "http_client")

        # Cancel works in-memory
        assert noop_port.cancel_order(order_id) is True

        # State is purely in-memory
        assert len(noop_port.fetch_open_orders("BTCUSDT")) == 0


# ===========================================================================
# B) Gate ordering contract (semantic, not line-based)
# ===========================================================================


class TestGateOrdering:
    """Verify gate ordering via observed behavior.

    The gate chain is:
        1. armed  2. mode  3. kill_switch  4. whitelist  5. drawdown  6. FSM  7. fill_prob

    We prove ordering by showing that an earlier gate blocks BEFORE a later
    gate has any chance to fire.  The technique: configure a later gate to
    block, but trigger an earlier gate — assert the earlier BlockReason is
    returned and the later gate object was never consulted.
    """

    def test_armed_blocks_before_kill_switch(
        self,
        mock_paper: MagicMock,
        tracking_port: MagicMock,
        snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """armed=False fires before kill_switch check.

        Both armed=False and kill_switch_active=True would block.
        If ordering is correct → BlockReason.NOT_ARMED (gate 1 wins).
        """
        config = LiveEngineConfig(
            armed=False,
            mode=SafeMode.LIVE_TRADE,
            kill_switch_active=True,
        )
        _, la = _run_single(mock_paper, tracking_port, config, snapshot, place_action)

        assert la.block_reason == BlockReason.NOT_ARMED

    def test_mode_blocks_before_kill_switch(
        self,
        mock_paper: MagicMock,
        tracking_port: MagicMock,
        snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """mode=READ_ONLY fires before kill_switch.

        armed=True + mode=READ_ONLY + kill_switch_active=True.
        If ordering is correct → BlockReason.MODE_NOT_LIVE_TRADE (gate 2 wins).
        """
        config = LiveEngineConfig(
            armed=True,
            mode=SafeMode.READ_ONLY,
            kill_switch_active=True,
        )
        _, la = _run_single(mock_paper, tracking_port, config, snapshot, place_action)

        assert la.block_reason == BlockReason.MODE_NOT_LIVE_TRADE

    def test_kill_switch_blocks_before_drawdown(
        self,
        mock_paper: MagicMock,
        tracking_port: MagicMock,
        snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """kill_switch fires before drawdown guard.

        Spy DrawdownGuardV1: if .allow() was never called, kill_switch
        (gate 3) blocked before reaching drawdown (gate 5).
        """
        spy_guard = MagicMock(spec=DrawdownGuardV1)

        config = LiveEngineConfig(
            armed=True,
            mode=SafeMode.LIVE_TRADE,
            kill_switch_active=True,
        )
        _, la = _run_single(
            mock_paper,
            tracking_port,
            config,
            snapshot,
            place_action,
            drawdown_guard=spy_guard,
        )

        assert la.block_reason == BlockReason.KILL_SWITCH_ACTIVE
        spy_guard.allow.assert_not_called()

    def test_whitelist_blocks_before_drawdown(
        self,
        mock_paper: MagicMock,
        tracking_port: MagicMock,
        snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """whitelist fires before drawdown guard.

        Spy DrawdownGuardV1: .allow() never called if symbol is not whitelisted.
        """
        spy_guard = MagicMock(spec=DrawdownGuardV1)

        config = LiveEngineConfig(
            armed=True,
            mode=SafeMode.LIVE_TRADE,
            symbol_whitelist=["ETHUSDT"],  # BTCUSDT not allowed
        )
        _, la = _run_single(
            mock_paper,
            tracking_port,
            config,
            snapshot,
            place_action,
            drawdown_guard=spy_guard,
        )

        assert la.block_reason == BlockReason.SYMBOL_NOT_WHITELISTED
        spy_guard.allow.assert_not_called()

    def test_drawdown_blocks_before_fsm(
        self,
        mock_paper: MagicMock,
        tracking_port: MagicMock,
        snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """drawdown guard fires before FSM driver.

        Configure drawdown guard to block, provide spy FsmDriver.
        If ordering is correct → DRAWDOWN_BLOCKED, fsm.check_intent never called.
        """
        guard = DrawdownGuardV1()
        # Force DRAWDOWN state
        guard._state = GuardState.DRAWDOWN

        spy_fsm = MagicMock(spec=FsmDriver)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        _, la = _run_single(
            mock_paper,
            tracking_port,
            config,
            snapshot,
            place_action,
            drawdown_guard=guard,
            fsm_driver=spy_fsm,
        )

        assert la.block_reason == BlockReason.DRAWDOWN_BLOCKED
        spy_fsm.check_intent.assert_not_called()


# ===========================================================================
# C) Hard gates
# ===========================================================================


class TestHardGates:
    """Verify each hard gate independently blocks risk-increasing actions."""

    def test_kill_switch_blocks_place_allows_cancel(
        self,
        mock_paper: MagicMock,
        tracking_port: MagicMock,
        snapshot: Snapshot,
        place_action: ExecutionAction,
        cancel_action: ExecutionAction,
    ) -> None:
        """kill_switch_active=True → PLACE blocked, CANCEL allowed."""
        mock_paper.process_snapshot.return_value = MagicMock(actions=[place_action, cancel_action])
        config = LiveEngineConfig(
            armed=True,
            mode=SafeMode.LIVE_TRADE,
            kill_switch_active=True,
        )
        engine = LiveEngineV0(mock_paper, tracking_port, config)
        output = engine.process_snapshot(snapshot)

        # PLACE → blocked
        assert output.live_actions[0].block_reason == BlockReason.KILL_SWITCH_ACTIVE
        # CANCEL → executed
        assert output.live_actions[1].status == LiveActionStatus.EXECUTED
        # Only the cancel reached the port
        assert len(tracking_port.calls) == 1
        assert tracking_port.calls[0][0] == "cancel_order"

    def test_drawdown_blocks_increase_allows_cancel(
        self,
        mock_paper: MagicMock,
        tracking_port: MagicMock,
        snapshot: Snapshot,
        place_action: ExecutionAction,
        cancel_action: ExecutionAction,
    ) -> None:
        """DrawdownGuardV1 in DRAWDOWN state blocks PLACE, allows CANCEL."""
        mock_paper.process_snapshot.return_value = MagicMock(actions=[place_action, cancel_action])
        guard = DrawdownGuardV1()
        guard._state = GuardState.DRAWDOWN

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper, tracking_port, config, drawdown_guard=guard)
        output = engine.process_snapshot(snapshot)

        assert output.live_actions[0].block_reason == BlockReason.DRAWDOWN_BLOCKED
        assert output.live_actions[1].status == LiveActionStatus.EXECUTED
        assert len(tracking_port.calls) == 1
        assert tracking_port.calls[0][0] == "cancel_order"

    def test_consecutive_loss_guard_trips_at_threshold(self) -> None:
        """ConsecutiveLossGuard trips after N consecutive losses.

        This guard is NOT in the engine gate chain — it's wired into the
        reconcile pipeline (consecutive_loss_wiring.py).  On trip, it sets
        GRINDER_OPERATOR_OVERRIDE=PAUSE which triggers FSM pause (gate 6).
        """
        config = ConsecutiveLossConfig(
            enabled=True,
            threshold=3,
            action=ConsecutiveLossAction.PAUSE,
        )
        guard = ConsecutiveLossGuard(config)

        # Feed consecutive losses
        tripped = guard.update("loss", row_id="r1", ts_ms=1000)
        assert not tripped
        tripped = guard.update("loss", row_id="r2", ts_ms=2000)
        assert not tripped
        tripped = guard.update("loss", row_id="r3", ts_ms=3000)
        assert tripped  # threshold=3, third loss trips

        assert guard.count == 3

    def test_consecutive_loss_wiring_sets_operator_pause(self) -> None:
        """set_operator_pause() sets GRINDER_OPERATOR_OVERRIDE=PAUSE.

        This is the mechanism by which CLG triggers FSM pause (indirect gate).
        """
        # Clear env first
        os.environ.pop("GRINDER_OPERATOR_OVERRIDE", None)

        set_operator_pause(count=3, threshold=3)

        assert os.environ.get("GRINDER_OPERATOR_OVERRIDE") == "PAUSE"

        # Clean up
        os.environ.pop("GRINDER_OPERATOR_OVERRIDE", None)
