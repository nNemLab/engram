"""Adapter protocol, Candidate dataclass, ADAPTERS registry, glob filter."""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

# ----- Candidate ---------------------------------------------------------

@dataclass
class Candidate:
    """One ingestable item produced by an adapter for the dedup gate."""
    source_url: str
    body: str
    title: str | None = None


# ----- Adapter protocol --------------------------------------------------

class Adapter(Protocol):
    """An adapter polls one source and yields Candidates.

    The adapter reads source['cursor'] (a JSON string) for its incremental
    state, and is expected to return updated cursor data via the source row
    (the poller writes it back atomically at end of run).
    """
    name: str

    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        ...


# ----- Glob filter helper ------------------------------------------------

def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a shell glob (with * / ? / **) to a regex.

    **/  (double-star followed by slash) -> (?:.*/)?   — zero or more full segments
    **   (trailing double-star)           -> .*          — anything
    *                                -> [^/]*           — within one segment
    ?                                -> [^/]            — one non-slash char
    any other char                   -> regex-escaped   — literal match
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        if pattern[i] == "*" and i + 1 < n and pattern[i + 1] == "*":
            if i + 2 < n and pattern[i + 2] == "/":
                out.append("(?:.*/)?")
                i += 3  # skip "**/"
            else:
                out.append(".*")
                i += 2  # trailing "**"
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches_globs(path: str, include: list[str], exclude: list[str]) -> bool:
    """True if path matches any include and no exclude. Empty include = wildcard."""
    if exclude and any(_glob_to_regex(p).match(path) for p in exclude):
        return False
    if not include:
        return True
    return any(_glob_to_regex(p).match(path) for p in include)


# ----- Registry (populated as adapters import) ---------------------------

ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    ADAPTERS[adapter.name] = adapter


# Eager-import adapters so they self-register into ADAPTERS on package import.
# Placed at module bottom so register() is defined before adapter modules call it.
from . import github_repo as _github_repo  # noqa: E402, F401
from . import mediawiki_api as _mediawiki_api  # noqa: E402, F401
from . import sitemap as _sitemap  # noqa: E402, F401
from . import urls as _urls  # noqa: E402, F401
