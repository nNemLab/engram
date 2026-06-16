"""Config loading. Single source: ~/.engram/config.yml (override via $ENGRAM_CONFIG)."""
from __future__ import annotations

import os
from dataclasses import MISSING, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .paths import expand

DEFAULT_CONFIG_PATH = Path("~/.engram/config.yml")


@dataclass
class Paths:
    root: Path
    vault: Path
    playbooks_scratch: Path
    playbooks_curated: Path
    playbooks_runs: Path
    db: Path


@dataclass
class RagConfig:
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embed_dim: int = 384
    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64
    top_k: int = 12
    rrf_k: int = 60
    near_dup_threshold: float = 0.92

    def __post_init__(self) -> None:
        """Validate embed_dim before it reaches the vec0 DDL f-string."""
        dim = self.embed_dim
        if not isinstance(dim, int) or isinstance(dim, bool):
            raise ValueError(
                f"rag.embed_dim must be a positive integer, got {dim!r} "
                f"(type {type(dim).__name__})."
            )
        if dim <= 0:
            raise ValueError(
                f"rag.embed_dim must be a positive integer, got {dim}."
            )
        if dim > 8192:
            raise ValueError(
                f"rag.embed_dim must be <= 8192, got {dim}."
            )


@dataclass
class ConfidenceConfig:
    source_tier_weights: dict[str, float] = field(default_factory=dict)
    recency_half_life_days: int = 365
    recency_score_enabled: bool = False
    recency_score_weight: float = 0.2
    recency_score_half_life_days: int = 30


@dataclass
class ProjectorConfig:
    poll_interval: int = 5
    kind_dirs: dict[str, str] = field(default_factory=dict)


@dataclass
class WatcherConfig:
    debounce_ms: int = 800
    ignore: list[str] = field(default_factory=list)


@dataclass
class ReactorConfig:
    embed_workers: int = 1
    retrieval_staleness_threshold: float = 0.8


@dataclass
class PlaybookConfig:
    default_runtime: str = "jupyter"
    jupyter: dict[str, Any] = field(default_factory=dict)
    marimo: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResearchConfig:
    searxng_url: str = "http://127.0.0.1:8888"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    web_default_k: int = 8
    web_max_candidates: int = 20
    arxiv_enabled: bool = True
    arxiv_default_k: int = 5


@dataclass
class GroundingConfig:
    tau_high: float = 0.62      # dense-cosine floor for STRONG
    tau_low: float = 0.45       # dense-cosine floor for WEAK (below = NONE)
    delta: float = 0.08         # min top-1 vs top-2 margin for STRONG
    token_budget: int = 1500    # default packed-injection budget
    port: int = 8770            # grounding daemon (Phase 2) loopback port
    usage_weight: float = 0.5   # weight of the usage term in ranking


@dataclass
class Config:
    paths: Paths
    rag: RagConfig
    confidence: ConfidenceConfig
    projector: ProjectorConfig
    watcher: WatcherConfig
    reactor: ReactorConfig
    playbooks: PlaybookConfig
    research: ResearchConfig
    grounding: GroundingConfig

    @property
    def vault(self) -> Path: return self.paths.vault

    @property
    def db_path(self) -> Path: return self.paths.db


def _resolve_config_path() -> Path:
    return expand(os.environ.get("ENGRAM_CONFIG", str(DEFAULT_CONFIG_PATH)))


def _expand_no_resolve(p: str | Path) -> Path:
    """~- and $VAR-expand a path WITHOUT resolving it against the CWD."""
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def _resolve_under_root(p: str | Path, root: Path) -> Path:
    """Resolve a config path deterministically.

    An absolute path (after ``~``/``$VAR`` expansion) is used as-is. A *relative*
    path is anchored to ``paths.root`` rather than the launching process's CWD,
    so every daemon resolves e.g. a relative ``db:`` to the same file regardless
    of where it was started.
    """
    expanded = _expand_no_resolve(p)
    if not expanded.is_absolute():
        expanded = root / expanded
    return expanded.resolve()


def _section(raw: dict[str, Any], name: str, path: Path, cls: type) -> dict[str, Any]:
    """Return a validated mapping for one config section.

    * A missing section, or one present but empty (``name:`` with nothing under
      it -> YAML ``None``), yields ``{}`` so the dataclass defaults apply
      instead of ``cls(**None)`` blowing up.
    * A section present but not a mapping is a config error, not a silent
      coercion.
    * Every provided key is checked against the target dataclass's fields
      *before* construction, so a typo'd/unknown key fails loudly naming the
      file, section, and key rather than raising an opaque ``TypeError``.
    """
    data = raw.get(name)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Section '{name}' in {path} must be a mapping, got {type(data).__name__}."
        )
    valid = {f.name for f in fields(cls)}
    for key in data:
        if key not in valid:
            raise ValueError(
                f"Unknown key '{key}' in section '{name}' of {path}. "
                f"Valid keys for '{name}': {sorted(valid)}."
            )
    return data


@lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> Config:
    p = path or _resolve_config_path()
    if not p.exists():
        raise FileNotFoundError(
            f"Config not found at {p}. Copy config.example.yml to {p} and edit."
        )
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Config {p} must be a YAML mapping at the top level, got {type(raw).__name__}."
        )

    if not raw.get("paths"):
        raise ValueError(f"Config {p} is missing the required 'paths' section.")
    pp = _section(raw, "paths", p, Paths)
    required = [
        f.name for f in fields(Paths)
        if f.default is MISSING and f.default_factory is MISSING
    ]
    missing = [k for k in required if k not in pp]
    if missing:
        raise ValueError(
            f"Section 'paths' in {p} is missing required key(s): {missing}."
        )

    # root anchors every relative path; expand it (and resolve against CWD only
    # if root itself is given relative -- there is nothing else to anchor to).
    root = expand(pp["root"])
    paths = Paths(
        root=root,
        vault=_resolve_under_root(pp["vault"], root),
        playbooks_scratch=_resolve_under_root(pp["playbooks_scratch"], root),
        playbooks_curated=_resolve_under_root(pp["playbooks_curated"], root),
        playbooks_runs=_resolve_under_root(pp["playbooks_runs"], root),
        db=_resolve_under_root(pp["db"], root),
    )
    return Config(
        paths=paths,
        rag=RagConfig(**_section(raw, "rag", p, RagConfig)),
        confidence=ConfidenceConfig(**_section(raw, "confidence", p, ConfidenceConfig)),
        projector=ProjectorConfig(**_section(raw, "projector", p, ProjectorConfig)),
        watcher=WatcherConfig(**_section(raw, "watcher", p, WatcherConfig)),
        reactor=ReactorConfig(**_section(raw, "reactor", p, ReactorConfig)),
        playbooks=PlaybookConfig(**_section(raw, "playbooks", p, PlaybookConfig)),
        research=ResearchConfig(**_section(raw, "research", p, ResearchConfig)),
        grounding=GroundingConfig(**_section(raw, "grounding", p, GroundingConfig)),
    )
