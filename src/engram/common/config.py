"""Config loading. Single source: ~/.engram/config.yml (override via $ENGRAM_CONFIG)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
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


@lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> Config:
    p = path or _resolve_config_path()
    if not p.exists():
        raise FileNotFoundError(
            f"Config not found at {p}. Copy config.example.yml to {p} and edit."
        )
    raw = yaml.safe_load(p.read_text())
    pp = raw["paths"]
    paths = Paths(
        root=expand(pp["root"]),
        vault=expand(pp["vault"]),
        playbooks_scratch=expand(pp["playbooks_scratch"]),
        playbooks_curated=expand(pp["playbooks_curated"]),
        playbooks_runs=expand(pp["playbooks_runs"]),
        db=expand(pp["db"]),
    )
    return Config(
        paths=paths,
        rag=RagConfig(**raw.get("rag", {})),
        confidence=ConfidenceConfig(**raw.get("confidence", {})),
        projector=ProjectorConfig(**raw.get("projector", {})),
        watcher=WatcherConfig(**raw.get("watcher", {})),
        reactor=ReactorConfig(**raw.get("reactor", {})),
        playbooks=PlaybookConfig(**raw.get("playbooks", {})),
        research=ResearchConfig(**raw.get("research", {})),
        grounding=GroundingConfig(**raw.get("grounding", {})),
    )
