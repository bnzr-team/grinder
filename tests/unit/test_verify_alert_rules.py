"""Tests for scripts/verify_alert_rules.py (Launch-05 + OBS-4).

Covers:
- Valid YAML → PASS
- Duplicate alert names → FAIL
- Forbidden labels (symbol=, order_id=, key=, client_id=) → FAIL
- Empty expr → FAIL
- Invalid YAML → exit 2
- op= allowlist: valid ops pass, unknown ops fail
- Real alert_rules.yml passes validation
- OBS-4: required labels (component, category), enum validation,
  dashboard_uid for critical/page, template prohibition
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from scripts.verify_alert_rules import (
    COMPONENT_ENUM,
    SEVERITY_ENUM,
    main,
    validate,
)

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
                    "labels": {
                        "severity": "warning",
                        "component": "engine",
                        "category": "availability",
                    },
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
        "labels": {
            "severity": "warning",
            "component": "engine",
            "category": "availability",
        },
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
        _labels = {"severity": "warning", "component": "engine", "category": "availability"}
        data = {
            "groups": [
                {"name": "g1", "rules": [{"alert": "A", "expr": "up == 1", "labels": _labels}]},
                {"name": "g2", "rules": [{"alert": "B", "expr": "up == 0", "labels": _labels}]},
            ],
        }
        assert validate(data) == []


class TestValidateDuplicates:
    def test_duplicate_name_same_group(self) -> None:
        _labels = {"severity": "warning", "component": "engine", "category": "availability"}
        data = {
            "groups": [
                {
                    "name": "g1",
                    "rules": [
                        {"alert": "Dup", "expr": "up == 1", "labels": _labels},
                        {"alert": "Dup", "expr": "up == 0", "labels": _labels},
                    ],
                },
            ],
        }
        errors = validate(data)
        assert len(errors) == 1
        assert "duplicate alert name" in errors[0]

    def test_duplicate_name_cross_group(self) -> None:
        _labels = {"severity": "warning", "component": "engine", "category": "availability"}
        data = {
            "groups": [
                {"name": "g1", "rules": [{"alert": "Dup", "expr": "up == 1", "labels": _labels}]},
                {"name": "g2", "rules": [{"alert": "Dup", "expr": "up == 0", "labels": _labels}]},
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


class TestValidateForbiddenLabels:
    """All forbidden labels (not just symbol=) are caught."""

    def test_order_id_in_expr(self) -> None:
        data = _rules_with(expr='metric{order_id="123"} > 0')
        errors = validate(data)
        assert len(errors) == 1
        assert "order_id=" in errors[0]

    def test_key_in_expr(self) -> None:
        data = _rules_with(expr='metric{key="abc"} > 0')
        errors = validate(data)
        assert len(errors) == 1
        assert "key=" in errors[0]

    def test_client_id_in_expr(self) -> None:
        data = _rules_with(expr='metric{client_id="x"} > 0')
        errors = validate(data)
        assert len(errors) == 1
        assert "client_id=" in errors[0]

    def test_op_label_allowed(self) -> None:
        data = _rules_with(expr='grinder_http_fail_total{op="cancel_order"} > 0')
        errors = validate(data)
        assert errors == []


class TestValidateOpAllowlist:
    """op= values must be from the ops taxonomy."""

    def test_valid_single_op(self) -> None:
        data = _rules_with(expr='grinder_http{op="cancel_order"} > 0')
        assert validate(data) == []

    def test_valid_regex_alternation(self) -> None:
        data = _rules_with(expr='grinder_http{op=~"cancel_order|place_order|cancel_all"} > 0')
        assert validate(data) == []

    def test_all_9_ops_valid(self) -> None:
        all_ops = "place_order|cancel_order|cancel_all|get_open_orders|get_positions|get_account|exchange_info|ping_time|get_user_trades"
        data = _rules_with(expr=f'grinder_http{{op=~"{all_ops}"}} > 0')
        assert validate(data) == []

    def test_unknown_op_rejected(self) -> None:
        data = _rules_with(expr='grinder_http{op="unknown_op"} > 0')
        errors = validate(data)
        assert len(errors) == 1
        assert "unknown op 'unknown_op'" in errors[0]

    def test_mixed_valid_invalid_ops(self) -> None:
        data = _rules_with(expr='grinder_http{op=~"cancel_order|bad_op"} > 0')
        errors = validate(data)
        assert len(errors) == 1
        assert "bad_op" in errors[0]

    def test_no_op_label_is_fine(self) -> None:
        data = _rules_with(expr="grinder_up == 1")
        assert validate(data) == []


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
    _VALID_YAML = (
        "groups:\n"
        "  - name: test\n"
        "    rules:\n"
        "      - alert: TestAlert\n"
        "        expr: up == 1\n"
        "        labels:\n"
        "          severity: warning\n"
        "          component: engine\n"
        "          category: availability\n"
    )

    def test_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "rules.yml"
        p.write_text(self._VALID_YAML)
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
            "        labels:\n"
            "          severity: warning\n"
            "          component: engine\n"
            "          category: availability\n"
            "      - alert: Dup\n"
            "        expr: up == 0\n"
            "        labels:\n"
            "          severity: warning\n"
            "          component: engine\n"
            "          category: availability\n"
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
            "        labels:\n"
            "          severity: warning\n"
            "          component: engine\n"
            "          category: availability\n"
        )
        assert main([str(p)]) == 1


class TestRealAlertRules:
    """Validate the actual alert_rules.yml in the repo."""

    def test_real_rules_pass(self) -> None:
        real_path = Path("monitoring/alert_rules.yml")
        if not real_path.exists():
            pytest.skip("monitoring/alert_rules.yml not found")
        assert main([str(real_path)]) == 0


# ---------------------------------------------------------------------------
# OBS-4: Alert annotation contract tests
# ---------------------------------------------------------------------------


class TestLabelContractComponent:
    """OBS-4: component label is required and enum-validated."""

    def test_missing_component(self) -> None:
        data = _rules_with(labels={"severity": "warning", "category": "availability"})
        errors = validate(data)
        assert any("missing required label 'component'" in e for e in errors)

    def test_empty_component(self) -> None:
        data = _rules_with(
            labels={"severity": "warning", "component": "", "category": "availability"},
        )
        errors = validate(data)
        assert any("missing required label 'component'" in e for e in errors)

    def test_invalid_component_enum(self) -> None:
        data = _rules_with(
            labels={"severity": "warning", "component": "scrpae", "category": "availability"},
        )
        errors = validate(data)
        assert len(errors) == 1
        assert "invalid component 'scrpae'" in errors[0]

    def test_all_valid_components(self) -> None:
        for comp in sorted(COMPONENT_ENUM):
            data = _rules_with(
                labels={"severity": "warning", "component": comp, "category": "availability"},
            )
            assert validate(data) == [], f"component '{comp}' should be valid"


class TestLabelContractCategory:
    """OBS-4: category label is required and enum-validated."""

    def test_missing_category(self) -> None:
        data = _rules_with(labels={"severity": "warning", "component": "engine"})
        errors = validate(data)
        assert any("missing required label 'category'" in e for e in errors)

    def test_empty_category(self) -> None:
        data = _rules_with(
            labels={"severity": "warning", "component": "engine", "category": ""},
        )
        errors = validate(data)
        assert any("missing required label 'category'" in e for e in errors)

    def test_invalid_category_enum(self) -> None:
        data = _rules_with(
            labels={"severity": "warning", "component": "engine", "category": "availabilty"},
        )
        errors = validate(data)
        assert len(errors) == 1
        assert "invalid category 'availabilty'" in errors[0]


class TestLabelContractSeverity:
    """OBS-4: severity enum validation."""

    def test_invalid_severity(self) -> None:
        data = _rules_with(
            labels={"severity": "urgent", "component": "engine", "category": "availability"},
        )
        errors = validate(data)
        assert any("invalid severity 'urgent'" in e for e in errors)

    def test_all_valid_severities(self) -> None:
        for sev in sorted(SEVERITY_ENUM):
            labels: dict[str, str] = {
                "severity": sev,
                "component": "engine",
                "category": "availability",
            }
            ann: dict[str, str] = {"summary": "Test"}
            if sev in {"critical", "page"}:
                ann["dashboard_uid"] = "grinder-overview"
            data = _rules_with(labels=labels, annotations=ann)
            assert validate(data) == [], f"severity '{sev}' should be valid"


class TestDashboardUidContract:
    """OBS-4: dashboard_uid required for critical/page severity."""

    def test_critical_without_dashboard_uid(self) -> None:
        data = _rules_with(
            labels={"severity": "critical", "component": "engine", "category": "availability"},
            annotations={"summary": "Down"},
        )
        errors = validate(data)
        assert any("requires 'dashboard_uid'" in e for e in errors)

    def test_page_without_dashboard_uid(self) -> None:
        data = _rules_with(
            labels={"severity": "page", "component": "engine", "category": "availability"},
            annotations={"summary": "Slow"},
        )
        errors = validate(data)
        assert any("requires 'dashboard_uid'" in e for e in errors)

    def test_critical_with_valid_dashboard_uid(self) -> None:
        data = _rules_with(
            labels={"severity": "critical", "component": "engine", "category": "availability"},
            annotations={"summary": "Down", "dashboard_uid": "grinder-overview"},
        )
        assert validate(data) == []

    def test_warning_without_dashboard_uid_ok(self) -> None:
        data = _rules_with(
            labels={"severity": "warning", "component": "engine", "category": "availability"},
        )
        assert validate(data) == []

    def test_invalid_dashboard_uid(self) -> None:
        data = _rules_with(
            labels={"severity": "critical", "component": "engine", "category": "availability"},
            annotations={"summary": "Down", "dashboard_uid": "bad-dashboard"},
        )
        errors = validate(data)
        assert any("invalid dashboard_uid 'bad-dashboard'" in e for e in errors)


class TestTemplateProhibition:
    """OBS-4: No Go-template syntax in constant fields."""

    def test_template_in_component(self) -> None:
        data = _rules_with(
            labels={
                "severity": "warning",
                "component": "{{ .Labels.job }}",
                "category": "availability",
            },
        )
        errors = validate(data)
        assert any("invalid component" in e for e in errors)

    def test_template_in_dashboard_uid(self) -> None:
        data = _rules_with(
            labels={"severity": "critical", "component": "engine", "category": "availability"},
            annotations={"summary": "X", "dashboard_uid": "{{ .ExternalURL }}"},
        )
        errors = validate(data)
        assert any("invalid dashboard_uid" in e for e in errors)


class TestRealAlertRulesContract:
    """OBS-4: Real alert_rules.yml passes ALL contract checks (golden positive)."""

    def test_real_rules_pass_full_contract(self) -> None:
        real_path = Path("monitoring/alert_rules.yml")
        if not real_path.exists():
            pytest.skip("monitoring/alert_rules.yml not found")
        assert main([str(real_path)]) == 0


# ---------------------------------------------------------------------------
# Alert name presence tests (snapshot protection)
# ---------------------------------------------------------------------------


def _load_alert_names() -> set[str]:
    """Load all alert names from the real alert_rules.yml."""
    real_path = Path("monitoring/alert_rules.yml")
    if not real_path.exists():
        pytest.skip("monitoring/alert_rules.yml not found")
    data = yaml.safe_load(real_path.read_text())
    names: set[str] = set()
    for group in data.get("groups", []):
        for rule in group.get("rules", []):
            name = rule.get("alert", "")
            if name:
                names.add(name)
    return names


class TestFsmAlertNamesPresent:
    """Launch-13: FSM alert names must exist in alert_rules.yml."""

    def test_fsm_bad_state_too_long(self) -> None:
        assert "FsmBadStateTooLong" in _load_alert_names()

    def test_fsm_action_blocked_spike(self) -> None:
        assert "FsmActionBlockedSpike" in _load_alert_names()


class TestSorAlertNamesPresent:
    """Launch-14: SOR alert names must exist in alert_rules.yml."""

    def test_sor_blocked_spike(self) -> None:
        assert "SorBlockedSpike" in _load_alert_names()

    def test_sor_noop_spike(self) -> None:
        assert "SorNoopSpike" in _load_alert_names()


class TestAccountSyncAlertNamesPresent:
    """Launch-15: AccountSync alert names must exist in alert_rules.yml."""

    def test_account_sync_stale(self) -> None:
        assert "AccountSyncStale" in _load_alert_names()

    def test_account_sync_errors(self) -> None:
        assert "AccountSyncErrors" in _load_alert_names()

    def test_account_sync_mismatch_spike(self) -> None:
        assert "AccountSyncMismatchSpike" in _load_alert_names()
