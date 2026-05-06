from datetime import timedelta

import pytest

from engram.poller.schedule import parse_interval


def test_minutes():
    assert parse_interval("30m") == timedelta(minutes=30)


def test_hours():
    assert parse_interval("6h") == timedelta(hours=6)


def test_days():
    assert parse_interval("1d") == timedelta(days=1)
    assert parse_interval("7d") == timedelta(days=7)


def test_weeks():
    assert parse_interval("2w") == timedelta(weeks=2)


def test_seconds():
    assert parse_interval("90s") == timedelta(seconds=90)


@pytest.mark.parametrize("bad", ["", "1", "x", "1y", "  ", "1d2h", "-1d"])
def test_invalid_raises(bad):
    with pytest.raises(ValueError):
        parse_interval(bad)
