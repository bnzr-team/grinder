"""Integration tests for DataConnector with PaperEngine.

Proves that BinanceWsMockConnector can feed the paper trading engine
and produce stable, deterministic outcomes.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from grinder.connectors import BinanceWsMockConnector, ConnectorState
from grinder.paper.engine import PaperEngine

if TYPE_CHECKING:
    from grinder.contracts import Snapshot


class TestConnectorEngineIntegration:
    """Integration tests for connector → engine data flow."""

    @pytest.mark.asyncio
    async def test_connector_feeds_engine_deterministically(self) -> None:
        """Connector feeds snapshots to engine with deterministic output.

        This test proves:
        1. Connector loads fixture and iterates snapshots
        2. Engine processes each snapshot
        3. Output digest is stable across runs
        """
        fixture_path = Path("tests/fixtures/sample_day")

        # Run 1
        digest1 = await self._run_engine_with_connector(fixture_path)

        # Run 2
        digest2 = await self._run_engine_with_connector(fixture_path)

        # Digests must match for determinism
        assert digest1 == digest2, f"Non-deterministic: {digest1} != {digest2}"

    @pytest.mark.asyncio
    async def test_connector_with_allowed_fixture(self) -> None:
        """Connector works with fixture that produces orders."""
        fixture_path = Path("tests/fixtures/sample_day_allowed")

        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        snapshots: list[Snapshot] = []
        async for snapshot in connector.iter_snapshots():
            snapshots.append(snapshot)

        await connector.close()

        # Verify snapshots were loaded
        assert len(snapshots) > 0
        assert connector.stats.snapshots_delivered == len(snapshots)
        assert connector.state == ConnectorState.CLOSED

    @pytest.mark.asyncio
    async def test_connector_symbol_filter_integration(self) -> None:
        """Connector symbol filter works end-to-end."""
        fixture_path = Path("tests/fixtures/sample_day")

        # Without filter - get all symbols
        connector_all = BinanceWsMockConnector(fixture_path)
        await connector_all.connect()
        all_symbols = set()
        async for snapshot in connector_all.iter_snapshots():
            all_symbols.add(snapshot.symbol)
        await connector_all.close()

        # With filter - only BTCUSDT
        connector_btc = BinanceWsMockConnector(fixture_path, symbols=["BTCUSDT"])
        await connector_btc.connect()
        btc_symbols = set()
        async for snapshot in connector_btc.iter_snapshots():
            btc_symbols.add(snapshot.symbol)
        await connector_btc.close()

        # Verify filter worked
        assert "BTCUSDT" in all_symbols
        assert btc_symbols == {"BTCUSDT"}

    @pytest.mark.asyncio
    async def test_connector_reconnect_resumes_correctly(self) -> None:
        """Connector reconnect preserves position and resumes correctly."""
        fixture_path = Path("tests/fixtures/sample_day")

        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        # Read partial data
        first_batch: list[int] = []
        count = 0
        async for snapshot in connector.iter_snapshots():
            first_batch.append(snapshot.ts)
            count += 1
            if count >= 2:
                break

        last_ts_before = connector.last_seen_ts
        assert last_ts_before is not None  # Ensure we read something

        # Reconnect
        await connector.reconnect()
        assert connector.state == ConnectorState.CONNECTED

        # Read remaining
        second_batch: list[int] = []
        async for snapshot in connector.iter_snapshots():
            second_batch.append(snapshot.ts)

        await connector.close()

        # Verify no overlap - all timestamps in second batch > last_ts_before
        assert all(ts > last_ts_before for ts in second_batch)
        # Verify we got the rest
        assert len(first_batch) + len(second_batch) == connector.stats.events_loaded

    @pytest.mark.asyncio
    async def test_connector_stats_accuracy(self) -> None:
        """Connector stats accurately reflect processing."""
        fixture_path = Path("tests/fixtures/sample_day")

        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        delivered_count = 0
        async for _ in connector.iter_snapshots():
            delivered_count += 1

        await connector.close()

        # Stats should match actual delivery
        assert connector.stats.snapshots_delivered == delivered_count
        assert connector.stats.events_loaded >= delivered_count
        assert len(connector.stats.errors) == 0

    async def _run_engine_with_connector(self, fixture_path: Path) -> str:
        """Run paper engine fed by connector, return output digest."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        # Collect all snapshots
        snapshots: list[Snapshot] = []
        async for snapshot in connector.iter_snapshots():
            snapshots.append(snapshot)

        await connector.close()

        # Feed to engine
        engine = PaperEngine(
            spacing_bps=50,
            levels=3,
        )

        outputs: list[dict[str, Any]] = []
        for snapshot in snapshots:
            result = engine.process_snapshot(snapshot)
            outputs.append(result.to_dict())

        # Compute digest of outputs
        serialized = json.dumps(outputs, sort_keys=True, default=str)
        digest = hashlib.sha256(serialized.encode()).hexdigest()[:16]

        return digest


class TestConnectorEdgeCases:
    """Edge case tests for connector integration."""

    @pytest.mark.asyncio
    async def test_empty_fixture_produces_no_snapshots(self) -> None:
        """Connector handles empty fixture gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = Path(tmpdir)
            # Create empty events.jsonl
            (fixture_path / "events.jsonl").write_text("")

            connector = BinanceWsMockConnector(fixture_path)
            await connector.connect()

            count = 0
            async for _ in connector.iter_snapshots():
                count += 1

            await connector.close()

            assert count == 0
            assert connector.stats.snapshots_delivered == 0

    @pytest.mark.asyncio
    async def test_connector_idempotency_across_reconnects(self) -> None:
        """Multiple reconnects maintain idempotency guarantees."""
        fixture_path = Path("tests/fixtures/sample_day")

        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        all_timestamps: list[int] = []

        # First iteration - partial
        async for snapshot in connector.iter_snapshots():
            all_timestamps.append(snapshot.ts)
            break  # Just one

        # Multiple reconnects
        for _ in range(3):
            await connector.reconnect()
            async for snapshot in connector.iter_snapshots():
                all_timestamps.append(snapshot.ts)
                break  # One per reconnect

        await connector.close()

        # All timestamps should be unique and increasing
        assert len(all_timestamps) == len(set(all_timestamps))
        assert all_timestamps == sorted(all_timestamps)
