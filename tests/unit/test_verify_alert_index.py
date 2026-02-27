"""Tests for scripts/verify_alert_index.py (OBS-7)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from textwrap import dedent

from scripts.verify_alert_index import (
    AlertInfo,
    parse_index,
    parse_yaml,
    validate,
)

_YAML_TWO_ALERTS = dedent("""\
    groups:
      - name: test
        rules:
          - alert: FooDown
            expr: foo == 0
            labels:
              severity: critical
              component: process
              category: availability
            annotations:
              summary: "foo down"
              description: "foo is down"
              runbook_url: "docs/runbooks/02.md"
              dashboard_uid: "grinder-overview"
          - alert: BarHigh
            expr: bar > 10
            labels:
              severity: warning
              component: risk
              category: safety
            annotations:
              summary: "bar high"
              description: "bar is high"
              runbook_url: "docs/runbooks/04.md"
""")


def _index_md(
    *,
    critical: str = "| `FooDown` | process | availability | `grinder-overview` | [02](02.md) | check foo |",
    warning: str = "| `BarHigh` | risk | safety | — | [04](04.md) | check bar |",
    critical_count: int = 1,
    warning_count: int = 1,
) -> str:
    header = "| Alert | component | category | `dashboard_uid` | Runbook | First look |\n"
    sep = "|-------|-----------|----------|-----------------|---------|------------|\n"
    lines = [
        "# Alert Routing Index\n",
        "\n",
        "## Alert routing table\n",
        "\n",
        f"### Critical ({critical_count} alerts)\n",
        "\n",
        header,
        sep,
    ]
    if critical:
        lines.append(critical + "\n")
    lines += [
        "\n",
        f"### Warning ({warning_count} alerts)\n",
        "\n",
        header,
        sep,
    ]
    if warning:
        lines.append(warning + "\n")
    return "".join(lines)


def _yaml_alerts() -> list[AlertInfo]:
    """Parse the two-alert YAML fixture."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(_YAML_TWO_ALERTS)
        f.flush()
        return parse_yaml(Path(f.name))


def _index_alerts(md: str) -> tuple[list[AlertInfo], dict[str, int]]:
    """Parse index markdown string."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(md)
        f.flush()
        return parse_index(Path(f.name))


class TestParseYaml:
    def test_parses_two_alerts(self) -> None:
        alerts = _yaml_alerts()
        assert len(alerts) == 2
        names = {a.name for a in alerts}
        assert names == {"FooDown", "BarHigh"}

    def test_extracts_fields(self) -> None:
        alerts = _yaml_alerts()
        foo = next(a for a in alerts if a.name == "FooDown")
        assert foo.severity == "critical"
        assert foo.component == "process"
        assert foo.category == "availability"
        assert foo.dashboard_uid == "grinder-overview"

    def test_missing_dashboard_uid(self) -> None:
        alerts = _yaml_alerts()
        bar = next(a for a in alerts if a.name == "BarHigh")
        assert bar.dashboard_uid == ""


class TestParseIndex:
    def test_parses_two_sections(self) -> None:
        alerts, counts = _index_alerts(_index_md())
        assert len(alerts) == 2
        assert counts == {"critical": 1, "warning": 1}

    def test_extracts_dashboard_uid(self) -> None:
        alerts, _ = _index_alerts(_index_md())
        foo = next(a for a in alerts if a.name == "FooDown")
        assert foo.dashboard_uid == "grinder-overview"

    def test_dash_means_no_uid(self) -> None:
        alerts, _ = _index_alerts(_index_md())
        bar = next(a for a in alerts if a.name == "BarHigh")
        assert bar.dashboard_uid == ""


class TestValidateHappy:
    def test_no_errors_when_consistent(self) -> None:
        ya = _yaml_alerts()
        ia, hc = _index_alerts(_index_md())
        errors = validate(ya, ia, hc)
        assert errors == []


class TestCoverage:
    def test_missing_alert(self) -> None:
        ya = _yaml_alerts()
        md = _index_md(warning="", warning_count=0)
        ia, hc = _index_alerts(md)
        errors = validate(ya, ia, hc)
        assert any("missing from index" in e and "BarHigh" in e for e in errors)

    def test_extra_alert(self) -> None:
        ya = _yaml_alerts()
        extra_row = (
            "| `BarHigh` | risk | safety | — | [04](04.md) | check bar |\n"
            "| `PhantomAlert` | ml | latency | — | [18](18.md) | phantom |"
        )
        md = _index_md(warning=extra_row, warning_count=2)
        ia, hc = _index_alerts(md)
        errors = validate(ya, ia, hc)
        assert any("extra in index" in e and "PhantomAlert" in e for e in errors)


class TestSeverityMismatch:
    def test_wrong_severity_section(self) -> None:
        ya = _yaml_alerts()
        md = _index_md(
            critical=(
                "| `FooDown` | process | availability | `grinder-overview` | [02](02.md) | check foo |\n"
                "| `BarHigh` | risk | safety | `grinder-overview` | [04](04.md) | check bar |"
            ),
            critical_count=2,
            warning="",
            warning_count=0,
        )
        ia, hc = _index_alerts(md)
        errors = validate(ya, ia, hc)
        assert any("severity mismatch" in e and "BarHigh" in e for e in errors)


class TestHeadingCount:
    def test_wrong_heading_count(self) -> None:
        ya = _yaml_alerts()
        md = _index_md(critical_count=5)
        ia, hc = _index_alerts(md)
        errors = validate(ya, ia, hc)
        assert any("heading count mismatch" in e and "critical" in e for e in errors)


class TestDashboardUidRequired:
    def test_critical_without_uid(self) -> None:
        ya = _yaml_alerts()
        md = _index_md(
            critical="| `FooDown` | process | availability | — | [02](02.md) | check foo |",
        )
        ia, hc = _index_alerts(md)
        errors = validate(ya, ia, hc)
        assert any("requires dashboard_uid" in e and "FooDown" in e for e in errors)


class TestFieldMismatch:
    def test_component_mismatch(self) -> None:
        ya = _yaml_alerts()
        md = _index_md(
            critical="| `FooDown` | engine | availability | `grinder-overview` | [02](02.md) | check foo |",
        )
        ia, hc = _index_alerts(md)
        errors = validate(ya, ia, hc)
        assert any("component mismatch" in e and "FooDown" in e for e in errors)

    def test_category_mismatch(self) -> None:
        ya = _yaml_alerts()
        md = _index_md(
            warning="| `BarHigh` | risk | latency | — | [04](04.md) | check bar |",
        )
        ia, hc = _index_alerts(md)
        errors = validate(ya, ia, hc)
        assert any("category mismatch" in e and "BarHigh" in e for e in errors)


class TestRealFiles:
    def test_real_alert_index_is_consistent(self) -> None:
        """Golden positive: ALERT_INDEX.md matches alert_rules.yml."""
        rules_path = Path("monitoring/alert_rules.yml")
        index_path = Path("docs/runbooks/ALERT_INDEX.md")
        if not rules_path.exists() or not index_path.exists():
            return
        ya = parse_yaml(rules_path)
        ia, hc = parse_index(index_path)
        errors = validate(ya, ia, hc)
        assert errors == [], "ALERT_INDEX.md is inconsistent:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
