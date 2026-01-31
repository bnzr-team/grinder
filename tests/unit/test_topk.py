"""Unit tests for Top-K prefilter selection.

These tests verify the deterministic behavior of TopKSelector.
See: ADR-010 for scoring and tie-breaking rules.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from grinder.paper import PaperEngine
from grinder.prefilter import SymbolScore, TopKResult, TopKSelector


class TestTopKSelector:
    """Tests for TopKSelector class."""

    def test_select_empty_returns_empty(self) -> None:
        """Selecting with no recorded prices returns empty list."""
        selector = TopKSelector(k=3)
        result = selector.select()
        assert result.selected == []
        assert result.k == 3
        assert result.scores == []

    def test_select_single_symbol(self) -> None:
        """Single symbol is always selected regardless of K."""
        selector = TopKSelector(k=3)
        selector.record_price(1000, "BTCUSDT", Decimal("50000"))
        selector.record_price(2000, "BTCUSDT", Decimal("50100"))

        result = selector.select()
        assert result.selected == ["BTCUSDT"]
        assert result.k == 3
        assert len(result.scores) == 1
        assert result.scores[0].symbol == "BTCUSDT"
        assert result.scores[0].score_bps > 0  # Has volatility

    def test_select_k_greater_than_symbols(self) -> None:
        """When K > number of symbols, all symbols are selected."""
        selector = TopKSelector(k=5)
        selector.record_price(1000, "AAA", Decimal("1.00"))
        selector.record_price(2000, "AAA", Decimal("1.10"))
        selector.record_price(1000, "BBB", Decimal("2.00"))
        selector.record_price(2000, "BBB", Decimal("2.05"))

        result = selector.select()
        assert len(result.selected) == 2
        assert set(result.selected) == {"AAA", "BBB"}
        # AAA should be first (higher volatility: 10% vs 2.5%)
        assert result.selected[0] == "AAA"

    def test_select_top_k_by_volatility(self) -> None:
        """Top-K selection is based on volatility score (sum of abs returns)."""
        selector = TopKSelector(k=2)

        # Symbol A: high volatility (10% moves)
        selector.record_price(1000, "A", Decimal("100"))
        selector.record_price(2000, "A", Decimal("110"))
        selector.record_price(3000, "A", Decimal("100"))

        # Symbol B: low volatility (0.1% moves)
        selector.record_price(1000, "B", Decimal("100"))
        selector.record_price(2000, "B", Decimal("100.1"))
        selector.record_price(3000, "B", Decimal("100.0"))

        # Symbol C: medium volatility (1% moves)
        selector.record_price(1000, "C", Decimal("100"))
        selector.record_price(2000, "C", Decimal("101"))
        selector.record_price(3000, "C", Decimal("100"))

        result = selector.select()
        assert result.selected == ["A", "C"]  # Top 2 by volatility
        assert result.scores[0].symbol == "A"
        assert result.scores[1].symbol == "C"
        assert result.scores[2].symbol == "B"

    def test_tie_breaker_lexicographic(self) -> None:
        """When scores tie, symbols are ordered lexicographically."""
        selector = TopKSelector(k=2)

        # All symbols have same volatility (no price change)
        selector.record_price(1000, "CCC", Decimal("100"))
        selector.record_price(2000, "CCC", Decimal("100"))
        selector.record_price(1000, "AAA", Decimal("100"))
        selector.record_price(2000, "AAA", Decimal("100"))
        selector.record_price(1000, "BBB", Decimal("100"))
        selector.record_price(2000, "BBB", Decimal("100"))

        result = selector.select()
        # All have score 0, so lexicographic order: AAA, BBB, CCC
        assert result.selected == ["AAA", "BBB"]

    def test_deterministic_ordering(self) -> None:
        """Selection order is deterministic across multiple runs."""
        results = []
        for _ in range(5):
            selector = TopKSelector(k=2)
            selector.record_price(1000, "X", Decimal("100"))
            selector.record_price(2000, "X", Decimal("105"))
            selector.record_price(1000, "Y", Decimal("100"))
            selector.record_price(2000, "Y", Decimal("103"))
            selector.record_price(1000, "Z", Decimal("100"))
            selector.record_price(2000, "Z", Decimal("101"))
            results.append(selector.select().selected)

        # All runs should produce identical results
        assert all(r == results[0] for r in results)
        assert results[0] == ["X", "Y"]  # X has highest volatility, Y second

    def test_window_size_limits_history(self) -> None:
        """Window size limits the number of prices stored per symbol."""
        selector = TopKSelector(k=1, window_size=3)

        # Record more prices than window allows
        for i in range(10):
            selector.record_price(i * 1000, "A", Decimal(str(100 + i)))

        # Only last 3 prices should be kept
        assert len(selector._price_history["A"]) == 3

    def test_reset_clears_state(self) -> None:
        """Reset clears all recorded prices."""
        selector = TopKSelector(k=2)
        selector.record_price(1000, "A", Decimal("100"))
        selector.record_price(2000, "A", Decimal("110"))
        selector.record_price(1000, "B", Decimal("200"))

        selector.reset()

        result = selector.select()
        assert result.selected == []
        assert result.scores == []

    def test_get_all_symbols(self) -> None:
        """get_all_symbols returns all recorded symbols."""
        selector = TopKSelector(k=2)
        selector.record_price(1000, "A", Decimal("100"))
        selector.record_price(1000, "B", Decimal("200"))
        selector.record_price(1000, "C", Decimal("300"))

        symbols = selector.get_all_symbols()
        assert set(symbols) == {"A", "B", "C"}


class TestSymbolScore:
    """Tests for SymbolScore dataclass."""

    def test_to_dict(self) -> None:
        """to_dict returns correct structure."""
        score = SymbolScore(symbol="BTCUSDT", score_bps=1234, event_count=5)
        d = score.to_dict()
        assert d == {
            "symbol": "BTCUSDT",
            "score_bps": 1234,
            "event_count": 5,
        }


class TestTopKResult:
    """Tests for TopKResult dataclass."""

    def test_to_dict(self) -> None:
        """to_dict returns correct structure."""
        result = TopKResult(
            selected=["A", "B"],
            scores=[
                SymbolScore(symbol="A", score_bps=1000, event_count=3),
                SymbolScore(symbol="B", score_bps=500, event_count=3),
            ],
            k=2,
        )
        d = result.to_dict()
        assert d["selected"] == ["A", "B"]
        assert d["k"] == 2
        assert len(d["scores"]) == 2
        assert d["scores"][0]["symbol"] == "A"
        assert d["scores"][0]["score_bps"] == 1000


class TestTopKIntegration:
    """Integration tests for Top-K with paper trading."""

    def test_multisymbol_fixture_selects_top3(self) -> None:
        """sample_day_multisymbol fixture selects correct top 3 symbols."""
        engine = PaperEngine()
        result = engine.run(Path("tests/fixtures/sample_day_multisymbol"))

        # Verify Top-K selection
        assert result.topk_k == 3
        assert result.topk_selected_symbols == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]

        # Verify DDDUSDT and EEEUSDT were filtered out (not in outputs)
        output_symbols = {o.symbol for o in result.outputs}
        assert "DDDUSDT" not in output_symbols
        assert "EEEUSDT" not in output_symbols

    def test_existing_fixtures_all_selected(self) -> None:
        """Existing fixtures with ≤3 symbols have all symbols selected."""
        # sample_day has 2 symbols
        engine = PaperEngine()
        result = engine.run(Path("tests/fixtures/sample_day"))
        assert set(result.topk_selected_symbols) == {"BTCUSDT", "ETHUSDT"}

        # sample_day_allowed has 2 symbols
        engine2 = PaperEngine()
        result2 = engine2.run(Path("tests/fixtures/sample_day_allowed"))
        assert set(result2.topk_selected_symbols) == {"TESTUSDT", "TEST2USDT"}

        # sample_day_toxic has 1 symbol
        engine3 = PaperEngine()
        result3 = engine3.run(Path("tests/fixtures/sample_day_toxic"))
        assert result3.topk_selected_symbols == ["TESTUSDT"]

    def test_canonical_digests_preserved(self) -> None:
        """Existing canonical digests are preserved with Top-K integration."""
        expected = {
            "sample_day": "66b29a4e92192f8f",
            "sample_day_allowed": "ec223bce78d7926f",
            "sample_day_toxic": "66d57776b7be4797",
        }

        for fixture, expected_digest in expected.items():
            engine = PaperEngine()
            result = engine.run(Path(f"tests/fixtures/{fixture}"))
            assert result.digest == expected_digest, (
                f"Digest mismatch for {fixture}: expected {expected_digest}, got {result.digest}"
            )
