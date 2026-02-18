"""Tests for fill cursor persistence (Launch-06 PR2)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from grinder.execution.fill_cursor import FillCursor, load_fill_cursor, save_fill_cursor

if TYPE_CHECKING:
    from pathlib import Path


class TestFillCursor:
    """FillCursor dataclass tests."""

    def test_default_values(self) -> None:
        cursor = FillCursor()
        assert cursor.last_trade_id == 0
        assert cursor.last_ts_ms == 0
        assert cursor.updated_at_ms == 0

    def test_custom_values(self) -> None:
        cursor = FillCursor(last_trade_id=42, last_ts_ms=1000, updated_at_ms=2000)
        assert cursor.last_trade_id == 42
        assert cursor.last_ts_ms == 1000
        assert cursor.updated_at_ms == 2000


class TestLoadFillCursor:
    """Tests for load_fill_cursor()."""

    def test_missing_file_returns_fresh(self, tmp_path: Path) -> None:
        cursor = load_fill_cursor(str(tmp_path / "nonexistent.json"))
        assert cursor.last_trade_id == 0
        assert cursor.last_ts_ms == 0

    def test_valid_file(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        path.write_text(
            json.dumps(
                {
                    "last_trade_id": 12345,
                    "last_ts_ms": 1700000000000,
                    "updated_at_ms": 1700000001000,
                }
            )
        )
        cursor = load_fill_cursor(str(path))
        assert cursor.last_trade_id == 12345
        assert cursor.last_ts_ms == 1700000000000
        assert cursor.updated_at_ms == 1700000001000

    def test_corrupt_json_returns_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        path.write_text("NOT VALID JSON")
        cursor = load_fill_cursor(str(path))
        assert cursor.last_trade_id == 0

    def test_missing_fields_default_to_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        path.write_text(json.dumps({"last_trade_id": 99}))
        cursor = load_fill_cursor(str(path))
        assert cursor.last_trade_id == 99
        assert cursor.last_ts_ms == 0
        assert cursor.updated_at_ms == 0

    def test_empty_json_object(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        path.write_text("{}")
        cursor = load_fill_cursor(str(path))
        assert cursor.last_trade_id == 0


class TestSaveFillCursor:
    """Tests for save_fill_cursor()."""

    def test_save_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        cursor = FillCursor(last_trade_id=42, last_ts_ms=1000)
        save_fill_cursor(str(path), cursor, now_ms=2000)
        assert path.exists()

        data = json.loads(path.read_text())
        assert data["last_trade_id"] == 42
        assert data["last_ts_ms"] == 1000
        assert data["updated_at_ms"] == 2000

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "cursor.json"
        cursor = FillCursor(last_trade_id=1)
        save_fill_cursor(str(path), cursor, now_ms=100)
        assert path.exists()

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        cursor1 = FillCursor(last_trade_id=10)
        save_fill_cursor(str(path), cursor1, now_ms=100)

        cursor2 = FillCursor(last_trade_id=20)
        save_fill_cursor(str(path), cursor2, now_ms=200)

        data = json.loads(path.read_text())
        assert data["last_trade_id"] == 20
        assert data["updated_at_ms"] == 200

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Save then load produces the same cursor."""
        path = str(tmp_path / "cursor.json")
        original = FillCursor(last_trade_id=999, last_ts_ms=5000)
        save_fill_cursor(path, original, now_ms=6000)

        loaded = load_fill_cursor(path)
        assert loaded.last_trade_id == original.last_trade_id
        assert loaded.last_ts_ms == original.last_ts_ms
        assert loaded.updated_at_ms == 6000
