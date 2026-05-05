"""Tool registry. Each module exposes register(conn) -> dict[str, ToolSpec]."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from . import kb, rag, research, playbook, goals


@dataclass
class ToolSpec:
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]


def build_registry(conn: sqlite3.Connection) -> dict[str, ToolSpec]:
    registry: dict[str, ToolSpec] = {}
    for mod in (kb, rag, research, playbook, goals):
        for name, spec in mod.register(conn).items():
            registry[name] = ToolSpec(**spec) if isinstance(spec, dict) else spec
    return registry
