"""Tests for constraint provider (M7-06, ADR-060).

Tests verify:
- Parsing LOT_SIZE filters from exchangeInfo
- Decimal parsing determinism
- Cache loading/saving
- Integration with ExecutionEngine constraints
"""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from grinder.core import GridMode
from grinder.execution import ExecutionEngine, ExecutionState, NoOpExchangePort
from grinder.execution.constraint_provider import (
    ConstraintParseError,
    ConstraintProvider,
    ConstraintProviderConfig,
    load_constraints_from_file,
    parse_exchange_info,
    parse_lot_size_filter,
)
from grinder.execution.engine import SymbolConstraints
from grinder.policies.base import GridPlan

# --- Fixtures ---

FIXTURE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "exchange_info" / "binance_futures_usdt.json"
)


@pytest.fixture
def exchange_info_data() -> dict:
    """Load exchange info fixture."""
    with FIXTURE_PATH.open() as f:
        return json.load(f)


@pytest.fixture
def temp_cache_dir() -> Path:
    """Create temporary directory for cache tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# --- Tests: parse_lot_size_filter ---


class TestParseLotSizeFilter:
    """Tests for LOT_SIZE filter parsing."""

    def test_parse_lot_size_btc(self) -> None:
        """Test parsing LOT_SIZE filter for BTC (step=0.001, min=0.001)."""
        filters = [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
        ]

        result = parse_lot_size_filter(filters)

        assert result is not None
        step_size, min_qty = result
        assert step_size == Decimal("0.001")
        assert min_qty == Decimal("0.001")

    def test_parse_lot_size_sol(self) -> None:
        """Test parsing LOT_SIZE filter for SOL (step=1, min=1)."""
        filters = [
            {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "1000000", "stepSize": "1"},
        ]

        result = parse_lot_size_filter(filters)

        assert result is not None
        step_size, min_qty = result
        assert step_size == Decimal("1")
        assert min_qty == Decimal("1")

    def test_parse_lot_size_missing(self) -> None:
        """Test returns None when no LOT_SIZE filter present."""
        filters = [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ]

        result = parse_lot_size_filter(filters)

        assert result is None

    def test_parse_lot_size_invalid(self) -> None:
        """Test returns None when LOT_SIZE filter has invalid data."""
        filters = [
            {"filterType": "LOT_SIZE", "minQty": "invalid", "stepSize": "0.001"},
        ]

        result = parse_lot_size_filter(filters)

        assert result is None


# --- Tests: parse_exchange_info ---


class TestParseExchangeInfo:
    """Tests for full exchangeInfo parsing."""

    def test_parse_all_symbols(self, exchange_info_data: dict) -> None:
        """Test parsing all symbols from fixture."""
        constraints = parse_exchange_info(exchange_info_data)

        assert len(constraints) == 3
        assert "BTCUSDT" in constraints
        assert "ETHUSDT" in constraints
        assert "SOLUSDT" in constraints

    def test_parse_btc_constraints(self, exchange_info_data: dict) -> None:
        """Test BTCUSDT constraints parsed correctly."""
        constraints = parse_exchange_info(exchange_info_data)

        btc = constraints["BTCUSDT"]
        assert btc.step_size == Decimal("0.001")
        assert btc.min_qty == Decimal("0.001")

    def test_parse_sol_constraints(self, exchange_info_data: dict) -> None:
        """Test SOLUSDT constraints parsed correctly (integer qty)."""
        constraints = parse_exchange_info(exchange_info_data)

        sol = constraints["SOLUSDT"]
        assert sol.step_size == Decimal("1")
        assert sol.min_qty == Decimal("1")

    def test_parse_deterministic(self, exchange_info_data: dict) -> None:
        """Test parsing is deterministic (same input = same output)."""
        constraints1 = parse_exchange_info(exchange_info_data)
        constraints2 = parse_exchange_info(exchange_info_data)

        for symbol in constraints1:
            assert constraints1[symbol] == constraints2[symbol]

    def test_parse_missing_symbols_key(self) -> None:
        """Test raises error when symbols key is missing."""
        with pytest.raises(ConstraintParseError, match="missing 'symbols'"):
            parse_exchange_info({"timezone": "UTC"})


# --- Tests: load_constraints_from_file ---


class TestLoadConstraintsFromFile:
    """Tests for loading constraints from JSON file."""

    def test_load_from_fixture(self) -> None:
        """Test loading from fixture file."""
        constraints = load_constraints_from_file(FIXTURE_PATH)

        assert len(constraints) == 3
        assert constraints["BTCUSDT"].step_size == Decimal("0.001")

    def test_load_file_not_found(self) -> None:
        """Test raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_constraints_from_file(Path("/nonexistent/file.json"))


# --- Tests: ConstraintProvider ---


class TestConstraintProvider:
    """Tests for ConstraintProvider class."""

    def test_from_cache_file(self) -> None:
        """Test loading from existing cache file."""
        provider = ConstraintProvider.from_cache(FIXTURE_PATH)
        constraints = provider.get_constraints()

        assert len(constraints) == 3
        assert "BTCUSDT" in constraints

    def test_get_constraint_single(self) -> None:
        """Test getting constraint for single symbol."""
        provider = ConstraintProvider.from_cache(FIXTURE_PATH)

        btc = provider.get_constraint("BTCUSDT")
        assert btc is not None
        assert btc.step_size == Decimal("0.001")

        missing = provider.get_constraint("DOESNOTEXIST")
        assert missing is None

    def test_cache_writes_and_reads(self, temp_cache_dir: Path) -> None:
        """Test caching: save then load from file."""
        # Load original data
        with FIXTURE_PATH.open() as f:
            data = json.load(f)

        # Create provider with custom cache dir
        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="test_cache.json",
            allow_fetch=False,
        )
        provider = ConstraintProvider(config=config)

        # Manually save to cache (simulating API fetch)
        provider._save_to_cache(data)

        # Now load should work
        constraints = provider._load_from_cache()
        assert constraints is not None
        assert len(constraints) == 3

    def test_empty_constraints_when_no_source(self, temp_cache_dir: Path) -> None:
        """Test returns empty dict when no cache and no API available."""
        config = ConstraintProviderConfig(
            cache_dir=temp_cache_dir,
            cache_file="nonexistent.json",
            allow_fetch=False,
        )
        provider = ConstraintProvider(config=config)

        constraints = provider.get_constraints()
        assert constraints == {}


# --- Tests: Integration with ExecutionEngine ---


class TestConstraintProviderIntegration:
    """Integration tests: ConstraintProvider + ExecutionEngine."""

    def test_provider_constraints_applied_in_engine(self) -> None:
        """Test constraints from provider are applied by engine."""
        # Load constraints from fixture
        constraints = load_constraints_from_file(FIXTURE_PATH)

        # Create engine with constraints
        port = NoOpExchangePort()
        engine = ExecutionEngine(port=port, symbol_constraints=constraints)

        # Plan with small qty that will be floored
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.0015")],  # Will floor to 0.001
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        # Qty should be floored to 0.001 (step_size)
        # Since 0.001 >= min_qty (0.001), orders should be placed
        placed_orders = list(result.state.open_orders.values())
        assert len(placed_orders) == 2
        for order in placed_orders:
            assert order.quantity == Decimal("0.001")

    def test_provider_constraints_skip_below_min(self) -> None:
        """Test orders below min_qty are skipped."""
        # Create custom constraints with high min_qty
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.01"),  # Higher than our test qty
            )
        }

        port = NoOpExchangePort()
        engine = ExecutionEngine(port=port, symbol_constraints=constraints)

        # Plan with qty that will be below min after rounding
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.005")],  # Below min_qty of 0.01
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        # Orders should be skipped
        assert len(result.state.open_orders) == 0
        skipped_events = [e for e in result.events if e.event_type == "ORDER_SKIPPED"]
        assert len(skipped_events) == 2

    def test_sol_integer_qty_constraints(self) -> None:
        """Test SOL constraints with integer qty (step=1, min=1)."""
        constraints = load_constraints_from_file(FIXTURE_PATH)

        port = NoOpExchangePort()
        engine = ExecutionEngine(port=port, symbol_constraints=constraints)

        # Plan with fractional qty that will floor to 0
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("100"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.5")],  # Floors to 0, below min=1
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "SOLUSDT", empty_state, ts=1000)

        # Orders should be skipped (qty floors to 0, below min=1)
        assert len(result.state.open_orders) == 0
        skipped_events = [e for e in result.events if e.event_type == "ORDER_SKIPPED"]
        assert len(skipped_events) == 2
