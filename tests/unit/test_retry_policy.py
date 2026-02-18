"""Tests for grinder.net.retry_policy (Launch-05 PR1).

Covers:
- HttpRetryPolicy: defaults, validation, backoff determinism, jitter=False
- DeadlinePolicy: per-op budgets, defaults, fallback
- classify_http_error: all reason categories
- is_http_retryable: read vs write retryable matrix
- Ops taxonomy: WRITE_OPS, READ_OPS, ALL_OPS consistency
"""

from __future__ import annotations

import pytest

from grinder.net.retry_policy import (
    ALL_OPS,
    READ_OPS,
    REASON_4XX,
    REASON_5XX,
    REASON_429,
    REASON_CONNECT,
    REASON_DECODE,
    REASON_DNS,
    REASON_TIMEOUT,
    REASON_TLS,
    REASON_UNKNOWN,
    WRITE_OPS,
    DeadlinePolicy,
    HttpRetryPolicy,
    classify_http_error,
    is_http_retryable,
)

# ---------------------------------------------------------------------------
# HttpRetryPolicy
# ---------------------------------------------------------------------------


class TestHttpRetryPolicy:
    def test_defaults(self) -> None:
        p = HttpRetryPolicy()
        assert p.max_attempts == 1
        assert p.base_delay_ms == 100
        assert p.max_delay_ms == 500
        assert p.jitter is False

    def test_validation_max_attempts(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            HttpRetryPolicy(max_attempts=0)

    def test_validation_base_delay(self) -> None:
        with pytest.raises(ValueError, match="base_delay_ms"):
            HttpRetryPolicy(base_delay_ms=-1)

    def test_validation_max_delay(self) -> None:
        with pytest.raises(ValueError, match="max_delay_ms"):
            HttpRetryPolicy(base_delay_ms=200, max_delay_ms=100)

    def test_backoff_deterministic_no_jitter(self) -> None:
        p = HttpRetryPolicy(base_delay_ms=100, max_delay_ms=1000, backoff_multiplier=2.0)
        assert p.compute_delay_ms(0) == 100
        assert p.compute_delay_ms(1) == 200
        assert p.compute_delay_ms(2) == 400
        assert p.compute_delay_ms(3) == 800

    def test_backoff_capped_at_max(self) -> None:
        p = HttpRetryPolicy(base_delay_ms=100, max_delay_ms=300, backoff_multiplier=2.0)
        assert p.compute_delay_ms(0) == 100
        assert p.compute_delay_ms(1) == 200
        assert p.compute_delay_ms(2) == 300  # capped
        assert p.compute_delay_ms(10) == 300  # still capped

    def test_for_read(self) -> None:
        p = HttpRetryPolicy.for_read(max_attempts=3)
        assert p.max_attempts == 3
        assert REASON_429 in p.retryable_reasons
        assert REASON_TIMEOUT in p.retryable_reasons

    def test_for_write(self) -> None:
        p = HttpRetryPolicy.for_write(max_attempts=2)
        assert p.max_attempts == 2
        assert REASON_429 not in p.retryable_reasons  # conservative
        assert REASON_TIMEOUT in p.retryable_reasons

    def test_frozen(self) -> None:
        p = HttpRetryPolicy()
        with pytest.raises(AttributeError):
            p.max_attempts = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DeadlinePolicy
# ---------------------------------------------------------------------------


class TestDeadlinePolicy:
    def test_defaults(self) -> None:
        d = DeadlinePolicy.defaults()
        assert d.get_deadline_ms("cancel_order") == 600
        assert d.get_deadline_ms("place_order") == 1500
        assert d.get_deadline_ms("exchange_info") == 5000

    def test_all_ops_have_deadlines(self) -> None:
        d = DeadlinePolicy.defaults()
        for op in ALL_OPS:
            deadline = d.get_deadline_ms(op)
            assert deadline > 0, f"Missing deadline for op={op}"

    def test_unknown_op_fallback(self) -> None:
        d = DeadlinePolicy.defaults()
        assert d.get_deadline_ms("unknown_op") == 5000

    def test_get_deadline_s(self) -> None:
        d = DeadlinePolicy.defaults()
        assert d.get_deadline_s("cancel_order") == 0.6

    def test_custom_deadlines(self) -> None:
        d = DeadlinePolicy(deadlines={"cancel_order": 300})
        assert d.get_deadline_ms("cancel_order") == 300

    def test_frozen(self) -> None:
        d = DeadlinePolicy.defaults()
        with pytest.raises(AttributeError):
            d.deadlines = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# classify_http_error
# ---------------------------------------------------------------------------


class TestClassifyHttpError:
    def test_status_429(self) -> None:
        assert classify_http_error(status_code=429) == REASON_429

    def test_status_500(self) -> None:
        assert classify_http_error(status_code=500) == REASON_5XX

    def test_status_503(self) -> None:
        assert classify_http_error(status_code=503) == REASON_5XX

    def test_status_400(self) -> None:
        assert classify_http_error(status_code=400) == REASON_4XX

    def test_status_403(self) -> None:
        assert classify_http_error(status_code=403) == REASON_4XX

    def test_timeout_error(self) -> None:
        assert classify_http_error(error=TimeoutError("timed out")) == REASON_TIMEOUT

    def test_connect_error(self) -> None:
        assert classify_http_error(error=ConnectionError("refused")) == REASON_CONNECT

    def test_dns_error(self) -> None:
        assert classify_http_error(error=OSError("dns resolution failed")) == REASON_DNS

    def test_tls_error(self) -> None:
        assert classify_http_error(error=OSError("ssl handshake failed")) == REASON_TLS

    def test_decode_error(self) -> None:
        assert classify_http_error(error=ValueError("json decode error")) == REASON_DECODE

    def test_unknown_error(self) -> None:
        assert classify_http_error(error=RuntimeError("something")) == REASON_UNKNOWN

    def test_no_args(self) -> None:
        assert classify_http_error() == REASON_UNKNOWN

    def test_status_takes_priority(self) -> None:
        # When both status and error are provided, status wins
        assert classify_http_error(status_code=429, error=TimeoutError()) == REASON_429


# ---------------------------------------------------------------------------
# is_http_retryable
# ---------------------------------------------------------------------------


class TestIsHttpRetryable:
    def test_read_timeout_retryable(self) -> None:
        p = HttpRetryPolicy.for_read(max_attempts=3)
        assert is_http_retryable(REASON_TIMEOUT, p) is True

    def test_read_connect_retryable(self) -> None:
        p = HttpRetryPolicy.for_read(max_attempts=3)
        assert is_http_retryable(REASON_CONNECT, p) is True

    def test_read_5xx_retryable(self) -> None:
        p = HttpRetryPolicy.for_read(max_attempts=3)
        assert is_http_retryable(REASON_5XX, p) is True

    def test_read_429_retryable(self) -> None:
        p = HttpRetryPolicy.for_read(max_attempts=3)
        assert is_http_retryable(REASON_429, p) is True

    def test_read_4xx_not_retryable(self) -> None:
        p = HttpRetryPolicy.for_read(max_attempts=3)
        assert is_http_retryable(REASON_4XX, p) is False

    def test_write_timeout_retryable(self) -> None:
        p = HttpRetryPolicy.for_write(max_attempts=2)
        assert is_http_retryable(REASON_TIMEOUT, p) is True

    def test_write_429_not_retryable(self) -> None:
        p = HttpRetryPolicy.for_write(max_attempts=2)
        assert is_http_retryable(REASON_429, p) is False

    def test_write_4xx_not_retryable(self) -> None:
        p = HttpRetryPolicy.for_write(max_attempts=2)
        assert is_http_retryable(REASON_4XX, p) is False

    def test_unknown_not_retryable(self) -> None:
        p = HttpRetryPolicy.for_read(max_attempts=3)
        assert is_http_retryable(REASON_UNKNOWN, p) is False


# ---------------------------------------------------------------------------
# Ops taxonomy
# ---------------------------------------------------------------------------


class TestOpsTaxonomy:
    def test_write_and_read_disjoint(self) -> None:
        assert frozenset() == WRITE_OPS & READ_OPS

    def test_all_is_union(self) -> None:
        assert ALL_OPS == WRITE_OPS | READ_OPS

    def test_all_ops_are_strings(self) -> None:
        for op in ALL_OPS:
            assert isinstance(op, str)
            assert "_" in op  # snake_case

    def test_expected_write_ops(self) -> None:
        assert "place_order" in WRITE_OPS
        assert "cancel_order" in WRITE_OPS
        assert "cancel_all" in WRITE_OPS

    def test_expected_read_ops(self) -> None:
        assert "get_positions" in READ_OPS
        assert "get_account" in READ_OPS
        assert "exchange_info" in READ_OPS
