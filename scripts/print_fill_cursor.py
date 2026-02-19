#!/usr/bin/env python3
"""Print fill cursor fields as space-separated integers.

Usage:
    python3 scripts/print_fill_cursor.py <path>

Output (stdout):
    <last_trade_id> <last_ts_ms> <updated_at_ms>

Exit codes:
    0 -- success
    1 -- invalid JSON, missing keys, or file not found
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: print_fill_cursor.py <cursor_json_path>", file=sys.stderr)
        return 1

    path = sys.argv[1]
    try:
        with open(path) as f:  # noqa: PTH123
            data = json.load(f)
        tid = int(data["last_trade_id"])
        ts = int(data["last_ts_ms"])
        updated = int(data["updated_at_ms"])
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        TypeError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"{tid} {ts} {updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
