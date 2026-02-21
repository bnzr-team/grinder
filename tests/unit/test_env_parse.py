"""Tests for grinder.env_parse (P2 Triage PR3).

Covers:
- parse_bool: truthy/falsey matrix, unknown values (strict + non-strict),
  unset env, default parameter, whitespace, mixed case.
- parse_int: valid ints, invalid strings, min/max bounds, unset, empty.
- parse_csv: split, trim, drop empties, unset.
- parse_enum: valid values, casefold, unknown (strict + non-strict), unset.
- ConfigError: raised only in strict mode.
"""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest

from grinder.env_parse import ConfigError, parse_bool, parse_csv, parse_enum, parse_int

# ---------------------------------------------------------------------------
# parse_bool
# ---------------------------------------------------------------------------


class TestParseBoolTruthyMatrix:
    """REQ-001: All canonical truthy values return True."""

    @pytest.mark.parametrize(
        "raw",
        ["1", "true", "TRUE", "True", "yes", "YES", "Yes", "on", "ON", "On"],
    )
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("TEST_BOOL", raw)
        assert parse_bool("TEST_BOOL") is True

    @pytest.mark.parametrize("raw", [" 1 ", " true ", " ON "])
    def test_truthy_stripped(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("TEST_BOOL", raw)
        assert parse_bool("TEST_BOOL") is True


class TestParseBoolFalseyMatrix:
    """REQ-002: All canonical falsey values return False."""

    @pytest.mark.parametrize(
        "raw",
        ["0", "false", "FALSE", "False", "no", "NO", "No", "off", "OFF", "Off", ""],
    )
    def test_falsey_values(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("TEST_BOOL", raw)
        assert parse_bool("TEST_BOOL") is False

    def test_unset_returns_default_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert parse_bool("TEST_BOOL") is False

    def test_unset_returns_default_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert parse_bool("TEST_BOOL", default=True) is True


class TestParseBoolUnknown:
    """REQ-003: Unknown values → ConfigError (strict) or default + warning (non-strict)."""

    @pytest.mark.parametrize("raw", ["maybe", "None", "2", "  random  "])
    def test_strict_raises(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("TEST_BOOL", raw)
        with pytest.raises(ConfigError, match="invalid boolean value"):
            parse_bool("TEST_BOOL", strict=True)

    @pytest.mark.parametrize("raw", ["maybe", "None", "2", "  random  "])
    def test_nonstrict_returns_default(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("TEST_BOOL", raw)
        assert parse_bool("TEST_BOOL", default=False, strict=False) is False

    def test_nonstrict_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("TEST_BOOL", "garbage")
        with caplog.at_level(logging.WARNING):
            parse_bool("TEST_BOOL", strict=False)
        assert "Invalid boolean value" in caplog.text

    def test_whitespace_only_is_falsey(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOOL", "   ")
        assert parse_bool("TEST_BOOL") is False


class TestParseBoolDefault:
    """REQ-004: default parameter controls unset behavior."""

    def test_default_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert parse_bool("TEST_BOOL", default=True) is True

    def test_default_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert parse_bool("TEST_BOOL", default=False) is False


# ---------------------------------------------------------------------------
# parse_int
# ---------------------------------------------------------------------------


class TestParseInt:
    """REQ-005: Integer parsing with bounds and strict/non-strict."""

    def test_valid_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "42")
        assert parse_int("TEST_INT") == 42

    def test_negative_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "-7")
        assert parse_int("TEST_INT") == -7

    def test_whitespace_trimmed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", " 100 ")
        assert parse_int("TEST_INT") == 100

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_INT", raising=False)
        assert parse_int("TEST_INT", default=99) == 99

    def test_empty_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "")
        assert parse_int("TEST_INT", default=99) == 99

    def test_invalid_strict_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "abc")
        with pytest.raises(ConfigError, match="invalid integer value"):
            parse_int("TEST_INT", strict=True)

    def test_invalid_nonstrict_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "abc")
        assert parse_int("TEST_INT", default=50, strict=False) == 50

    def test_min_bound_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "3")
        with pytest.raises(ConfigError, match="below minimum"):
            parse_int("TEST_INT", min_value=5, strict=True)

    def test_max_bound_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "100")
        with pytest.raises(ConfigError, match="above maximum"):
            parse_int("TEST_INT", max_value=50, strict=True)

    def test_min_bound_nonstrict_clamps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "3")
        assert parse_int("TEST_INT", min_value=5, strict=False) == 5

    def test_max_bound_nonstrict_clamps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "100")
        assert parse_int("TEST_INT", max_value=50, strict=False) == 50


# ---------------------------------------------------------------------------
# parse_csv
# ---------------------------------------------------------------------------


class TestParseCsv:
    def test_basic_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_CSV", "a,b,c")
        assert parse_csv("TEST_CSV") == ["a", "b", "c"]

    def test_trim_and_drop_empties(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_CSV", "a,b, c ,, ")
        assert parse_csv("TEST_CSV") == ["a", "b", "c"]

    def test_unset_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_CSV", raising=False)
        assert parse_csv("TEST_CSV") == []

    def test_empty_string_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_CSV", "")
        assert parse_csv("TEST_CSV") == []

    def test_single_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_CSV", "only")
        assert parse_csv("TEST_CSV") == ["only"]


# ---------------------------------------------------------------------------
# parse_enum
# ---------------------------------------------------------------------------


class TestParseEnum:
    """REQ-006/007: Enum parsing with casefold, strict/non-strict."""

    ALLOWED: ClassVar[set[str]] = {"PAUSE", "EMERGENCY"}

    def test_valid_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENUM", "PAUSE")
        assert parse_enum("TEST_ENUM", allowed=self.ALLOWED) == "PAUSE"

    def test_valid_casefold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENUM", "emergency")
        assert parse_enum("TEST_ENUM", allowed=self.ALLOWED, casefold=True) == "EMERGENCY"

    def test_valid_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENUM", " PAUSE ")
        assert parse_enum("TEST_ENUM", allowed=self.ALLOWED) == "PAUSE"

    def test_invalid_strict_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENUM", "INVALID")
        with pytest.raises(ConfigError, match="invalid value for"):
            parse_enum("TEST_ENUM", allowed=self.ALLOWED, strict=True)

    def test_invalid_nonstrict_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENUM", "INVALID")
        assert parse_enum("TEST_ENUM", allowed=self.ALLOWED, default=None, strict=False) is None

    def test_invalid_nonstrict_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("TEST_ENUM", "INVALID")
        with caplog.at_level(logging.WARNING):
            parse_enum("TEST_ENUM", allowed=self.ALLOWED, strict=False)
        assert "Invalid value for" in caplog.text

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_ENUM", raising=False)
        assert parse_enum("TEST_ENUM", allowed=self.ALLOWED, default=None) is None

    def test_empty_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENUM", "")
        assert parse_enum("TEST_ENUM", allowed=self.ALLOWED, default=None) is None

    def test_no_casefold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENUM", "pause")
        with pytest.raises(ConfigError, match="invalid value for"):
            parse_enum("TEST_ENUM", allowed=self.ALLOWED, casefold=False, strict=True)


# ---------------------------------------------------------------------------
# REQ-008: All parse functions handle unset env
# ---------------------------------------------------------------------------


class TestUnsetEnv:
    def test_parse_bool_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UNSET_VAR", raising=False)
        assert parse_bool("UNSET_VAR") is False

    def test_parse_int_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UNSET_VAR", raising=False)
        assert parse_int("UNSET_VAR", default=10) == 10

    def test_parse_csv_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UNSET_VAR", raising=False)
        assert parse_csv("UNSET_VAR") == []

    def test_parse_enum_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UNSET_VAR", raising=False)
        assert parse_enum("UNSET_VAR", allowed={"A", "B"}, default=None) is None
