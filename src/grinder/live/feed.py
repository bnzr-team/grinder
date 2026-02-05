"""Live feed pipeline: WebSocket → Snapshot → Features.

This module provides the read-only data pipeline for live market data:
1. Receives Snapshot objects from a DataConnector (WS or mock)
2. Feeds snapshots through FeatureEngine (BarBuilder + indicators)
3. Emits LiveFeaturesUpdate objects

IMPORTANT: This module is READ-ONLY. It has NO imports from execution/
and makes ZERO trading calls. This is enforced by test_no_execution_imports.

Usage:
    config = LiveFeedConfig(symbols=["BTCUSDT"])
    feed = LiveFeed(config)

    async for update in feed.run(connector):
        print(f"Features: {update.features}")

See ADR-037 for design decisions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from grinder.features.engine import FeatureEngine, FeatureEngineConfig
from grinder.live.types import LiveFeaturesUpdate, LiveFeedStats

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from grinder.connectors.data_connector import DataConnector
    from grinder.contracts import Snapshot

logger = logging.getLogger(__name__)


@dataclass
class LiveFeedConfig:
    """Configuration for live feed pipeline.

    Attributes:
        symbols: Symbols to process (filter; empty = all)
        feature_config: FeatureEngine configuration
        max_queue_size: Maximum snapshot queue size
        warmup_bars: Bars needed before is_warmed_up=True
    """

    symbols: list[str] = field(default_factory=list)
    feature_config: FeatureEngineConfig = field(default_factory=FeatureEngineConfig)
    max_queue_size: int = 1000
    warmup_bars: int = 15  # ATR(14) + 1

    def is_symbol_allowed(self, symbol: str) -> bool:
        """Check if symbol should be processed."""
        if not self.symbols:
            return True  # No filter = all symbols
        return symbol in self.symbols


class LiveFeed:
    """Live feed pipeline: DataConnector → Features.

    This class orchestrates the read-only data pipeline:
    1. Receives Snapshot objects from a DataConnector
    2. Filters by configured symbols
    3. Feeds through FeatureEngine (BarBuilder + indicators)
    4. Yields LiveFeaturesUpdate objects

    Thread safety: NOT thread-safe. Use one instance per feed.

    Example:
        feed = LiveFeed(LiveFeedConfig(symbols=["BTCUSDT"]))
        async for update in feed.run(ws_connector):
            if update.is_warmed_up:
                print(f"Features ready: {update.features}")
    """

    def __init__(
        self,
        config: LiveFeedConfig,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize live feed.

        Args:
            config: Feed configuration
            clock: Clock function for timestamps (injectable for testing)
        """
        self._config = config
        self._clock = clock or time.time
        self._feature_engine = FeatureEngine(config.feature_config)
        self._stats = LiveFeedStats()
        self._running = False

    @property
    def config(self) -> LiveFeedConfig:
        """Get feed configuration."""
        return self._config

    @property
    def stats(self) -> LiveFeedStats:
        """Get feed statistics."""
        return self._stats

    @property
    def feature_engine(self) -> FeatureEngine:
        """Get feature engine instance."""
        return self._feature_engine

    async def run(self, connector: DataConnector) -> AsyncIterator[LiveFeaturesUpdate]:
        """Run the feed pipeline.

        Args:
            connector: DataConnector yielding Snapshot objects

        Yields:
            LiveFeaturesUpdate for each processed snapshot
        """
        self._running = True
        logger.info("LiveFeed started, symbols=%s", self._config.symbols or "all")

        try:
            async for snapshot in connector.iter_snapshots():
                if not self._running:
                    break

                # Filter by symbol
                if not self._config.is_symbol_allowed(snapshot.symbol):
                    continue

                # Process snapshot
                update = self._process_snapshot(snapshot)
                if update is not None:
                    yield update

        except Exception as e:
            logger.error("LiveFeed error: %s", str(e))
            self._stats.record_error()
            raise
        finally:
            self._running = False
            logger.info(
                "LiveFeed stopped, stats=%s",
                self._stats.to_dict(),
            )

    def _process_snapshot(self, snapshot: Snapshot) -> LiveFeaturesUpdate | None:
        """Process a single snapshot through the feature engine.

        Args:
            snapshot: Market data snapshot

        Returns:
            LiveFeaturesUpdate with computed features
        """
        start_ts = int(self._clock() * 1000)

        try:
            # Get bar count before processing
            bars_before = self._feature_engine.get_bar_count(snapshot.symbol)

            # Process through feature engine
            features = self._feature_engine.process_snapshot(snapshot)

            # Get bar count after processing
            bars_after = self._feature_engine.get_bar_count(snapshot.symbol)
            bar_completed = bars_after > bars_before

            # Calculate latency
            end_ts = int(self._clock() * 1000)
            latency_ms = end_ts - start_ts

            # Update stats
            self._stats.record_tick(latency_ms)
            self._stats.last_ts = snapshot.ts
            if bar_completed:
                self._stats.record_bar()

            # Build update
            return LiveFeaturesUpdate(
                ts=snapshot.ts,
                symbol=snapshot.symbol,
                features=features,
                bar_completed=bar_completed,
                bars_available=bars_after,
                is_warmed_up=bars_after >= self._config.warmup_bars,
                latency_ms=latency_ms,
            )

        except Exception as e:
            logger.warning(
                "Failed to process snapshot ts=%d symbol=%s: %s",
                snapshot.ts,
                snapshot.symbol,
                str(e),
            )
            self._stats.record_error()
            return None

    def process_snapshot_sync(self, snapshot: Snapshot) -> LiveFeaturesUpdate | None:
        """Synchronous version of snapshot processing.

        Useful for testing and batch processing.

        Args:
            snapshot: Market data snapshot

        Returns:
            LiveFeaturesUpdate with computed features
        """
        return self._process_snapshot(snapshot)

    def stop(self) -> None:
        """Stop the feed pipeline."""
        self._running = False

    def reset(self) -> None:
        """Reset feed state."""
        self._feature_engine.reset()
        self._stats = LiveFeedStats()
        self._running = False

    def get_latest_features(self, symbol: str) -> LiveFeaturesUpdate | None:
        """Get latest features for a symbol.

        Args:
            symbol: Trading symbol

        Returns:
            Latest LiveFeaturesUpdate or None if not available
        """
        features = self._feature_engine.get_latest_snapshot(symbol)
        if features is None:
            return None

        bars = self._feature_engine.get_bar_count(symbol)
        return LiveFeaturesUpdate(
            ts=features.ts,
            symbol=symbol,
            features=features,
            bar_completed=False,  # Unknown for cached
            bars_available=bars,
            is_warmed_up=bars >= self._config.warmup_bars,
        )


class LiveFeedRunner:
    """Convenience runner for LiveFeed with lifecycle management.

    Handles:
    - Connector lifecycle (connect/close)
    - Feed lifecycle
    - Graceful shutdown

    Example:
        runner = LiveFeedRunner(feed_config, ws_config)
        async for update in runner.run():
            process(update)
    """

    def __init__(
        self,
        feed_config: LiveFeedConfig,
        connector_factory: Callable[[], DataConnector],
    ) -> None:
        """Initialize runner.

        Args:
            feed_config: LiveFeed configuration
            connector_factory: Factory function to create DataConnector
        """
        self._feed_config = feed_config
        self._connector_factory = connector_factory
        self._feed: LiveFeed | None = None
        self._connector: DataConnector | None = None

    async def run(self) -> AsyncIterator[LiveFeaturesUpdate]:
        """Run the feed pipeline with managed lifecycle.

        Yields:
            LiveFeaturesUpdate for each processed snapshot
        """
        self._feed = LiveFeed(self._feed_config)
        self._connector = self._connector_factory()

        try:
            await self._connector.connect()
            async for update in self._feed.run(self._connector):
                yield update
        finally:
            if self._connector is not None:
                await self._connector.close()

    async def stop(self) -> None:
        """Stop the runner."""
        if self._feed is not None:
            self._feed.stop()
        if self._connector is not None:
            await self._connector.close()
