#!/usr/bin/env python3
"""Triage manifest generator and appender (OPS-4).

Creates and maintains triage_manifest.json — structured metadata
about each artifact collected by triage_bundle.sh.

Subcommands:
  init    Create a new manifest skeleton
  append  Add artifact/warning/next-step entries to an existing manifest

Usage:
  python3 scripts/triage_manifest.py init --mode auto --out /tmp/manifest.json \\
      --metrics-url http://localhost:9090/metrics --readyz-url http://localhost:9090/readyz
  python3 scripts/triage_manifest.py append --manifest /tmp/manifest.json \\
      --name readyz --path readyz.txt --ok 1 --cmd "curl ..." --bytes 42
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "triage-manifest-v1"

_SENSITIVE_ENV_PATTERNS = [
    re.compile(r".*TOKEN.*", re.IGNORECASE),
    re.compile(r".*SECRET.*", re.IGNORECASE),
    re.compile(r".*KEY.*", re.IGNORECASE),
    re.compile(r".*PASSWORD.*", re.IGNORECASE),
    re.compile(r".*AUTH.*", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """ISO 8601 UTC timestamp without microseconds."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_cmd(cmd: list[str]) -> str:
    """Run a command, return stdout or empty string on failure."""
    try:
        return subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
    except Exception:
        return ""


def detect_mode(requested: str) -> str:
    """Resolve auto mode to ci/local/prod based on environment.

    Rules:
      - GITHUB_ACTIONS=true|1 → ci
      - GRINDER_BASE_URL or PROMETHEUS_URL set → prod
      - otherwise → local
    """
    if requested != "auto":
        return requested
    if os.getenv("GITHUB_ACTIONS") in ("true", "1"):
        return "ci"
    if os.getenv("GRINDER_BASE_URL") or os.getenv("PROMETHEUS_URL"):
        return "prod"
    return "local"


def redact_url(url: str) -> str:
    """Strip basic-auth credentials from a URL.

    ``http://user:pass@host`` → ``http://redacted@host``
    """
    if "://" not in url:
        return url
    try:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            userinfo, hostpart = rest.split("@", 1)
            if ":" in userinfo:
                return f"{scheme}://redacted@{hostpart}"
        return url
    except Exception:
        return "redacted"


def is_sensitive_env_key(key: str) -> bool:
    """Return True if the env var name looks like a secret."""
    return any(p.match(key) for p in _SENSITIVE_ENV_PATTERNS)


def _safe_env_presence(env: dict[str, str]) -> dict[str, Any]:
    """For sensitive env vars, record only ``{present: bool}``."""
    result: dict[str, Any] = {}
    for k, v in sorted(env.items()):
        if is_sensitive_env_key(k):
            result[k] = {"present": bool(v)}
    return result


def _build_environment() -> dict[str, Any]:
    return {
        "git_sha": _run_cmd(["git", "rev-parse", "HEAD"]),
        "branch": _run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "ci": os.getenv("GITHUB_ACTIONS") in ("true", "1"),
        "sensitive_env": _safe_env_presence(dict(os.environ)),
    }


# ---------------------------------------------------------------------------
# Init subcommand
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Create a new manifest skeleton."""
    mode = detect_mode(args.mode)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now_iso(),
        "mode": mode,
        "inputs": {
            "metrics_url": redact_url(args.metrics_url),
            "readyz_url": redact_url(args.readyz_url),
            "log_lines": args.log_lines,
            "service": args.service,
            "compact": args.compact,
            "bundle_format": args.bundle_format,
        },
        "environment": _build_environment(),
        "artifacts": [],
        "warnings": [],
        "next_steps": [],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# Append subcommand
# ---------------------------------------------------------------------------


def _load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    return data


def _save_manifest(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _ensure_list(obj: dict[str, Any], key: str) -> list[Any]:
    if key not in obj or obj[key] is None:
        obj[key] = []
    val = obj[key]
    if not isinstance(val, list):
        raise ValueError(f"manifest field {key!r} must be a list")
    return val


def cmd_append(args: argparse.Namespace) -> int:
    """Append artifact/warning/next-step entries to manifest."""
    mf_path = Path(args.manifest)
    mf = _load_manifest(mf_path)

    artifacts = _ensure_list(mf, "artifacts")
    artifacts.append(
        {
            "name": args.name,
            "path": args.path,
            "ok": args.ok == "1",
            "cmd": args.cmd,
            "bytes": args.bytes,
            "error": args.error,
        }
    )

    if args.warning:
        warnings = _ensure_list(mf, "warnings")
        warnings.extend(w for w in args.warning if w)

    if args.next_step:
        steps = _ensure_list(mf, "next_steps")
        steps.extend(s for s in args.next_step if s)

    _save_manifest(mf_path, mf)
    return 0


# ---------------------------------------------------------------------------
# Summary subcommand
# ---------------------------------------------------------------------------


def cmd_summary(args: argparse.Namespace) -> int:
    """Print a one-line manifest summary to stdout (read-only)."""
    mf_path = Path(args.manifest)
    if not mf_path.exists():
        print(f"error: manifest not found: {mf_path}", file=sys.stderr)
        return 2
    try:
        mf = _load_manifest(mf_path)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"error: invalid manifest: {exc}", file=sys.stderr)
        return 2

    mode = mf.get("mode", "unknown")
    artifacts = mf.get("artifacts", [])
    if not isinstance(artifacts, list):
        print("error: manifest artifacts is not a list", file=sys.stderr)
        return 2
    total = len(artifacts)
    failed = sum(1 for a in artifacts if isinstance(a, dict) and not a.get("ok", True))
    warnings_count = len(mf.get("warnings", []))
    next_steps_count = len(mf.get("next_steps", []))
    print(
        f"mode={mode} artifacts={total} failed={failed}"
        f" warnings={warnings_count} next_steps={next_steps_count}"
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns exit code."""
    parser = argparse.ArgumentParser(
        description="Triage manifest generator (OPS-4).",
    )
    sub = parser.add_subparsers(dest="command")

    # -- init --
    p_init = sub.add_parser("init", help="Create a new manifest skeleton")
    p_init.add_argument("--mode", choices=["auto", "ci", "local", "prod"], default="auto")
    p_init.add_argument("--out", required=True, help="Output JSON path")
    p_init.add_argument("--metrics-url", default="http://localhost:9090/metrics")
    p_init.add_argument("--readyz-url", default="http://localhost:9090/readyz")
    p_init.add_argument("--log-lines", type=int, default=300)
    p_init.add_argument("--service", default="grinder")
    p_init.add_argument("--compact", action="store_true")
    p_init.add_argument("--bundle-format", default="txt", choices=["txt", "tgz"])

    # -- append --
    p_append = sub.add_parser("append", help="Append entries to manifest")
    p_append.add_argument("--manifest", required=True, help="Path to triage_manifest.json")
    p_append.add_argument("--name", required=True, help="Artifact name (e.g. metrics, readyz)")
    p_append.add_argument("--path", required=True, help="Relative path inside bundle")
    p_append.add_argument("--ok", required=True, choices=["0", "1"], help="1=ok, 0=failed")
    p_append.add_argument("--cmd", required=True, help="Command used to produce the artifact")
    p_append.add_argument("--bytes", type=int, default=0, help="Artifact size in bytes")
    p_append.add_argument("--error", default="", help="Error string (e.g. exit=7)")
    p_append.add_argument("--warning", action="append", default=[], help="Warning (repeatable)")
    p_append.add_argument(
        "--next-step", action="append", default=[], help="Next-step hint (repeatable)"
    )

    # -- summary --
    p_summary = sub.add_parser("summary", help="Print one-line manifest summary")
    p_summary.add_argument("--manifest", required=True, help="Path to triage_manifest.json")

    args = parser.parse_args(argv)

    if args.command == "init":
        return cmd_init(args)
    if args.command == "append":
        return cmd_append(args)
    if args.command == "summary":
        return cmd_summary(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
