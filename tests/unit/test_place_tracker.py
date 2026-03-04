"""Tests for P0-2 place correlation helper."""

from __future__ import annotations

from collections import deque

from grinder.live.place_tracker import CorrelationResult, correlate_recent_places


class TestCorrelateRecentPlaces:
    """Tests for correlate_recent_places pure function."""

    def test_all_found(self) -> None:
        recent: deque[tuple[str, int, str]] = deque(maxlen=20)
        recent.append(("grinder_d_BTCUSDT_1_1000_1", 1000, "BTCUSDT"))
        open_ids = {"grinder_d_BTCUSDT_1_1000_1"}
        result = correlate_recent_places(recent, open_ids, now_ms=2000)
        assert result == CorrelationResult(total=1, found=1, missing=0, missing_details=[])

    def test_all_missing(self) -> None:
        recent: deque[tuple[str, int, str]] = deque(maxlen=20)
        recent.append(("grinder_d_BTCUSDT_1_1000_1", 1000, "BTCUSDT"))
        recent.append(("grinder_d_BTCUSDT_2_1000_2", 2000, "BTCUSDT"))
        result = correlate_recent_places(recent, open_ids=set(), now_ms=5000)
        assert result.total == 2
        assert result.found == 0
        assert result.missing == 2
        assert len(result.missing_details) == 2
        assert "age=4000ms" in result.missing_details[0]
        assert "age=3000ms" in result.missing_details[1]

    def test_partial_match(self) -> None:
        recent: deque[tuple[str, int, str]] = deque(maxlen=20)
        recent.append(("id_a", 1000, "BTCUSDT"))
        recent.append(("id_b", 2000, "BTCUSDT"))
        result = correlate_recent_places(recent, open_ids={"id_a"}, now_ms=3000)
        assert result.found == 1
        assert result.missing == 1

    def test_empty_recent(self) -> None:
        recent: deque[tuple[str, int, str]] = deque(maxlen=20)
        result = correlate_recent_places(recent, open_ids=set(), now_ms=1000)
        assert result == CorrelationResult(total=0, found=0, missing=0, missing_details=[])

    def test_bounded_deque(self) -> None:
        recent: deque[tuple[str, int, str]] = deque(maxlen=20)
        for i in range(25):
            recent.append((f"id_{i}", i * 1000, "BTCUSDT"))
        assert len(recent) == 20
        assert recent[0][0] == "id_5"  # oldest 5 evicted
