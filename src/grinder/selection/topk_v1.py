"""Top-K v1 symbol selection (L1-only, deterministic).

Selects K symbols using scoring formula:
    score = range_score + liquidity_score - toxicity_penalty - trend_penalty

Hard gates (exclusion before scoring):
    - toxicity_blocked → excluded (if tox_blocked_exclude=True)
    - spread_bps > spread_max_bps → excluded
    - thin_l1 < thin_l1_min → excluded
    - warmup_bars < warmup_min → excluded

Tie-breaker: (-score, symbol) for deterministic ordering.

See: docs/17_ADAPTIVE_SMART_GRID_V1.md, ADR-023
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TopKConfigV1:
    """Configuration for Top-K v1 selection.

    All thresholds in basis points (integer) or raw units for determinism.

    Attributes:
        # K parameters
        k: Number of symbols to select (fixed value for determinism)

        # Hard gate thresholds
        spread_max_bps: Max spread to be eligible (default 50 bps)
        thin_l1_min: Min thin-side depth to be eligible (default 1.0)
        warmup_min: Min completed bars for reliable features (default 15)
        tox_blocked_exclude: Exclude symbols with toxicity gate blocked (default True)

        # Scoring weights (scaled by 100 for integer math, 100 = 1.0)
        w_range: Weight for range_score (default 100 = 1.0)
        w_liquidity: Weight for liquidity_score (default 50 = 0.5)
        w_toxicity: Weight for toxicity_penalty (default 200 = 2.0)
        w_trend: Weight for trend_penalty (default 100 = 1.0)

        # Liquidity score scaling
        liq_scale: Multiplier for log10(thin_l1 + 1) (default 1000)
    """

    # K parameters
    k: int = 3

    # Hard gate thresholds
    spread_max_bps: int = 50
    thin_l1_min: Decimal = field(default_factory=lambda: Decimal("1.0"))
    warmup_min: int = 15
    tox_blocked_exclude: bool = True

    # Scoring weights (scaled by 100)
    w_range: int = 100  # 1.0
    w_liquidity: int = 50  # 0.5
    w_toxicity: int = 200  # 2.0
    w_trend: int = 100  # 1.0

    # Liquidity score scaling
    liq_scale: int = 1000

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "k": self.k,
            "spread_max_bps": self.spread_max_bps,
            "thin_l1_min": str(self.thin_l1_min),
            "warmup_min": self.warmup_min,
            "tox_blocked_exclude": self.tox_blocked_exclude,
            "w_range": self.w_range,
            "w_liquidity": self.w_liquidity,
            "w_toxicity": self.w_toxicity,
            "w_trend": self.w_trend,
            "liq_scale": self.liq_scale,
        }


@dataclass(frozen=True)
class SelectionCandidate:
    """Input candidate for Top-K selection.

    Attributes:
        symbol: Trading symbol
        range_score: From FeatureSnapshot (higher = more choppy = better)
        spread_bps: Bid-ask spread in bps
        thin_l1: Thin-side depth (min of bid/ask qty)
        net_return_bps: Net return over horizon (abs value = trend strength)
        warmup_bars: Number of completed bars
        toxicity_blocked: Whether toxicity gate blocked this symbol
    """

    symbol: str
    range_score: int
    spread_bps: int
    thin_l1: Decimal
    net_return_bps: int
    warmup_bars: int
    toxicity_blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "symbol": self.symbol,
            "range_score": self.range_score,
            "spread_bps": self.spread_bps,
            "thin_l1": str(self.thin_l1),
            "net_return_bps": self.net_return_bps,
            "warmup_bars": self.warmup_bars,
            "toxicity_blocked": self.toxicity_blocked,
        }


@dataclass(frozen=True)
class SymbolScoreV1:
    """Score and components for a single symbol.

    All scores are integer bps for determinism.

    Attributes:
        symbol: Trading symbol
        score: Final composite score (higher = better)
        range_component: Weighted range_score contribution
        liquidity_component: Weighted liquidity contribution
        toxicity_penalty: Weighted toxicity penalty (subtracted)
        trend_penalty: Weighted trend penalty (subtracted)
        gates_failed: List of gate names that failed (empty if passed all)
        selected: Whether this symbol is in Top-K
        rank: 1-based rank (0 if not selected or gate-blocked)
    """

    symbol: str
    score: int
    range_component: int
    liquidity_component: int
    toxicity_penalty: int
    trend_penalty: int
    gates_failed: tuple[str, ...]
    selected: bool
    rank: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "symbol": self.symbol,
            "score": self.score,
            "range_component": self.range_component,
            "liquidity_component": self.liquidity_component,
            "toxicity_penalty": self.toxicity_penalty,
            "trend_penalty": self.trend_penalty,
            "gates_failed": list(self.gates_failed),
            "selected": self.selected,
            "rank": self.rank,
        }


@dataclass
class SelectionResult:
    """Result of Top-K v1 selection.

    Attributes:
        selected: Ordered list of selected symbols (highest score first)
        scores: All symbol scores including components and gate failures
        k: The K value used
        total_candidates: Total input candidates
        gate_excluded: Number excluded by hard gates
    """

    selected: list[str]
    scores: list[SymbolScoreV1]
    k: int
    total_candidates: int
    gate_excluded: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "selected": self.selected,
            "scores": [s.to_dict() for s in self.scores],
            "k": self.k,
            "total_candidates": self.total_candidates,
            "gate_excluded": self.gate_excluded,
        }


def _check_gates(
    candidate: SelectionCandidate,
    config: TopKConfigV1,
) -> list[str]:
    """Check hard gates and return list of failed gate names.

    Args:
        candidate: The candidate to check
        config: Selection configuration

    Returns:
        List of failed gate names (empty if all passed)
    """
    failed: list[str] = []

    # Gate 1: Toxicity blocked
    if config.tox_blocked_exclude and candidate.toxicity_blocked:
        failed.append("TOXICITY_BLOCKED")

    # Gate 2: Spread too wide
    if candidate.spread_bps > config.spread_max_bps:
        failed.append("SPREAD_TOO_WIDE")

    # Gate 3: Thin book
    if candidate.thin_l1 < config.thin_l1_min:
        failed.append("THIN_BOOK")

    # Gate 4: Warmup insufficient
    if candidate.warmup_bars < config.warmup_min:
        failed.append("WARMUP_INSUFFICIENT")

    return failed


def _compute_score(
    candidate: SelectionCandidate,
    config: TopKConfigV1,
) -> tuple[int, int, int, int, int]:
    """Compute score components for a candidate.

    Returns:
        Tuple of (total_score, range_component, liquidity_component,
                  toxicity_penalty, trend_penalty)
    """
    # Range component: range_score * w_range / 100
    # Higher range_score = more choppy = better for grid trading
    range_component = (candidate.range_score * config.w_range) // 100

    # Liquidity component: log10(thin_l1 + 1) * liq_scale * w_liquidity / 100
    # Using log10 to normalize across different order sizes
    # Add 1 to avoid log(0)
    thin_l1_float = float(candidate.thin_l1)
    liq_raw = int(math.log10(thin_l1_float + 1) * config.liq_scale)
    liquidity_component = (liq_raw * config.w_liquidity) // 100

    # Toxicity penalty: if blocked, apply w_toxicity * 100 (scaled penalty)
    # This is a fixed penalty, not based on degree
    toxicity_penalty = 0
    if candidate.toxicity_blocked:
        toxicity_penalty = config.w_toxicity * 100

    # Trend penalty: abs(net_return_bps) * w_trend / 100
    # Higher absolute net return = more trending = worse for grid
    trend_penalty = (abs(candidate.net_return_bps) * config.w_trend) // 100

    # Final score
    total_score = range_component + liquidity_component - toxicity_penalty - trend_penalty

    return (total_score, range_component, liquidity_component, toxicity_penalty, trend_penalty)


def select_topk_v1(
    candidates: list[SelectionCandidate],
    config: TopKConfigV1 | None = None,
) -> SelectionResult:
    """Select Top-K symbols using L1-only scoring.

    Scoring formula:
        score = (range_score * w_range + liquidity_score * w_liquidity
                 - toxicity_penalty * w_toxicity - trend_penalty * w_trend)

    Hard gates (exclusion before scoring):
        - toxicity_blocked → excluded (if tox_blocked_exclude=True)
        - spread_bps > spread_max_bps → excluded
        - thin_l1 < thin_l1_min → excluded
        - warmup_bars < warmup_min → excluded

    Tie-breaker: (-score, symbol) for deterministic ordering.

    Args:
        candidates: List of selection candidates with features
        config: Selection configuration (uses defaults if None)

    Returns:
        SelectionResult with selected symbols and all scores
    """
    if config is None:
        config = TopKConfigV1()

    # Process all candidates
    scores: list[SymbolScoreV1] = []
    eligible: list[tuple[int, str, SymbolScoreV1]] = []  # (score, symbol, full_score)
    gate_excluded = 0

    for candidate in candidates:
        # Check gates
        gates_failed = _check_gates(candidate, config)

        if gates_failed:
            # Gate blocked - score is 0, not eligible for selection
            gate_excluded += 1
            score_obj = SymbolScoreV1(
                symbol=candidate.symbol,
                score=0,
                range_component=0,
                liquidity_component=0,
                toxicity_penalty=0,
                trend_penalty=0,
                gates_failed=tuple(gates_failed),
                selected=False,
                rank=0,
            )
            scores.append(score_obj)
        else:
            # Gates passed - compute score
            total, range_c, liq_c, tox_p, trend_p = _compute_score(candidate, config)
            score_obj = SymbolScoreV1(
                symbol=candidate.symbol,
                score=total,
                range_component=range_c,
                liquidity_component=liq_c,
                toxicity_penalty=tox_p,
                trend_penalty=trend_p,
                gates_failed=(),
                selected=False,  # Will update later
                rank=0,  # Will update later
            )
            scores.append(score_obj)
            eligible.append((total, candidate.symbol, score_obj))

    # Sort eligible by (-score, symbol) for deterministic tie-breaking
    eligible.sort(key=lambda x: (-x[0], x[1]))

    # Select top K
    selected_symbols: list[str] = []
    selected_set: set[str] = set()

    for _, symbol, _ in eligible[: config.k]:
        selected_symbols.append(symbol)
        selected_set.add(symbol)

    # Update scores with selection status and rank
    final_scores: list[SymbolScoreV1] = []
    for score in scores:
        if score.symbol in selected_set:
            # Find rank (1-based)
            rank = selected_symbols.index(score.symbol) + 1
            # Create new frozen dataclass with updated values
            final_scores.append(
                SymbolScoreV1(
                    symbol=score.symbol,
                    score=score.score,
                    range_component=score.range_component,
                    liquidity_component=score.liquidity_component,
                    toxicity_penalty=score.toxicity_penalty,
                    trend_penalty=score.trend_penalty,
                    gates_failed=score.gates_failed,
                    selected=True,
                    rank=rank,
                )
            )
        else:
            final_scores.append(score)

    # Sort final scores for consistent output: selected first (by rank), then others (by symbol)
    final_scores.sort(key=lambda s: (0 if s.selected else 1, s.rank if s.selected else 0, s.symbol))

    return SelectionResult(
        selected=selected_symbols,
        scores=final_scores,
        k=config.k,
        total_candidates=len(candidates),
        gate_excluded=gate_excluded,
    )
