"""Tests for vibebuild._retry."""
from __future__ import annotations

import pytest

from vibebuild._retry import is_transient, with_retry


class TestIsTransient:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("connection reset by peer", True),
            ("operation timed out", True),
            ("503 Service Unavailable", True),
            ("temporarily unavailable", True),
            ("SSL: handshake failure", True),
            ("invalid SRPM format", False),
            ("permission denied", False),
            ("no such tag: f99", False),
            (None, False),
        ],
    )
    def test_is_transient(self, text, expected):
        assert is_transient(text) is expected


class TestWithRetry:
    def test_returns_value_on_first_success(self):
        calls = []

        @with_retry(attempts=3, initial_delay=0)
        def fn():
            calls.append(1)
            return 42

        assert fn() == 42
        assert len(calls) == 1

    def test_retries_on_transient_then_succeeds(self):
        calls = []

        @with_retry(attempts=3, initial_delay=0)
        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("connection refused")
            return "ok"

        assert fn() == "ok"
        assert len(calls) == 3

    def test_does_not_retry_non_transient(self):
        calls = []

        @with_retry(attempts=3, initial_delay=0)
        def fn():
            calls.append(1)
            raise ValueError("bad spec format")

        with pytest.raises(ValueError):
            fn()
        assert len(calls) == 1

    def test_raises_after_max_attempts(self):
        calls = []

        @with_retry(attempts=3, initial_delay=0)
        def fn():
            calls.append(1)
            raise TimeoutError("operation timed out")

        with pytest.raises(TimeoutError):
            fn()
        assert len(calls) == 3
