"""ML models for parameter calibration.

M8-01: MlSignalSnapshot contract for regime-based signal injection.

See: docs/ROADMAP.md M8-ML_POLICY milestone
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Valid regime keys
VALID_REGIMES = frozenset({"LOW", "MID", "HIGH"})

# Probability sum must equal this (10000 bps = 100%)
PROBS_SUM_BPS = 10000


class MlSignalValidationError(ValueError):
    """Raised when MlSignalSnapshot validation fails."""

    pass


@dataclass(frozen=True)
class MlSignalSnapshot:
    """ML signal snapshot for policy feature injection.

    M8-01 contract (aligned with M8-00 spec):
    - regime_probs_bps: Probability distribution over regimes in basis points.
      Keys must be {"LOW", "MID", "HIGH"}, values must sum to 10000.
    - predicted_regime: The regime with highest probability ("LOW"|"MID"|"HIGH").
    - spacing_multiplier_x1000: Spacing adjustment factor (1000 = 1.0x).
    - ts_ms: Timestamp when signal was generated (milliseconds).
    - symbol: Trading symbol this signal applies to.

    All values are integers to ensure deterministic JSON serialization.
    """

    ts_ms: int
    symbol: str
    regime_probs_bps: dict[str, int]
    predicted_regime: str
    spacing_multiplier_x1000: int

    def __post_init__(self) -> None:
        """Validate the signal on construction."""
        self._validate()

    def _validate(self) -> None:
        """Validate signal invariants.

        Raises:
            MlSignalValidationError: If any validation fails.
        """
        # Check regime_probs_bps keys
        keys = set(self.regime_probs_bps.keys())
        if keys != VALID_REGIMES:
            raise MlSignalValidationError(
                f"regime_probs_bps keys must be {VALID_REGIMES}, got {keys}"
            )

        # Check all values are non-negative integers
        for key, value in self.regime_probs_bps.items():
            if not isinstance(value, int):
                raise MlSignalValidationError(
                    f"regime_probs_bps[{key!r}] must be int, got {type(value).__name__}"
                )
            if value < 0:
                raise MlSignalValidationError(
                    f"regime_probs_bps[{key!r}] must be >= 0, got {value}"
                )

        # Check sum equals 10000
        total = sum(self.regime_probs_bps.values())
        if total != PROBS_SUM_BPS:
            raise MlSignalValidationError(
                f"regime_probs_bps must sum to {PROBS_SUM_BPS}, got {total}"
            )

        # Check predicted_regime is valid
        if self.predicted_regime not in VALID_REGIMES:
            raise MlSignalValidationError(
                f"predicted_regime must be in {VALID_REGIMES}, got {self.predicted_regime!r}"
            )

        # Check spacing_multiplier_x1000 is positive
        if self.spacing_multiplier_x1000 <= 0:
            raise MlSignalValidationError(
                f"spacing_multiplier_x1000 must be > 0, got {self.spacing_multiplier_x1000}"
            )

        # Check ts_ms is non-negative
        if self.ts_ms < 0:
            raise MlSignalValidationError(f"ts_ms must be >= 0, got {self.ts_ms}")

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts_ms": self.ts_ms,
            "symbol": self.symbol,
            "regime_probs_bps": dict(self.regime_probs_bps),
            "predicted_regime": self.predicted_regime,
            "spacing_multiplier_x1000": self.spacing_multiplier_x1000,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MlSignalSnapshot:
        """Create from dict.

        Args:
            d: Dict with signal fields.

        Returns:
            MlSignalSnapshot instance.

        Raises:
            MlSignalValidationError: If validation fails.
        """
        return cls(
            ts_ms=d["ts_ms"],
            symbol=d["symbol"],
            regime_probs_bps=d["regime_probs_bps"],
            predicted_regime=d["predicted_regime"],
            spacing_multiplier_x1000=d["spacing_multiplier_x1000"],
        )

    def to_json(self) -> str:
        """Serialize to deterministic JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> MlSignalSnapshot:
        """Deserialize from JSON string.

        Args:
            s: JSON string.

        Returns:
            MlSignalSnapshot instance.
        """
        return cls.from_dict(json.loads(s))

    def to_policy_features(self) -> dict[str, int]:
        """Convert to integer features for policy injection.

        Returns dict with keys:
        - ml_regime_prob_low_bps: int (0-10000)
        - ml_regime_prob_mid_bps: int (0-10000)
        - ml_regime_prob_high_bps: int (0-10000)
        - ml_spacing_multiplier_x1000: int (1000 = 1.0x)
        - ml_predicted_regime_ord: int (0=LOW, 1=MID, 2=HIGH)

        All values are integers to preserve determinism.
        """
        regime_ord_map = {"LOW": 0, "MID": 1, "HIGH": 2}
        return {
            "ml_regime_prob_low_bps": self.regime_probs_bps["LOW"],
            "ml_regime_prob_mid_bps": self.regime_probs_bps["MID"],
            "ml_regime_prob_high_bps": self.regime_probs_bps["HIGH"],
            "ml_spacing_multiplier_x1000": self.spacing_multiplier_x1000,
            "ml_predicted_regime_ord": regime_ord_map[self.predicted_regime],
        }


__all__ = [
    "PROBS_SUM_BPS",
    "VALID_REGIMES",
    "MlSignalSnapshot",
    "MlSignalValidationError",
]
