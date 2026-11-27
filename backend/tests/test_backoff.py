"""Exponential backoff: 0.5s doubling, capped at 30s."""

import pytest

from app.db import backoff_delay


def test_default_sequence():
    delays = [backoff_delay(n, base=0.5, cap=30.0) for n in range(1, 10)]
    assert delays == [0.5, 1, 2, 4, 8, 16, 30, 30, 30]


def test_custom_base_and_cap():
    assert [backoff_delay(n, base=1.0, cap=5.0) for n in range(1, 6)] == [1, 2, 4, 5, 5]


def test_very_large_attempt_stays_at_cap():
    assert backoff_delay(10_000, base=0.5, cap=30.0) == 30.0


@pytest.mark.parametrize("attempt", [0, -1])
def test_attempt_must_be_positive(attempt):
    with pytest.raises(ValueError):
        backoff_delay(attempt, base=0.5, cap=30.0)
