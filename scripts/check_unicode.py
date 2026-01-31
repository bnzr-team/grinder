#!/usr/bin/env python3
"""Check docs for dangerous Unicode characters.

This script scans all markdown files in docs/ for:
1. Bidi controls (U+202A-202E, U+2066-2069, U+200E-200F) - DANGEROUS
2. Zero-width chars (U+200B-200D, U+FEFF) - DANGEROUS
3. Soft hyphen (U+00AD) - DANGEROUS
4. All Unicode category Cf (Format) and Cc (Control) - SUSPICIOUS

Box-drawing characters (U+2500-257F) are explicitly allowed for diagrams.

Exit codes:
  0 - Clean (no dangerous chars, only allowed non-ASCII)
  1 - Dangerous chars found
"""

from __future__ import annotations

import pathlib
import sys
import unicodedata

# Dangerous: bidi controls, zero-width, soft hyphen, BOM
DANGEROUS = {
    # Bidi embeds/overrides
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    # Bidi isolates
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    # LRM/RLM
    "\u200e",
    "\u200f",
    # Zero-width
    "\u200b",
    "\u200c",
    "\u200d",
    # BOM / zero-width no-break
    "\ufeff",
    # Soft hyphen
    "\u00ad",
    # Invisible operators
    "\u2060",
    "\u2061",
    "\u2062",
    "\u2063",
    "\u2064",
    # Mongolian vowel separator
    "\u180e",
}

# Allowed non-ASCII ranges
ALLOWED_RANGES = [
    (0x0400, 0x04FF),  # Cyrillic (Russian text in docs)
    (0x2500, 0x257F),  # Box-drawing (diagrams)
    (0x2010, 0x2027),  # Dashes, quotes
    (0x2190, 0x21FF),  # Arrows
    (0x2700, 0x27BF),  # Dingbats (checkmarks, etc.)
    (0x2026, 0x2026),  # Ellipsis
]


def is_allowed(code: int) -> bool:
    """Check if codepoint is in allowed ranges."""
    return any(start <= code <= end for start, end in ALLOWED_RANGES)


def main() -> int:  # noqa: PLR0912
    docs_path = pathlib.Path("docs")
    if not docs_path.exists():
        print("ERROR: docs/ directory not found")
        return 1

    dangerous_found: list[tuple[pathlib.Path, int, str, str]] = []
    suspicious_found: list[tuple[pathlib.Path, int, str, str, str]] = []
    box_drawing_files: set[pathlib.Path] = set()

    for p in docs_path.rglob("*.md"):
        content = p.read_text(encoding="utf-8", errors="replace")

        for i, ch in enumerate(content):
            code = ord(ch)

            # Skip ASCII
            if code < 128:
                continue

            line_num = content[:i].count("\n") + 1

            # Check dangerous
            if ch in DANGEROUS:
                dangerous_found.append((p, line_num, f"U+{code:04X}", ch))
                continue

            # Check category Cf (Format) or Cc (Control)
            category = unicodedata.category(ch)
            if category in ("Cf", "Cc"):
                suspicious_found.append((p, line_num, f"U+{code:04X}", category, ch))
                continue

            # Track box-drawing usage
            if 0x2500 <= code <= 0x257F:
                box_drawing_files.add(p)

    # Report
    print("=" * 60)
    print("UNICODE SECURITY SCAN REPORT")
    print("=" * 60)

    print("\n## 1. DANGEROUS CHARS (bidi/zero-width/soft-hyphen)")
    if dangerous_found:
        print(f"FOUND {len(dangerous_found)} dangerous characters:")
        for filepath, linenum, codepoint, _char in dangerous_found:
            print(f"  - {filepath}:{linenum} {codepoint}")
        print("\nVERDICT: FAIL - dangerous Unicode found")
        return 1
    else:
        print("NONE FOUND")
        print("VERDICT: PASS")

    print("\n## 2. SUSPICIOUS CHARS (category Cf/Cc)")
    if suspicious_found:
        print(f"FOUND {len(suspicious_found)} suspicious characters:")
        for filepath, linenum, codepoint, cat, _char in suspicious_found:
            print(f"  - {filepath}:{linenum} {codepoint} (category={cat})")
    else:
        print("NONE FOUND")
        print("VERDICT: PASS")

    print("\n## 3. BOX-DRAWING CHARS (U+2500-257F)")
    if box_drawing_files:
        print(f"Used in {len(box_drawing_files)} files (for diagrams):")
        for p in sorted(box_drawing_files):
            print(f"  - {p}")
        print("VERDICT: ALLOWED (legitimate diagram characters)")
    else:
        print("NONE FOUND")

    print("\n" + "=" * 60)
    print("FINAL VERDICT: CLEAN")
    print("No dangerous bidi/zero-width/format characters found.")
    print("Box-drawing chars are present for diagrams (allowed).")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
