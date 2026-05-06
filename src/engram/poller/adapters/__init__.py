"""Adapter protocol, Candidate dataclass, ADAPTERS registry, glob filter."""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


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
    """Translate a shell glob (with ** for any-depth) to a regex.

    fnmatch.translate handles single * and ? but treats ** identically to *.
    We pre-substitute a sentinel for ** so multi-segment matches work.
    """
    SENTINEL = "\x00DOUBLESTAR\x00"
    pat = pattern.replace("**", SENTINEL)
    regex = fnmatch.translate(pat)
    regex = regex.replace(re.escape(SENTINEL), ".*")
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
from . import sitemap as _sitemap  # noqa: E402, F401
from . import github_repo as _github_repo  # noqa: E402, F401
