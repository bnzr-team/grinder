#!/usr/bin/env python3
"""Fixture-based soak runner for CI gate.

Runs PaperEngine on registered fixtures multiple times and collects metrics:
- Processing latency (p50, p99)
- Memory usage (RSS max)
- Fill rate
- Error counts

Usage:
  python -m scripts.run_soak_fixtures --output artifacts/soak_fixtures.json
  python -m scripts.run_soak_fixtures --fixtures sample_day sample_day_allowed --runs 5
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import resource
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from grinder.paper import PaperEngine

# Default fixtures for soak testing
DEFAULT_FIXTURES = [
    Path("tests/fixtures/sample_day"),
    Path("tests/fixtures/sample_day_allowed"),
    Path("tests/fixtures/sample_day_toxic"),
    Path("tests/fixtures/sample_day_multisymbol"),
    Path("tests/fixtures/sample_day_controller"),
    Path("tests/fixtures/sample_day_drawdown"),
]

# Report schema version
SOAK_REPORT_SCHEMA_VERSION = "v1"


def get_rss_mb() -> float:
    """Get current RSS memory usage in MB."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss / 1024  # Convert KB to MB on Linux


@dataclass
class FixtureSoakResult:
    """Result for a single fixture soak run."""

    fixture_path: str
    runs: int
    latencies_ms: list[float] = field(default_factory=list)
    fill_counts: list[int] = field(default_factory=list)
    orders_placed_counts: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    digests: list[str] = field(default_factory=list)

    @property
    def latency_p50_ms(self) -> float:
        """50th percentile latency."""
        if not self.latencies_ms:
            return 0.0
        return statistics.median(self.latencies_ms)

    @property
    def latency_p99_ms(self) -> float:
        """99th percentile latency."""
        if not self.latencies_ms:
            return 0.0
        sorted_lat = sorted(self.latencies_ms)
        idx = int(len(sorted_lat) * 0.99)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def fill_rate(self) -> float:
        """Fill rate as ratio of fills to orders placed (0-1)."""
        total_fills = sum(self.fill_counts) if self.fill_counts else 0
        total_orders = sum(self.orders_placed_counts) if self.orders_placed_counts else 0
        if total_orders == 0:
            # No orders placed = 1.0 (nothing to fill, so "all filled")
            return 1.0
        return total_fills / total_orders

    @property
    def digest_stable(self) -> bool:
        """Check if all digests are identical (determinism)."""
        if not self.digests:
            return True
        return len(set(self.digests)) == 1

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "fixture_path": self.fixture_path,
            "runs": self.runs,
            "latency_p50_ms": round(self.latency_p50_ms, 2),
            "latency_p99_ms": round(self.latency_p99_ms, 2),
            "fill_rate": round(self.fill_rate, 2),
            "errors_total": len(self.errors),
            "digest_stable": self.digest_stable,
            "errors": self.errors,
        }


@dataclass
class SoakReport:
    """Complete soak test report."""

    schema_version: str
    mode: str
    fixtures_tested: int
    total_runs: int
    decision_latency_p50_ms: float = 0.0
    decision_latency_p99_ms: float = 0.0
    rss_mb_max: float = 0.0
    fill_rate: float = 0.0
    errors_total: int = 0
    events_dropped: int = 0
    all_digests_stable: bool = True
    results: list[FixtureSoakResult] = field(default_factory=list)
    report_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "fixtures_tested": self.fixtures_tested,
            "total_runs": self.total_runs,
            "decision_latency_p50_ms": round(self.decision_latency_p50_ms, 2),
            "decision_latency_p99_ms": round(self.decision_latency_p99_ms, 2),
            # Compatibility with existing threshold checks (order latency = decision latency for fixtures)
            "order_latency_p99_ms": round(self.decision_latency_p99_ms, 2),
            "event_queue_depth_max": 0,  # Not applicable for fixture-based
            "snapshot_queue_depth_max": 0,  # Not applicable for fixture-based
            "rss_mb_max": round(self.rss_mb_max, 0),
            "fill_rate": round(self.fill_rate, 4),
            "errors_total": self.errors_total,
            "events_dropped": self.events_dropped,
            "all_digests_stable": self.all_digests_stable,
            "results": [r.to_dict() for r in self.results],
            "report_digest": self.report_digest,
        }

    def to_json(self) -> str:
        """Serialize to deterministic JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_json_pretty(self) -> str:
        """Serialize to human-readable JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)


def load_fixture_config(fixture_path: Path) -> dict[str, Any]:
    """Load fixture config.json."""
    config_path = fixture_path / "config.json"
    if config_path.exists():
        with config_path.open() as f:
            result: dict[str, Any] = json.load(f)
            return result
    return {}


def run_fixture_soak(fixture_path: Path, runs: int = 3) -> FixtureSoakResult:
    """Run soak test on a single fixture."""
    result = FixtureSoakResult(fixture_path=str(fixture_path), runs=runs)

    if not fixture_path.exists():
        result.errors.append(f"Fixture not found: {fixture_path}")
        return result

    config = load_fixture_config(fixture_path)
    controller_enabled = config.get("controller_enabled", False)
    kill_switch_enabled = config.get("kill_switch_enabled", False)

    for _ in range(runs):
        try:
            # Create fresh engine
            engine = PaperEngine(
                controller_enabled=controller_enabled,
                kill_switch_enabled=kill_switch_enabled,
                initial_capital=Decimal("10000"),
                max_drawdown_pct=5.0,
            )

            # Measure execution time
            start = time.perf_counter()
            paper_result = engine.run(fixture_path)
            elapsed_ms = (time.perf_counter() - start) * 1000

            # Record metrics
            result.latencies_ms.append(elapsed_ms)
            result.fill_counts.append(paper_result.total_fills)
            result.orders_placed_counts.append(paper_result.orders_placed)
            result.digests.append(paper_result.digest)

            # Record any errors from the run
            if paper_result.errors:
                result.errors.extend(paper_result.errors)

            # Force GC between runs
            gc.collect()

        except Exception as e:
            result.errors.append(f"Run failed: {e!s}")

    return result


def run_soak(
    fixtures: list[Path] | None = None,
    runs_per_fixture: int = 3,
    mode: str = "baseline",
) -> SoakReport:
    """Run soak test on all fixtures."""
    if fixtures is None:
        fixtures = [f for f in DEFAULT_FIXTURES if f.exists()]

    all_latencies: list[float] = []
    all_fill_rates: list[float] = []
    total_errors = 0
    all_stable = True

    results: list[FixtureSoakResult] = []

    for fixture_path in fixtures:
        fixture_result = run_fixture_soak(fixture_path, runs=runs_per_fixture)
        results.append(fixture_result)

        all_latencies.extend(fixture_result.latencies_ms)
        if fixture_result.fill_counts:
            all_fill_rates.append(fixture_result.fill_rate)
        total_errors += len(fixture_result.errors)
        if not fixture_result.digest_stable:
            all_stable = False

    # Compute aggregate metrics
    latency_p50 = statistics.median(all_latencies) if all_latencies else 0.0
    latency_p99 = 0.0
    if all_latencies:
        sorted_lat = sorted(all_latencies)
        idx = int(len(sorted_lat) * 0.99)
        latency_p99 = sorted_lat[min(idx, len(sorted_lat) - 1)]

    avg_fill_rate = statistics.mean(all_fill_rates) if all_fill_rates else 0.0

    report = SoakReport(
        schema_version=SOAK_REPORT_SCHEMA_VERSION,
        mode=mode,
        fixtures_tested=len(fixtures),
        total_runs=len(fixtures) * runs_per_fixture,
        decision_latency_p50_ms=latency_p50,
        decision_latency_p99_ms=latency_p99,
        rss_mb_max=get_rss_mb(),
        fill_rate=avg_fill_rate,
        errors_total=total_errors,
        events_dropped=0,  # Not applicable for fixture-based
        all_digests_stable=all_stable,
        results=results,
    )

    # Compute report digest
    report_content = json.dumps(
        {k: v for k, v in report.to_dict().items() if k != "report_digest"},
        sort_keys=True,
        separators=(",", ":"),
    )
    report.report_digest = hashlib.sha256(report_content.encode()).hexdigest()[:16]

    return report


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Run fixture-based soak test")
    parser.add_argument(
        "--fixtures",
        nargs="+",
        type=str,
        help="Fixture names to test (default: all registered)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of runs per fixture (default: 3)",
    )
    parser.add_argument(
        "--mode",
        choices=["baseline", "stress"],
        default="baseline",
        help="Test mode (affects threshold profile)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON path",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress stdout output",
    )
    args = parser.parse_args()

    # Resolve fixtures
    fixtures: list[Path] | None = None
    if args.fixtures:
        base = Path("tests/fixtures")
        fixtures = [base / name for name in args.fixtures]

    # Run soak
    report = run_soak(fixtures=fixtures, runs_per_fixture=args.runs, mode=args.mode)

    json_output = report.to_json_pretty()

    if not args.quiet:
        print(json_output)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json_output + "\n")
        if not args.quiet:
            print(f"\nReport written to: {args.output}")

    # Exit with error if determinism broken or errors occurred
    if not report.all_digests_stable:
        print("\nERROR: Digest instability detected!", file=__import__("sys").stderr)
        __import__("sys").exit(1)

    if report.errors_total > 0:
        print(f"\nWARNING: {report.errors_total} error(s) occurred", file=__import__("sys").stderr)


if __name__ == "__main__":
    main()
