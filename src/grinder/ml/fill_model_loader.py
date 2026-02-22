"""FillModelV0 runtime loader + online feature extraction (Track C, PR-C4a).

Provides:
- ``load_fill_model_v0(model_dir)``: load + SHA256 verify, return model or None.
- ``extract_online_features()``: build ``FillModelFeaturesV0`` from live
  pipeline fields (entry-side only, no leakage).
- Module-level shadow metrics state (gauge + counter) for Prometheus export.

Design:
- **Fail-open**: any load error → None + WARN log.  Caller treats None as
  "model unavailable" and skips prediction (metrics stay at default 0).
- **No caching magic**: caller loads once at startup and holds the reference.
  No mtime polling, no hot-reload — deterministic, testable.
- **Shadow-only**: this module never touches SOR / decision logic.

SSOT: this module.  ADR-069 in docs/DECISIONS.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from grinder.ml.fill_model_v0 import (
    FillModelFeaturesV0,
    FillModelV0,
    quantize_fill_count,
    quantize_holding_ms,
    quantize_notional,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_fill_model_v0(model_dir: str | Path) -> FillModelV0 | None:
    """Load FillModelV0 from *model_dir* with SHA256 integrity check.

    Returns ``None`` on **any** failure (missing dir, bad JSON, SHA256
    mismatch, etc.) and logs a warning.  Caller should treat ``None`` as
    "model unavailable" and proceed without predictions (fail-open).

    Args:
        model_dir: Directory containing ``model.json`` + ``manifest.json``.

    Returns:
        Loaded model, or ``None`` on error.
    """
    path = Path(model_dir)
    if not path.is_dir():
        logger.warning(
            "FILL_MODEL_LOAD_FAILED dir_missing=true path=%s",
            path,
        )
        return None

    try:
        model = FillModelV0.load(path)
    except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "FILL_MODEL_LOAD_FAILED reason=%s path=%s",
            exc,
            path,
        )
        return None

    logger.info(
        "FILL_MODEL_LOADED n_bins=%d global_prior_bps=%d n_train_rows=%d path=%s",
        len(model.bins),
        model.global_prior_bps,
        model.n_train_rows,
        path,
    )
    return model


# ---------------------------------------------------------------------------
# Online feature extraction (no leakage)
# ---------------------------------------------------------------------------


def extract_online_features(
    *,
    direction: str,
    notional: Any,
    entry_fill_count: int = 1,
    holding_ms: int = 0,
) -> FillModelFeaturesV0:
    """Build ``FillModelFeaturesV0`` from live pipeline fields.

    Entry-side only — does **not** use exit price, realized PnL, or any
    post-fill information.  Safe for online (pre-decision) use.

    Args:
        direction: ``"long"`` or ``"short"``.
        notional: Entry price * qty (Decimal or float-like).
        entry_fill_count: Number of entry fills so far (default 1 =
            conservative; use actual count if available).
        holding_ms: Milliseconds since planned entry (``0`` if at
            planning time — produces bucket 0, the most conservative).

    Returns:
        ``FillModelFeaturesV0`` ready for ``model.predict()``.
    """
    return FillModelFeaturesV0(
        direction=direction,
        notional_bucket=quantize_notional(notional),
        entry_fill_count=quantize_fill_count(entry_fill_count),
        holding_ms_bucket=quantize_holding_ms(holding_ms),
    )


# ---------------------------------------------------------------------------
# Shadow metrics state
# ---------------------------------------------------------------------------

# Module-level state (same pattern as metrics_builder.py _consec_loss_state)
_fill_model_state: list[tuple[int, int, bool]] = [
    (0, 0, False)
]  # (last_prob_bps, calc_total, model_loaded)


def set_fill_model_metrics(prob_bps: int, calc_total: int, model_loaded: bool) -> None:
    """Update fill model shadow metrics (called after each prediction)."""
    _fill_model_state[0] = (prob_bps, calc_total, model_loaded)


def get_fill_model_metrics() -> tuple[int, int, bool]:
    """Get fill model shadow metrics (prob_bps, calc_total, model_loaded)."""
    return _fill_model_state[0]


def reset_fill_model_metrics() -> None:
    """Reset fill model shadow metrics (for testing)."""
    _fill_model_state[0] = (0, 0, False)


def fill_model_metrics_to_prometheus_lines() -> list[str]:
    """Export fill model shadow metrics in Prometheus text format.

    Always emitted (even when model is unavailable) — gauges default to 0.
    """
    prob_bps, _calc_total, model_loaded = _fill_model_state[0]
    return [
        "# HELP grinder_ml_fill_prob_bps_last Last computed fill probability (bps 0..10000)",
        "# TYPE grinder_ml_fill_prob_bps_last gauge",
        f"grinder_ml_fill_prob_bps_last {prob_bps}",
        "# HELP grinder_ml_fill_model_loaded Whether fill probability model is loaded (1=yes, 0=no)",
        "# TYPE grinder_ml_fill_model_loaded gauge",
        f"grinder_ml_fill_model_loaded {1 if model_loaded else 0}",
    ]
