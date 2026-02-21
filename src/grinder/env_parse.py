"""Unified environment variable parsing (P2 Triage PR3).

Single source of truth for parsing env-gated booleans, integers, CSV lists,
and enum values.  Every flag across the repo should go through these helpers
so that 0/1, true/false, yes/no, on/off behave identically everywhere.

Design decisions:
- Safe-by-default: unset / empty / whitespace → default value, never "on".
- strict=True (default): unknown values raise ``ConfigError``.
- strict=False: unknown values → log warning + return default.
- Truthy / falsey sets are frozen; adding values requires a code change.

SSOT: this module.  Callers should *not* define their own truthy/falsey sets.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ── Canonical truthy / falsey sets ────────────────────────────────────────
TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})
FALSEY: frozenset[str] = frozenset({"0", "false", "no", "off", ""})


class ConfigError(Exception):
    """Raised when an environment variable has an invalid value (strict mode)."""


# ── Public API ────────────────────────────────────────────────────────────


def parse_bool(
    name: str,
    default: bool = False,
    *,
    strict: bool = True,
) -> bool:
    """Parse a boolean environment variable.

    Recognised truthy values:  ``1  true  yes  on``  (case-insensitive, stripped).
    Recognised falsey values:  ``0  false  no  off  ""``  (case-insensitive, stripped).

    Args:
        name: Environment variable name.
        default: Value when the variable is unset.
        strict: If *True*, unknown values raise :class:`ConfigError`.
                If *False*, unknown values log a warning and return *default*.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in TRUTHY:
        return True
    if v in FALSEY:
        return False
    if strict:
        raise ConfigError(f"invalid boolean value for {name}: {raw!r}")
    logger.warning("Invalid boolean value for %s: %r, using default %s", name, raw, default)
    return default


def parse_int(
    name: str,
    default: int | None = None,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
    strict: bool = True,
) -> int | None:
    """Parse an integer environment variable.

    Args:
        name: Environment variable name.
        default: Value when unset or (in non-strict mode) unparseable.
        min_value: Optional lower bound (inclusive).
        max_value: Optional upper bound (inclusive).
        strict: If *True*, non-integer values raise :class:`ConfigError`.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = raw.strip()
    if v == "":
        return default
    try:
        result = int(v)
    except ValueError:
        if strict:
            raise ConfigError(f"invalid integer value for {name}: {raw!r}") from None
        logger.warning("Invalid integer value for %s: %r, using default %s", name, raw, default)
        return default
    if min_value is not None and result < min_value:
        if strict:
            raise ConfigError(f"{name}={result} is below minimum {min_value}")
        logger.warning("%s=%d is below minimum %d, clamping", name, result, min_value)
        return min_value
    if max_value is not None and result > max_value:
        if strict:
            raise ConfigError(f"{name}={result} is above maximum {max_value}")
        logger.warning("%s=%d is above maximum %d, clamping", name, result, max_value)
        return max_value
    return result


def parse_csv(name: str) -> list[str]:
    """Parse a comma-separated environment variable.

    Trims whitespace from each element and drops empty strings.

    Returns an empty list when the variable is unset or blank.
    """
    raw = os.environ.get(name)
    if raw is None:
        return []
    return [item for item in (s.strip() for s in raw.split(",")) if item]


def parse_enum(
    name: str,
    allowed: set[str],
    default: str | None = None,
    *,
    casefold: bool = True,
    strict: bool = True,
) -> str | None:
    """Parse an enum-like environment variable.

    Args:
        name: Environment variable name.
        allowed: Set of valid values (canonical casing).
        default: Value when unset or empty.
        casefold: If *True*, comparison is case-insensitive and the
                  canonical (as-in-*allowed*) form is returned.
        strict: If *True*, invalid values raise :class:`ConfigError`.
                If *False*, invalid values log a warning and return *default*.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = raw.strip()
    if v == "":
        return default
    # Build lookup once (canonical form preserved)
    if casefold:
        lookup = {a.lower(): a for a in allowed}
        match = lookup.get(v.lower())
    else:
        match = v if v in allowed else None
    if match is not None:
        return match
    if strict:
        raise ConfigError(f"invalid value for {name}: {raw!r} (allowed: {sorted(allowed)})")
    logger.warning(
        "Invalid value for %s: %r (allowed: %s), using default %s",
        name,
        raw,
        sorted(allowed),
        default,
    )
    return default
