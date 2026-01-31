"""Types for Adaptive Controller v0.

This module defines the contracts for controller decisions.
All types use integer basis points for deterministic sorting.

See: docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md, ADR-011
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ControllerMode(Enum):
    """Controller operation modes.

    Priority order (highest to lowest): PAUSE > WIDEN > TIGHTEN > BASE
    """

    BASE = "BASE"  # Normal operation, no adjustment
    WIDEN = "WIDEN"  # High volatility, widen grid spacing
    TIGHTEN = "TIGHTEN"  # Low volatility, tighten grid spacing
    PAUSE = "PAUSE"  # Wide spread or dangerous conditions, no new orders


class ControllerReason(Enum):
    """Reason codes for controller decisions."""

    NORMAL = "NORMAL"  # Metrics within normal thresholds
    LOW_VOL = "LOW_VOL"  # Volatility below threshold
    HIGH_VOL = "HIGH_VOL"  # Volatility above threshold
    WIDE_SPREAD = "WIDE_SPREAD"  # Spread above threshold


@dataclass
class ControllerDecision:
    """Output of controller evaluation.

    Attributes:
        mode: Operating mode for this snapshot
        reason: Primary reason for the decision
        spacing_multiplier: Multiplier to apply to base spacing
        vol_bps: Current volatility in integer basis points
        spread_bps_max: Maximum spread in window (integer bps)
        window_size: Number of events in current window
    """

    mode: ControllerMode
    reason: ControllerReason
    spacing_multiplier: float
    vol_bps: int  # Integer bps for determinism
    spread_bps_max: int  # Integer bps for determinism
    window_size: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "mode": self.mode.value,
            "reason": self.reason.value,
            "spacing_multiplier": self.spacing_multiplier,
            "vol_bps": self.vol_bps,
            "spread_bps_max": self.spread_bps_max,
            "window_size": self.window_size,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ControllerDecision:
        """Create from dict."""
        return cls(
            mode=ControllerMode(d["mode"]),
            reason=ControllerReason(d["reason"]),
            spacing_multiplier=d["spacing_multiplier"],
            vol_bps=d["vol_bps"],
            spread_bps_max=d["spread_bps_max"],
            window_size=d["window_size"],
        )
