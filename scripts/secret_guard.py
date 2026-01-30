#!/usr/bin/env python3
"""
Secret Guard - Scan for accidentally committed secrets.

Usage:
    python -m scripts.secret_guard
    python -m scripts.secret_guard --verbose
"""

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple


class SecretMatch(NamedTuple):
    file: Path
    line_num: int
    pattern_name: str
    match: str


# Patterns for common secrets
SECRET_PATTERNS = [
    # API Keys
    (r"(?i)api[_-]?key['\"]?\s*[:=]\s*['\"]?([a-zA-Z0-9]{20,})", "API_KEY"),
    (r"(?i)apikey['\"]?\s*[:=]\s*['\"]?([a-zA-Z0-9]{20,})", "API_KEY"),
    # API Secrets
    (r"(?i)api[_-]?secret['\"]?\s*[:=]\s*['\"]?([a-zA-Z0-9]{20,})", "API_SECRET"),
    (r"(?i)secret[_-]?key['\"]?\s*[:=]\s*['\"]?([a-zA-Z0-9]{20,})", "SECRET_KEY"),
    # AWS
    (r"AKIA[0-9A-Z]{16}", "AWS_ACCESS_KEY"),
    (r"(?i)aws[_-]?secret['\"]?\s*[:=]\s*['\"]?([a-zA-Z0-9/+=]{40})", "AWS_SECRET"),
    # Private Keys
    (r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", "PRIVATE_KEY"),
    (r"-----BEGIN PGP PRIVATE KEY BLOCK-----", "PGP_PRIVATE_KEY"),
    # Tokens
    (r"(?i)bearer\s+[a-zA-Z0-9\-_.]+", "BEARER_TOKEN"),
    (r"ghp_[a-zA-Z0-9]{36}", "GITHUB_TOKEN"),
    (r"gho_[a-zA-Z0-9]{36}", "GITHUB_OAUTH"),
    (r"github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}", "GITHUB_PAT"),
    # Database URLs
    (r"(?i)(postgres|mysql|mongodb)://[^:]+:[^@]+@", "DATABASE_URL"),
    # Generic passwords
    (r"(?i)password['\"]?\s*[:=]\s*['\"]([^'\"]{8,})['\"]", "PASSWORD"),
    # Binance specific
    (r"[a-zA-Z0-9]{64}", "POSSIBLE_BINANCE_SECRET"),  # Only flag if near api/secret keyword
]

# Files/directories to skip
SKIP_PATTERNS = [
    r"\.git/",
    r"\.venv/",
    r"__pycache__/",
    r"\.pyc$",
    r"\.pyo$",
    r"node_modules/",
    r"\.egg-info/",
    r"dist/",
    r"build/",
    r"\.coverage",
    r"htmlcov/",
    r"\.pytest_cache/",
    r"\.mypy_cache/",
    r"\.ruff_cache/",
]

# False positive patterns
FALSE_POSITIVE_PATTERNS = [
    r"example",
    r"placeholder",
    r"your[_-]?api",
    r"<api",
    r"xxx",
    r"test",
    r"dummy",
    r"fake",
    r"mock",
    r"sample",
]


def should_skip(path: Path) -> bool:
    """Check if path should be skipped."""
    path_str = str(path)
    return any(re.search(pattern, path_str) for pattern in SKIP_PATTERNS)


def is_false_positive(match: str) -> bool:
    """Check if match is likely a false positive."""
    match_lower = match.lower()
    return any(re.search(pattern, match_lower) for pattern in FALSE_POSITIVE_PATTERNS)


def scan_file(file_path: Path) -> list[SecretMatch]:
    """Scan a file for secrets."""
    matches: list[SecretMatch] = []

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return matches

    for line_num, line in enumerate(content.split("\n"), 1):
        for pattern, pattern_name in SECRET_PATTERNS:
            for match in re.finditer(pattern, line):
                matched_text = match.group(0)

                # Skip false positives
                if is_false_positive(matched_text):
                    continue

                # For generic patterns, require context
                # Only flag POSSIBLE_BINANCE_SECRET if near api/secret keyword
                if pattern_name == "POSSIBLE_BINANCE_SECRET" and not re.search(
                    r"(?i)(api|secret|key)", line
                ):
                    continue

                matches.append(
                    SecretMatch(
                        file=file_path,
                        line_num=line_num,
                        pattern_name=pattern_name,
                        match=matched_text[:50] + "..." if len(matched_text) > 50 else matched_text,
                    )
                )

    return matches


def scan_directory(root: Path, verbose: bool = False) -> list[SecretMatch]:
    """Scan directory for secrets."""
    all_matches = []

    for path in root.rglob("*"):
        if path.is_file() and not should_skip(path):
            if verbose:
                print(f"Scanning: {path}")

            matches = scan_file(path)
            all_matches.extend(matches)

    return all_matches


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan for secrets in codebase")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--path", type=Path, default=Path(), help="Path to scan")
    args = parser.parse_args()

    print(f"Scanning {args.path.absolute()} for secrets...")

    matches = scan_directory(args.path, verbose=args.verbose)

    if matches:
        print(f"\nFOUND {len(matches)} POTENTIAL SECRET(S):\n")

        for match in matches:
            print(f"  {match.file}:{match.line_num}")
            print(f"    Type: {match.pattern_name}")
            print(f"    Match: {match.match}")
            print()

        print("Please review these matches and remove any real secrets.")
        print("Use environment variables or secret managers instead.")
        sys.exit(1)
    else:
        print("\nNo secrets found.")
        sys.exit(0)


if __name__ == "__main__":
    main()
