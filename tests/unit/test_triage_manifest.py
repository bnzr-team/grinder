"""Tests for scripts/triage_manifest.py (OPS-4)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from scripts.triage_manifest import (
    cmd_append,
    cmd_init,
    detect_mode,
    is_sensitive_env_key,
    main,
    redact_url,
)

# ---------------------------------------------------------------------------
# detect_mode
# ---------------------------------------------------------------------------


class TestDetectMode:
    def test_auto_github_actions_true(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False):
            assert detect_mode("auto") == "ci"

    def test_auto_github_actions_1(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "1"}, clear=False):
            assert detect_mode("auto") == "ci"

    def test_auto_prometheus_url(self) -> None:
        env = {"PROMETHEUS_URL": "http://prom:9090"}
        with mock.patch.dict(os.environ, env, clear=False):
            # Remove GITHUB_ACTIONS if present
            os.environ.pop("GITHUB_ACTIONS", None)
            assert detect_mode("auto") == "prod"

    def test_auto_grinder_base_url(self) -> None:
        env = {"GRINDER_BASE_URL": "http://grinder:8000"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            os.environ.pop("PROMETHEUS_URL", None)
            assert detect_mode("auto") == "prod"

    def test_auto_no_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert detect_mode("auto") == "local"

    def test_explicit_ci(self) -> None:
        # Explicit mode ignores env
        with mock.patch.dict(os.environ, {}, clear=True):
            assert detect_mode("ci") == "ci"

    def test_explicit_prod(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert detect_mode("prod") == "prod"


# ---------------------------------------------------------------------------
# redact_url
# ---------------------------------------------------------------------------


class TestRedactUrl:
    def test_strips_basic_auth(self) -> None:
        assert redact_url("http://user:pass@host:9090") == "http://redacted@host:9090"

    def test_no_auth_unchanged(self) -> None:
        assert redact_url("http://host:9090/metrics") == "http://host:9090/metrics"

    def test_not_a_url_unchanged(self) -> None:
        assert redact_url("not-a-url") == "not-a-url"

    def test_at_without_colon_unchanged(self) -> None:
        # user@ without password colon is not redacted
        assert redact_url("http://user@host:9090") == "http://user@host:9090"

    def test_https(self) -> None:
        assert redact_url("https://admin:s3cret@prom.io") == "https://redacted@prom.io"


# ---------------------------------------------------------------------------
# is_sensitive_env_key
# ---------------------------------------------------------------------------


class TestIsSensitiveEnvKey:
    @pytest.mark.parametrize(
        "key",
        ["BINANCE_API_KEY", "BINANCE_API_SECRET", "SOME_TOKEN", "DB_PASSWORD", "OAUTH_AUTH_TOKEN"],
    )
    def test_sensitive_keys(self, key: str) -> None:
        assert is_sensitive_env_key(key) is True

    @pytest.mark.parametrize(
        "key",
        ["METRICS_URL", "PROMETHEUS_URL", "HOME", "PATH", "LOG_LINES"],
    )
    def test_non_sensitive_keys(self, key: str) -> None:
        assert is_sensitive_env_key(key) is False


# ---------------------------------------------------------------------------
# init subcommand
# ---------------------------------------------------------------------------


class TestCmdInit:
    def test_creates_valid_manifest(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name

        args = _init_args(out=out_path)
        rc = cmd_init(args)
        assert rc == 0

        data = json.loads(Path(out_path).read_text())
        assert data["schema_version"] == "triage-manifest-v1"
        assert data["mode"] == "local"
        assert isinstance(data["artifacts"], list)
        assert isinstance(data["warnings"], list)
        assert isinstance(data["next_steps"], list)
        assert "generated_at_utc" in data
        assert "environment" in data
        assert "inputs" in data

        inputs = data["inputs"]
        assert inputs["metrics_url"] == "http://localhost:9090/metrics"
        assert inputs["readyz_url"] == "http://localhost:9090/readyz"
        assert inputs["log_lines"] == 300
        assert inputs["compact"] is False
        assert inputs["bundle_format"] == "txt"

    def test_redacts_url_in_inputs(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name

        args = _init_args(out=out_path, metrics_url="http://admin:pass@prom:9090/metrics")
        cmd_init(args)

        data = json.loads(Path(out_path).read_text())
        assert "pass" not in data["inputs"]["metrics_url"]
        assert "redacted" in data["inputs"]["metrics_url"]


# ---------------------------------------------------------------------------
# append subcommand
# ---------------------------------------------------------------------------


class TestCmdAppend:
    def test_appends_artifact(self) -> None:
        mf_path = _create_empty_manifest()
        args = _append_args(
            manifest=mf_path,
            name="readyz",
            path="readyz.txt",
            ok="1",
            cmd="curl -sS http://localhost:9090/readyz",
            bytes_=42,
        )
        rc = cmd_append(args)
        assert rc == 0

        data = json.loads(Path(mf_path).read_text())
        assert len(data["artifacts"]) == 1
        a = data["artifacts"][0]
        assert a["name"] == "readyz"
        assert a["ok"] is True
        assert a["bytes"] == 42

    def test_appends_warning_and_next_step(self) -> None:
        mf_path = _create_empty_manifest()
        args = _append_args(
            manifest=mf_path,
            name="metrics",
            path="metrics.txt",
            ok="0",
            cmd="curl -fsS http://localhost:9090/metrics",
            error="exit=7",
            warning=["metrics unreachable"],
            next_step=["check port 9090"],
        )
        cmd_append(args)

        data = json.loads(Path(mf_path).read_text())
        assert data["artifacts"][0]["ok"] is False
        assert data["artifacts"][0]["error"] == "exit=7"
        assert "metrics unreachable" in data["warnings"]
        assert "check port 9090" in data["next_steps"]

    def test_preserves_existing_data(self) -> None:
        mf_path = _create_empty_manifest()
        # First append
        cmd_append(_append_args(manifest=mf_path, name="a", path="a.txt", ok="1", cmd="echo a"))
        # Second append
        cmd_append(
            _append_args(
                manifest=mf_path, name="b", path="b.txt", ok="0", cmd="echo b", error="exit=1"
            )
        )

        data = json.loads(Path(mf_path).read_text())
        assert len(data["artifacts"]) == 2
        assert data["artifacts"][0]["name"] == "a"
        assert data["artifacts"][1]["name"] == "b"


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------


class TestMain:
    def test_no_subcommand_returns_2(self) -> None:
        assert main([]) == 2

    def test_init_via_main(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name
        rc = main(["init", "--out", out_path])
        assert rc == 0
        data = json.loads(Path(out_path).read_text())
        assert data["schema_version"] == "triage-manifest-v1"


# ---------------------------------------------------------------------------
# Integration: bundle always created with manifest
# ---------------------------------------------------------------------------


class TestBundleIntegration:
    def test_bundle_tgz_contains_manifest_on_failure(self) -> None:
        """triage_bundle.sh --bundle-format tgz creates manifest even when endpoints fail."""
        bundle_script = Path("scripts/triage_bundle.sh")
        if not bundle_script.exists():
            pytest.skip("triage_bundle.sh not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_tgz = Path(tmpdir) / "bundle.tgz"
            result = subprocess.run(
                [
                    "bash",
                    str(bundle_script),
                    "--mode",
                    "local",
                    "--bundle-format",
                    "tgz",
                    "--metrics-url",
                    "http://127.0.0.1:1/metrics",
                    "--readyz-url",
                    "http://127.0.0.1:1/readyz",
                    "--out",
                    str(out_tgz),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            # Bundle should always be created (exit 0)
            assert result.returncode == 0, f"stderr: {result.stderr}"
            assert out_tgz.exists(), "tgz not created"

            # Extract and check manifest
            extract_dir = Path(tmpdir) / "extracted"
            extract_dir.mkdir()
            subprocess.run(
                ["tar", "-xzf", str(out_tgz), "-C", str(extract_dir)],
                check=True,
            )
            manifest_path = extract_dir / "triage_manifest.json"
            assert manifest_path.exists(), "triage_manifest.json missing from tgz"

            data = json.loads(manifest_path.read_text())
            assert data["schema_version"] == "triage-manifest-v1"
            assert data["mode"] == "local"

            # At least one artifact should have ok=false (bad URLs)
            failed = [a for a in data["artifacts"] if not a["ok"]]
            assert len(failed) > 0, f"expected failed artifacts, got: {data['artifacts']}"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _init_args(
    *,
    out: str = "/tmp/test_manifest.json",
    mode: str = "local",
    metrics_url: str = "http://localhost:9090/metrics",
    readyz_url: str = "http://localhost:9090/readyz",
    log_lines: int = 300,
    service: str = "grinder",
    compact: bool = False,
    bundle_format: str = "txt",
) -> argparse.Namespace:
    return argparse.Namespace(
        command="init",
        mode=mode,
        out=out,
        metrics_url=metrics_url,
        readyz_url=readyz_url,
        log_lines=log_lines,
        service=service,
        compact=compact,
        bundle_format=bundle_format,
    )


def _append_args(
    *,
    manifest: str,
    name: str = "test",
    path: str = "test.txt",
    ok: str = "1",
    cmd: str = "echo test",
    bytes_: int = 0,
    error: str = "",
    warning: list[str] | None = None,
    next_step: list[str] | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        command="append",
        manifest=manifest,
        name=name,
        path=path,
        ok=ok,
        cmd=cmd,
        bytes=bytes_,
        error=error,
        warning=warning or [],
        next_step=next_step or [],
    )


def _create_empty_manifest() -> str:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = f.name
    cmd_init(_init_args(out=out_path))
    return out_path
