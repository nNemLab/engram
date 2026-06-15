"""Adapter protocol, Candidate dataclass, ADAPTERS registry, glob filter."""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

# ----- Candidate ---------------------------------------------------------

@dataclass
class Candidate:
    """One ingestable item produced by an adapter for the dedup gate."""
    source_url: str
    body: str
    title: str | None = None
    fetched_at: str | None = None
    metadata: dict = field(default_factory=dict)


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

    *  matches within one path segment  →  [^/]*
    ** matches across any number of segments (including zero)  →  .*
    ?  matches a single character  →  [^/]  (single-char, no slash)
    Everything else is literal-escaped.
    """
    segments = pattern.split("/")
    regex_parts: list[str] = []
    for i, seg in enumerate(segments):
        parts: list[str] = []
        j = 0
        was_double_star = False
        while j < len(seg):
            if seg[j] == "*":
                if j + 1 < len(seg) and seg[j + 1] == "*":
                    parts.append(".*")
                    j += 2
                    was_double_star = True
                else:
                    parts.append("[^/]*")
                    j += 1
                    was_double_star = False
            elif seg[j] == "?":
                parts.append("[^/]")
                j += 1
                was_double_star = False
            else:
                parts.append(re.escape(seg[j]))
                j += 1
                was_double_star = False
        regex_parts.append("".join(parts))
        # Only add a literal '/' between segments if the last part wasn't "**"
        # — "**" is zero-or-more-segments and its ".*" already spans slashes.
        if i < len(segments) - 1 and not was_double_star:
            regex_parts.append("/")
    regex = "^" + "".join(regex_parts) + "$"
    return re.compile(regex)


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
