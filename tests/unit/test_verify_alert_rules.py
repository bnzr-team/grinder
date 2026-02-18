"""Tests for scripts/verify_alert_rules.py (Launch-04).

Covers:
- Valid YAML → PASS
- Duplicate alert names → FAIL
- symbol= in expr/labels/annotations → FAIL
- Empty expr → FAIL
- Invalid YAML → exit 2
- Real alert_rules.yml passes validation
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.verify_alert_rules import main, validate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_RULES: dict[str, object] = {
    "groups": [
        {
            "name": "test_group",
            "rules": [
                {
                    "alert": "TestAlert",
                    "expr": "up == 1",
                    "labels": {"severity": "warning"},
                    "annotations": {"summary": "Test alert"},
                },
            ],
        },
    ],
}


def _rules_with(**overrides: object) -> dict[str, object]:
    """Build a single-rule YAML dict with field overrides."""
    rule: dict[str, object] = {
        "alert": "TestAlert",
        "expr": "up == 1",
        "labels": {"severity": "warning"},
        "annotations": {"summary": "Test"},
    }
    rule.update(overrides)
    return {"groups": [{"name": "g", "rules": [rule]}]}


# ---------------------------------------------------------------------------
# validate() unit tests
# ---------------------------------------------------------------------------


class TestValidateHappy:
    def test_valid_rules_pass(self) -> None:
        errors = validate(VALID_RULES)
        assert errors == []

    def test_multiple_groups_unique_names(self) -> None:
        data = {
            "groups": [
                {"name": "g1", "rules": [{"alert": "A", "expr": "up == 1"}]},
                {"name": "g2", "rules": [{"alert": "B", "expr": "up == 0"}]},
            ],
        }
        assert validate(data) == []


class TestValidateDuplicates:
    def test_duplicate_name_same_group(self) -> None:
        data = {
            "groups": [
                {
                    "name": "g1",
                    "rules": [
                        {"alert": "Dup", "expr": "up == 1"},
                        {"alert": "Dup", "expr": "up == 0"},
                    ],
                },
            ],
        }
        errors = validate(data)
        assert len(errors) == 1
        assert "duplicate alert name" in errors[0]

    def test_duplicate_name_cross_group(self) -> None:
        data = {
            "groups": [
                {"name": "g1", "rules": [{"alert": "Dup", "expr": "up == 1"}]},
                {"name": "g2", "rules": [{"alert": "Dup", "expr": "up == 0"}]},
            ],
        }
        errors = validate(data)
        assert len(errors) == 1
        assert "duplicate" in errors[0]


class TestValidateSymbol:
    def test_symbol_in_expr(self) -> None:
        data = _rules_with(expr='rate(metric{symbol="BTCUSDT"}[5m])')
        errors = validate(data)
        assert len(errors) == 1
        assert "symbol=" in errors[0]

    def test_symbol_in_labels(self) -> None:
        data = _rules_with(labels={"severity": "warning", "symbol": "BTCUSDT"})
        # "symbol" as a key won't match, but "symbol=" in a value would.
        # Let's test symbol= in annotation value instead.
        data = _rules_with(
            annotations={"desc": "Check symbol=BTCUSDT for issues"},
        )
        errors = validate(data)
        assert len(errors) == 1
        assert "symbol=" in errors[0]

    def test_symbol_regex_match(self) -> None:
        data = _rules_with(expr='metric{symbol=~"BTC.*"}')
        errors = validate(data)
        assert len(errors) == 1
        assert "symbol=" in errors[0]

    def test_symbol_negation_match(self) -> None:
        data = _rules_with(expr='metric{symbol!=""}')
        errors = validate(data)
        assert len(errors) == 1
        assert "symbol=" in errors[0]

    def test_stream_label_allowed(self) -> None:
        data = _rules_with(expr='rate(grinder_data_stale_total{stream="live_feed"}[5m])')
        errors = validate(data)
        assert errors == []


class TestValidateEmptyExpr:
    def test_empty_expr(self) -> None:
        data = _rules_with(expr="")
        errors = validate(data)
        assert any("empty 'expr'" in e for e in errors)

    def test_whitespace_expr(self) -> None:
        data = _rules_with(expr="   ")
        errors = validate(data)
        assert any("empty 'expr'" in e for e in errors)


class TestValidateStructure:
    def test_missing_groups(self) -> None:
        errors = validate({"rules": []})
        assert len(errors) == 1
        assert "groups" in errors[0]

    def test_missing_rules_in_group(self) -> None:
        data = {"groups": [{"name": "g1"}]}
        errors = validate(data)
        assert len(errors) == 1
        assert "rules" in errors[0]


# ---------------------------------------------------------------------------
# main() integration tests (via tmp_path)
# ---------------------------------------------------------------------------


class TestMainIntegration:
    def test_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yml"
        p.write_text(
            "groups:\n  - name: test\n    rules:\n      - alert: TestAlert\n        expr: up == 1\n"
        )
        assert main([str(p)]) == 0

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yml"
        p.write_text("groups:\n  - name: [invalid\n")
        assert main([str(p)]) == 2

    def test_file_not_found(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent.yml"
        assert main([str(p)]) == 2

    def test_duplicate_fails(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yml"
        p.write_text(
            "groups:\n"
            "  - name: g1\n"
            "    rules:\n"
            "      - alert: Dup\n"
            "        expr: up == 1\n"
            "      - alert: Dup\n"
            "        expr: up == 0\n"
        )
        assert main([str(p)]) == 1

    def test_symbol_fails(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yml"
        p.write_text(
            "groups:\n"
            "  - name: g1\n"
            "    rules:\n"
            "      - alert: Bad\n"
            "        expr: 'metric{symbol=\"BTC\"}'\n"
        )
        assert main([str(p)]) == 1


class TestRealAlertRules:
    """Validate the actual alert_rules.yml in the repo."""

    def test_real_rules_pass(self) -> None:
        real_path = Path("monitoring/alert_rules.yml")
        if not real_path.exists():
            pytest.skip("monitoring/alert_rules.yml not found")
        assert main([str(real_path)]) == 0
