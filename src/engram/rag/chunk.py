"""Markdown-aware chunking. Splits on headings; falls back to sliding token windows."""
from __future__ import annotations

import re
from dataclasses import dataclass


_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
# Approx: ~4 chars per token. Cheap heuristic, no tokenizer dep.
_CHARS_PER_TOKEN = 4


@dataclass
class Chunk:
    text: str
    heading_path: list[str]
    start: int
    end: int


def _split_on_headings(text: str) -> list[tuple[list[str], str]]:
    """Return list of (heading_path, body) tuples. Heading path tracks nested context."""
    parts: list[tuple[list[str], str]] = []
    stack: list[str] = []
    last_end = 0
    last_path: list[str] = []
    for m in _HEADING.finditer(text):
        depth = len(m.group(1))
        title = m.group(2).strip()
        body = text[last_end:m.start()].strip()
        if body:
            parts.append((list(last_path), body))
        stack = stack[: depth - 1] + [title]
        last_path = list(stack)
        last_end = m.end()
    tail = text[last_end:].strip()
    if tail:
        parts.append((list(last_path), tail))
    if not parts and text.strip():
        parts.append(([], text.strip()))
    return parts


def _window(text: str, size_tokens: int, overlap_tokens: int) -> list[tuple[int, int, str]]:
    size = size_tokens * _CHARS_PER_TOKEN
    overlap = overlap_tokens * _CHARS_PER_TOKEN
    step = max(1, size - overlap)
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(text):
        j = min(len(text), i + size)
        out.append((i, j, text[i:j]))
        if j >= len(text):
            break
        i += step
    return out


def chunk_markdown(text: str, size_tokens: int = 512, overlap_tokens: int = 64) -> list[Chunk]:
    chunks: list[Chunk] = []
    sections = _split_on_headings(text)
    cursor = 0
    for path, body in sections:
        # Find body's offset in the original text, for stable start/end fields.
        idx = text.find(body, cursor)
        if idx < 0:
            idx = cursor
        cursor = idx + len(body)
        if len(body) <= size_tokens * _CHARS_PER_TOKEN:
            chunks.append(Chunk(text=body, heading_path=path, start=idx, end=idx + len(body)))
        else:
            for s, e, sub in _window(body, size_tokens, overlap_tokens):
                chunks.append(Chunk(text=sub, heading_path=path, start=idx + s, end=idx + e))
    return chunks
