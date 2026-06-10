"""Shared UTC timestamp helper: format and precision."""
import re

import pytest

from engram.common.time import utcnow_iso

MS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
S_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_default_is_millisecond():
    assert MS_RE.match(utcnow_iso())
    assert MS_RE.match(utcnow_iso("ms"))


def test_second_precision():
    assert S_RE.match(utcnow_iso("s"))
    # whole-second form must not carry a fractional part
    assert "." not in utcnow_iso("s")


def test_unknown_precision_raises():
    with pytest.raises(ValueError):
        utcnow_iso("us")
