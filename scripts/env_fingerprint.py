"""Print environment fingerprint for CI/gates diagnostics.

Never fails on missing packages — prints MISSING instead.
Exit code is always 0.

Usage:
    python -m scripts.env_fingerprint
"""

from __future__ import annotations

import platform
import sys


def _pkg_version(name: str) -> str:
    """Get installed package version or MISSING."""
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version(name)
    except Exception:
        return "MISSING"


def main() -> None:
    """Print environment fingerprint."""
    in_venv = sys.prefix != sys.base_prefix

    print("=== ENV FINGERPRINT ===")
    print(f"python:       {sys.executable}")
    print(f"version:      {platform.python_version()}")
    print(f"platform:     {platform.platform()}")
    print(f"venv:         {'YES' if in_venv else 'NO'}")
    print(f"  prefix:     {sys.prefix}")
    print(f"  base:       {sys.base_prefix}")

    pkgs = [
        "numpy",
        "onnx",
        "onnxruntime",
        "scikit-learn",
        "skl2onnx",
    ]
    print("--- packages ---")
    for pkg in pkgs:
        print(f"  {pkg:20s} {_pkg_version(pkg)}")
    print("=== END FINGERPRINT ===")


if __name__ == "__main__":
    main()
