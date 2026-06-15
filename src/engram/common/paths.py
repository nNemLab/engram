"""Path resolution. All paths flow from config; no hardcoded locations."""
from __future__ import annotations

import os
from pathlib import Path


def expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p)))).resolve()
