"""Unit tests for Top-K v1 selector (ASM-P1-06).

Tests verify:
- Hard gates (toxicity, spread, thin_l1, warmup)
- Scoring formula monotonicity
- Deterministic tie-breaking
- K clamping when fewer candidates
- Score component correctness

See: docs/17_ADAPTIVE_SMART_GRID_V1.md, ADR-023
"""

from __future__ import annotations

from decimal import Decimal

from grinder.selection.topk_v1 import (
    SelectionCandidate,
    TopKConfigV1,
    _ilog10,
    select_topk_v1,
)


class TestHardGates:
    """Tests for hard gate exclusions."""

    def test_toxicity_blocked_excluded(self) -> None:
        """Toxicity blocked candidates are excluded when tox_blocked_exclude=True."""
        candidates = [
            SelectionCandidate(
                symbol="GOODUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
                toxicity_blocked=False,
            ),
            SelectionCandidate(
                symbol="TOXICUSDT",
                range_score=200,  # Higher score but toxic
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
                toxicity_blocked=True,
            ),
        ]
        config = TopKConfigV1(k=2, tox_blocked_exclude=True)
        result = select_topk_v1(candidates, config)

        assert "GOODUSDT" in result.selected
        assert "TOXICUSDT" not in result.selected
        assert result.gate_excluded == 1

        # Check gate failure recorded
        toxic_score = next(s for s in result.scores if s.symbol == "TOXICUSDT")
        assert "TOXICITY_BLOCKED" in toxic_score.gates_failed

    def test_toxicity_blocked_included_when_disabled(self) -> None:
        """Toxicity blocked candidates included when tox_blocked_exclude=False."""
        candidates = [
            SelectionCandidate(
                symbol="TOXICUSDT",
                range_score=200,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
                toxicity_blocked=True,
            ),
        ]
        config = TopKConfigV1(k=1, tox_blocked_exclude=False)
        result = select_topk_v1(candidates, config)

        assert "TOXICUSDT" in result.selected
        assert result.gate_excluded == 0

    def test_spread_too_wide_excluded(self) -> None:
        """Spread > spread_max_bps is excluded."""
        candidates = [
            SelectionCandidate(
                symbol="WIDEUSDT",
                range_score=100,
                spread_bps=100,  # 100 bps > 50 bps default
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="TIGHTUSDT",
                range_score=50,
                spread_bps=20,  # 20 bps < 50 bps
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        config = TopKConfigV1(k=2, spread_max_bps=50)
        result = select_topk_v1(candidates, config)

        assert "TIGHTUSDT" in result.selected
        assert "WIDEUSDT" not in result.selected

        wide_score = next(s for s in result.scores if s.symbol == "WIDEUSDT")
        assert "SPREAD_TOO_WIDE" in wide_score.gates_failed

    def test_thin_book_excluded(self) -> None:
        """thin_l1 < thin_l1_min is excluded."""
        candidates = [
            SelectionCandidate(
                symbol="THINUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("0.5"),  # < 1.0 default
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="DEEPUSDT",
                range_score=50,
                spread_bps=10,
                thin_l1=Decimal("10.0"),  # > 1.0
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        config = TopKConfigV1(k=2, thin_l1_min=Decimal("1.0"))
        result = select_topk_v1(candidates, config)

        assert "DEEPUSDT" in result.selected
        assert "THINUSDT" not in result.selected

        thin_score = next(s for s in result.scores if s.symbol == "THINUSDT")
        assert "THIN_BOOK" in thin_score.gates_failed

    def test_warmup_insufficient_excluded(self) -> None:
        """warmup_bars < warmup_min is excluded."""
        candidates = [
            SelectionCandidate(
                symbol="COLDUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=5,  # < 15 default
            ),
            SelectionCandidate(
                symbol="WARMUPDT",
                range_score=50,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,  # > 15
            ),
        ]
        config = TopKConfigV1(k=2, warmup_min=15)
        result = select_topk_v1(candidates, config)

        assert "WARMUPDT" in result.selected
        assert "COLDUSDT" not in result.selected

        cold_score = next(s for s in result.scores if s.symbol == "COLDUSDT")
        assert "WARMUP_INSUFFICIENT" in cold_score.gates_failed

    def test_multiple_gates_failed(self) -> None:
        """Multiple gate failures are recorded."""
        candidates = [
            SelectionCandidate(
                symbol="BADUSDT",
                range_score=100,
                spread_bps=100,  # Too wide
                thin_l1=Decimal("0.5"),  # Too thin
                net_return_bps=50,
                warmup_bars=5,  # Not warmed up
                toxicity_blocked=True,  # Toxic
            ),
        ]
        config = TopKConfigV1(k=1)
        result = select_topk_v1(candidates, config)

        assert result.selected == []
        assert result.gate_excluded == 1

        bad_score = result.scores[0]
        assert "TOXICITY_BLOCKED" in bad_score.gates_failed
        assert "SPREAD_TOO_WIDE" in bad_score.gates_failed
        assert "THIN_BOOK" in bad_score.gates_failed
        assert "WARMUP_INSUFFICIENT" in bad_score.gates_failed


class TestScoringMonotonicity:
    """Tests for scoring formula monotonicity."""

    def test_higher_range_score_wins(self) -> None:
        """Higher range_score → higher total score (all else equal)."""
        candidates = [
            SelectionCandidate(
                symbol="CHOPPYUSDT",
                range_score=200,  # Higher = more choppy = better
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="SMOOTHUSDT",
                range_score=50,  # Lower
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        result = select_topk_v1(candidates)

        assert result.selected[0] == "CHOPPYUSDT"

        choppy_score = next(s for s in result.scores if s.symbol == "CHOPPYUSDT")
        smooth_score = next(s for s in result.scores if s.symbol == "SMOOTHUSDT")
        assert choppy_score.score > smooth_score.score

    def test_higher_liquidity_wins(self) -> None:
        """Higher thin_l1 → higher liquidity_score (all else equal)."""
        candidates = [
            SelectionCandidate(
                symbol="LIQUIDUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("1000.0"),  # High depth
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="ILLIQUIDUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("1.0"),  # Low depth
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        result = select_topk_v1(candidates)

        assert result.selected[0] == "LIQUIDUSDT"

        liquid_score = next(s for s in result.scores if s.symbol == "LIQUIDUSDT")
        illiquid_score = next(s for s in result.scores if s.symbol == "ILLIQUIDUSDT")
        assert liquid_score.liquidity_component > illiquid_score.liquidity_component

    def test_higher_trend_penalized(self) -> None:
        """Higher abs(net_return_bps) → lower score (trend penalty)."""
        candidates = [
            SelectionCandidate(
                symbol="TRENDUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=500,  # Strong trend
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="RANGEUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=10,  # Ranging
                warmup_bars=20,
            ),
        ]
        result = select_topk_v1(candidates)

        assert result.selected[0] == "RANGEUSDT"

        trend_score = next(s for s in result.scores if s.symbol == "TRENDUSDT")
        range_score = next(s for s in result.scores if s.symbol == "RANGEUSDT")
        assert trend_score.trend_penalty > range_score.trend_penalty
        assert trend_score.score < range_score.score

    def test_toxicity_penalty_applied(self) -> None:
        """Toxicity blocked applies penalty when tox_blocked_exclude=False."""
        candidates = [
            SelectionCandidate(
                symbol="TOXICUSDT",
                range_score=500,  # Very high but toxic
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
                toxicity_blocked=True,
            ),
            SelectionCandidate(
                symbol="CLEANUSDT",
                range_score=100,  # Lower but clean
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
                toxicity_blocked=False,
            ),
        ]
        # Allow toxic but with penalty
        config = TopKConfigV1(k=2, tox_blocked_exclude=False)
        result = select_topk_v1(candidates, config)

        toxic_score = next(s for s in result.scores if s.symbol == "TOXICUSDT")
        assert toxic_score.toxicity_penalty > 0


class TestTieBreaking:
    """Tests for deterministic tie-breaking."""

    def test_same_score_sorted_by_symbol(self) -> None:
        """Equal scores are sorted by symbol (lexicographic)."""
        candidates = [
            SelectionCandidate(
                symbol="ZZZUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="AAAUSDT",
                range_score=100,  # Same score
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="MMMUSDT",
                range_score=100,  # Same score
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        config = TopKConfigV1(k=3)
        result = select_topk_v1(candidates, config)

        # All equal scores → sorted by symbol alphabetically
        assert result.selected == ["AAAUSDT", "MMMUSDT", "ZZZUSDT"]

    def test_deterministic_ordering_multiple_runs(self) -> None:
        """Multiple runs produce identical results."""
        candidates = [
            SelectionCandidate(
                symbol="AAAUSDT",
                range_score=150,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="BBBUSDT",
                range_score=200,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="CCCUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        config = TopKConfigV1(k=2)

        result1 = select_topk_v1(candidates, config)
        result2 = select_topk_v1(candidates, config)

        assert result1.selected == result2.selected
        assert result1.scores[0].score == result2.scores[0].score


class TestKClamping:
    """Tests for K clamping behavior."""

    def test_fewer_candidates_than_k(self) -> None:
        """When fewer candidates than K, select all eligible."""
        candidates = [
            SelectionCandidate(
                symbol="ONLYUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        config = TopKConfigV1(k=5)
        result = select_topk_v1(candidates, config)

        assert result.selected == ["ONLYUSDT"]
        assert len(result.selected) == 1

    def test_all_gate_blocked_returns_empty(self) -> None:
        """All candidates blocked by gates → empty selection."""
        candidates = [
            SelectionCandidate(
                symbol="BADUSDT",
                range_score=100,
                spread_bps=100,  # Too wide
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        config = TopKConfigV1(k=3, spread_max_bps=50)
        result = select_topk_v1(candidates, config)

        assert result.selected == []
        assert result.gate_excluded == 1

    def test_empty_candidates_returns_empty(self) -> None:
        """Empty input returns empty selection."""
        result = select_topk_v1([])

        assert result.selected == []
        assert result.total_candidates == 0
        assert result.gate_excluded == 0


class TestScoreComponents:
    """Tests for score component calculation."""

    def test_score_components_correct(self) -> None:
        """Score components calculated correctly."""
        candidates = [
            SelectionCandidate(
                symbol="TESTUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("100.0"),  # log10(101) ≈ 2.004
                net_return_bps=200,
                warmup_bars=20,
            ),
        ]
        # Use default weights: w_range=100, w_liquidity=50, w_trend=100
        config = TopKConfigV1(k=1)
        result = select_topk_v1(candidates, config)

        score = result.scores[0]

        # range_component = 100 * 100 / 100 = 100
        assert score.range_component == 100

        # Liquidity component from log10(thin_l1+1) * liq_scale * w_liquidity / 100
        # Expected: log10(101) ≈ 2.004 → liq_raw ≈ 2004 → component ≈ 1002
        assert score.liquidity_component > 0  # Exact value depends on math.log10

        # trend_penalty = 200 * 100 / 100 = 200
        assert score.trend_penalty == 200

        # No toxicity penalty since symbol not blocked
        assert score.toxicity_penalty == 0

    def test_selected_symbols_have_ranks(self) -> None:
        """Selected symbols have correct 1-based ranks."""
        candidates = [
            SelectionCandidate(
                symbol="FIRSTUSDT",
                range_score=300,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="SECONDUSDT",
                range_score=200,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="THIRDUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        config = TopKConfigV1(k=2)
        result = select_topk_v1(candidates, config)

        first_score = next(s for s in result.scores if s.symbol == "FIRSTUSDT")
        second_score = next(s for s in result.scores if s.symbol == "SECONDUSDT")
        third_score = next(s for s in result.scores if s.symbol == "THIRDUSDT")

        assert first_score.selected is True
        assert first_score.rank == 1
        assert second_score.selected is True
        assert second_score.rank == 2
        assert third_score.selected is False
        assert third_score.rank == 0


class TestConfigVariants:
    """Tests for different configuration variants."""

    def test_custom_weights(self) -> None:
        """Custom weights affect scoring."""
        candidates = [
            SelectionCandidate(
                symbol="AAAUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("1000.0"),  # High liquidity
                net_return_bps=50,
                warmup_bars=20,
            ),
            SelectionCandidate(
                symbol="BBBUSDT",
                range_score=500,  # Much higher range
                spread_bps=10,
                thin_l1=Decimal("1.5"),  # Minimal liquidity (just above min)
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]

        # High liquidity weight → AAAUSDT wins
        config_liq = TopKConfigV1(k=1, w_range=10, w_liquidity=500)
        result_liq = select_topk_v1(candidates, config_liq)
        assert result_liq.selected[0] == "AAAUSDT"

        # High range weight, zero liquidity weight → BBBUSDT wins
        config_range = TopKConfigV1(k=1, w_range=200, w_liquidity=0)
        result_range = select_topk_v1(candidates, config_range)
        assert result_range.selected[0] == "BBBUSDT"

    def test_config_to_dict(self) -> None:
        """Config serializes correctly."""
        config = TopKConfigV1(
            k=5,
            spread_max_bps=30,
            thin_l1_min=Decimal("2.0"),
        )
        d = config.to_dict()

        assert d["k"] == 5
        assert d["spread_max_bps"] == 30
        assert d["thin_l1_min"] == "2.0"

    def test_result_to_dict(self) -> None:
        """Result serializes correctly."""
        candidates = [
            SelectionCandidate(
                symbol="TESTUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("10.0"),
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        result = select_topk_v1(candidates)
        d = result.to_dict()

        assert "selected" in d
        assert "scores" in d
        assert "k" in d
        assert "total_candidates" in d
        assert "gate_excluded" in d


class TestIlog10Determinism:
    """Tests for _ilog10 function (integer log10 via digit counting)."""

    def test_ilog10_basic_values(self) -> None:
        """Basic ilog10 values match floor(log10(x))."""
        # floor(log10(1)) = 0
        assert _ilog10(1) == 0
        # floor(log10(9)) = 0
        assert _ilog10(9) == 0
        # floor(log10(10)) = 1
        assert _ilog10(10) == 1
        # floor(log10(99)) = 1
        assert _ilog10(99) == 1
        # floor(log10(100)) = 2
        assert _ilog10(100) == 2
        # floor(log10(999)) = 2
        assert _ilog10(999) == 2
        # floor(log10(1000)) = 3
        assert _ilog10(1000) == 3

    def test_ilog10_edge_cases(self) -> None:
        """Edge cases return 0."""
        assert _ilog10(0) == 0
        assert _ilog10(-1) == 0
        assert _ilog10(-100) == 0

    def test_ilog10_large_values(self) -> None:
        """Large values work correctly."""
        # floor(log10(1_000_000)) = 6
        assert _ilog10(1_000_000) == 6
        # floor(log10(10_000_000_000)) = 10
        assert _ilog10(10_000_000_000) == 10

    def test_ilog10_deterministic_across_calls(self) -> None:
        """Multiple calls return identical results."""
        for x in [1, 10, 100, 1000, 12345, 999999]:
            result1 = _ilog10(x)
            result2 = _ilog10(x)
            assert result1 == result2, f"Non-deterministic for x={x}"

    def test_liquidity_component_determinism(self) -> None:
        """Liquidity component is deterministic for same thin_l1."""
        # Test that same thin_l1 always produces same liquidity_component
        candidates = [
            SelectionCandidate(
                symbol="TESTUSDT",
                range_score=100,
                spread_bps=10,
                thin_l1=Decimal("100.5"),  # Will be truncated to int
                net_return_bps=50,
                warmup_bars=20,
            ),
        ]
        config = TopKConfigV1(k=1)

        # Run multiple times
        results = [select_topk_v1(candidates, config) for _ in range(5)]

        # All liquidity components must be identical
        liq_components = [r.scores[0].liquidity_component for r in results]
        assert len(set(liq_components)) == 1, f"Non-deterministic: {liq_components}"

    def test_liquidity_boundary_values(self) -> None:
        """Liquidity at decade boundaries behaves predictably."""
        # thin_l1=9 → int=10 → ilog10(10)=1
        # thin_l1=10 → int=11 → ilog10(11)=1
        # thin_l1=99 → int=100 → ilog10(100)=2
        candidates_9 = [
            SelectionCandidate(
                symbol="A",
                range_score=0,
                spread_bps=10,
                thin_l1=Decimal("9"),
                net_return_bps=0,
                warmup_bars=20,
            ),
        ]
        candidates_10 = [
            SelectionCandidate(
                symbol="A",
                range_score=0,
                spread_bps=10,
                thin_l1=Decimal("10"),
                net_return_bps=0,
                warmup_bars=20,
            ),
        ]
        candidates_99 = [
            SelectionCandidate(
                symbol="A",
                range_score=0,
                spread_bps=10,
                thin_l1=Decimal("99"),
                net_return_bps=0,
                warmup_bars=20,
            ),
        ]

        config = TopKConfigV1(k=1)
        liq_9 = select_topk_v1(candidates_9, config).scores[0].liquidity_component
        liq_10 = select_topk_v1(candidates_10, config).scores[0].liquidity_component
        liq_99 = select_topk_v1(candidates_99, config).scores[0].liquidity_component

        # All should be predictable integer values
        # thin_l1=9: ilog10(10)=1 → liq_raw=1000 → component=1000*50/100=500
        # thin_l1=10: ilog10(11)=1 → liq_raw=1000 → component=500
        # thin_l1=99: ilog10(100)=2 → liq_raw=2000 → component=1000
        assert liq_9 == 500, f"Expected 500 for thin_l1=9, got {liq_9}"
        assert liq_10 == 500, f"Expected 500 for thin_l1=10, got {liq_10}"
        assert liq_99 == 1000, f"Expected 1000 for thin_l1=99, got {liq_99}"
