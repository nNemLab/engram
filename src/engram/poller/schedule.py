"""Duration-string parser for source schedules.

Grammar: <int><unit> where unit ∈ {s,m,h,d,w}. Examples: 30m, 6h, 1d, 7d, 2w.
"""
from __future__ import annotations

import re
from datetime import timedelta

_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}
_PATTERN = re.compile(r"^(\d+)([smhdw])$")


def parse_interval(s: str) -> timedelta:
    if not isinstance(s, str):
        raise ValueError(f"schedule must be str, got {type(s).__name__}")
    m = _PATTERN.match(s.strip())
    if not m:
        raise ValueError(f"invalid schedule {s!r}; expected like '7d', '6h', '30m'")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError(f"schedule duration must be positive: {s!r}")
    return timedelta(**{_UNITS[unit]: n})
