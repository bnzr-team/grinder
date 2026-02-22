"""Fill probability gate for SOR-level order blocking (Track C, PR-C5).

Pure function: ``check_fill_prob(model, features, threshold, enforce)``
returns ``FillProbResult`` with verdict ALLOW / BLOCK / SHADOW.

Behaviour:
- **Fail-open**: ``model=None`` → ALLOW (prob_bps=0, never blocks).
- **Shadow** (enforce=False): prediction computed, never blocks.
- **Enforce** (enforce=True): prob >= threshold → ALLOW, else BLOCK.

Gate applies only to PLACE/AMEND (risk-increasing) decisions.
CANCEL and NOOP always pass through without checking.

SSOT: this module.  ADR-071 in docs/DECISIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.ml.fill_model_v0 import FillModelFeaturesV0, FillModelV0


class FillProbVerdict(StrEnum):
    """Verdict from fill probability gate."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    SHADOW = "SHADOW"


@dataclass(frozen=True)
class FillProbResult:
    """Result of a fill probability gate check.

    Attributes:
        verdict: Gate decision (ALLOW / BLOCK / SHADOW).
        prob_bps: Predicted fill probability in bps (0..10000), 0 if model unavailable.
        threshold_bps: Configured minimum threshold in bps.
        enforce: Whether enforcement was active for this check.
    """

    verdict: FillProbVerdict
    prob_bps: int
    threshold_bps: int
    enforce: bool


def check_fill_prob(
    *,
    model: FillModelV0 | None,
    features: FillModelFeaturesV0,
    threshold_bps: int = 2500,
    enforce: bool = False,
) -> FillProbResult:
    """Check fill probability gate.

    Args:
        model: Loaded FillModelV0, or None if unavailable.
        features: Entry-side features for prediction.
        threshold_bps: Minimum fill probability in bps (0..10000).
        enforce: If True, block orders below threshold.

    Returns:
        FillProbResult with verdict and prediction details.
    """
    # Fail-open: no model → always allow
    if model is None:
        return FillProbResult(
            verdict=FillProbVerdict.ALLOW,
            prob_bps=0,
            threshold_bps=threshold_bps,
            enforce=enforce,
        )

    prob_bps = model.predict(features)

    # Shadow mode: predict but never block
    if not enforce:
        return FillProbResult(
            verdict=FillProbVerdict.SHADOW,
            prob_bps=prob_bps,
            threshold_bps=threshold_bps,
            enforce=False,
        )

    # Enforce mode: block if below threshold
    if prob_bps >= threshold_bps:
        return FillProbResult(
            verdict=FillProbVerdict.ALLOW,
            prob_bps=prob_bps,
            threshold_bps=threshold_bps,
            enforce=True,
        )

    return FillProbResult(
        verdict=FillProbVerdict.BLOCK,
        prob_bps=prob_bps,
        threshold_bps=threshold_bps,
        enforce=True,
    )
