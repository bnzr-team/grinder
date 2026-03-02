"""Tests for gh_pr_meta.sh JSON parsing logic.

Validates that the GitHub REST API response format contains all fields
needed by gh_pr_meta.sh and acceptance_packet.sh.

No network calls - uses a local fixture JSON file.
No external tools (jq) - pure Python JSON parsing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "github_pr_sample.json"


@pytest.fixture()
def raw() -> dict[str, Any]:
    """Raw GitHub REST API response (fixture)."""
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def _extract_default(raw: dict[str, Any]) -> dict[str, Any]:
    """Simulate the default jq expression from gh_pr_meta.sh."""
    return {
        "number": raw["number"],
        "title": raw["title"],
        "state": raw["state"],
        "merged": raw["merged"],
        "merged_at": raw["merged_at"],
        "merge_commit_sha": raw["merge_commit_sha"],
        "base": raw["base"]["ref"],
        "head": raw["head"]["ref"],
        "head_sha": raw["head"]["sha"],
        "url": raw["html_url"],
    }


def _extract_acceptance_packet(raw: dict[str, Any]) -> dict[str, Any]:
    """Simulate the jq expression acceptance_packet.sh needs."""
    return {
        "url": raw["html_url"],
        "state": raw["state"],
        "mergedAt": raw["merged_at"],
        "mergeCommit": raw["merge_commit_sha"],
        "baseRefName": raw["base"]["ref"],
        "headRefName": raw["head"]["ref"],
        "title": raw["title"],
        "number": raw["number"],
    }


class TestDefaultExpression:
    """The default extraction from gh_pr_meta.sh."""

    def test_parses_all_fields(self, raw: dict[str, Any]) -> None:
        parsed = _extract_default(raw)

        assert parsed["number"] == 332
        assert parsed["title"] == "docs: SSOT refresh for main d7b778f"
        assert parsed["state"] == "closed"
        assert parsed["merged"] is True
        assert parsed["merged_at"] == "2026-03-02T10:30:00Z"
        assert parsed["merge_commit_sha"] == "0fd88f6628a581e234c800b7a6ac75289a8eb519"
        assert parsed["base"] == "main"
        assert parsed["head"] == "docs/refresh-ssot-d7b778f"
        assert parsed["head_sha"] == "abc123def456"
        assert parsed["url"] == "https://github.com/bnzr-team/grinder/pull/332"

    def test_merged_is_boolean(self, raw: dict[str, Any]) -> None:
        parsed = _extract_default(raw)
        assert isinstance(parsed["merged"], bool)

    def test_number_is_integer(self, raw: dict[str, Any]) -> None:
        parsed = _extract_default(raw)
        assert isinstance(parsed["number"], int)

    def test_null_merged_at_when_open(self) -> None:
        """Open PRs have merged_at=null."""
        fixture = json.loads(FIXTURE.read_text())
        fixture["state"] = "open"
        fixture["merged"] = False
        fixture["merged_at"] = None
        fixture["merge_commit_sha"] = None

        parsed = _extract_default(fixture)
        assert parsed["state"] == "open"
        assert parsed["merged"] is False
        assert parsed["merged_at"] is None
        assert parsed["merge_commit_sha"] is None


class TestAcceptancePacketCompat:
    """Fields needed by acceptance_packet.sh."""

    def test_all_required_fields_present(self, raw: dict[str, Any]) -> None:
        parsed = _extract_acceptance_packet(raw)

        assert parsed["url"] == "https://github.com/bnzr-team/grinder/pull/332"
        assert parsed["state"] == "closed"
        assert parsed["mergedAt"] == "2026-03-02T10:30:00Z"
        assert parsed["mergeCommit"] == "0fd88f6628a581e234c800b7a6ac75289a8eb519"
        assert parsed["baseRefName"] == "main"
        assert parsed["headRefName"] == "docs/refresh-ssot-d7b778f"
        assert parsed["title"] == "docs: SSOT refresh for main d7b778f"
        assert parsed["number"] == 332

    def test_head_sha_accessible(self, raw: dict[str, Any]) -> None:
        """acceptance_packet.yml needs headRefOid (= head.sha)."""
        assert raw["head"]["sha"] == "abc123def456"


class TestFixtureIntegrity:
    """Ensure fixture represents a valid GitHub REST API PR response."""

    REQUIRED_TOP_KEYS: ClassVar[set[str]] = {
        "number",
        "title",
        "state",
        "merged",
        "merged_at",
        "merge_commit_sha",
        "base",
        "head",
        "html_url",
    }

    def test_all_required_keys_present(self, raw: dict[str, Any]) -> None:
        missing = self.REQUIRED_TOP_KEYS - set(raw.keys())
        assert not missing, f"Missing keys in fixture: {missing}"

    def test_base_has_ref(self, raw: dict[str, Any]) -> None:
        assert "ref" in raw["base"]

    def test_head_has_ref_and_sha(self, raw: dict[str, Any]) -> None:
        assert "ref" in raw["head"]
        assert "sha" in raw["head"]
