"""Embedding pipeline. Lazy-loads the model; produces normalized float32 vectors as bytes."""
from __future__ import annotations

import struct
import threading
from typing import Sequence

import numpy as np

from ..common.config import load_config


_lock = threading.Lock()
_model = None


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                cfg = load_config()
                _model = SentenceTransformer(cfg.rag.embed_model)
    return _model


def embed_one(text: str) -> bytes:
    return embed_many([text])[0]


def embed_many(texts: Sequence[str]) -> list[bytes]:
    model = _get_model()
    arr = model.encode(list(texts), normalize_embeddings=True, show_progress_bar=False)
    arr = np.asarray(arr, dtype=np.float32)
    return [_to_bytes(row) for row in arr]


def _to_bytes(row: np.ndarray) -> bytes:
    return struct.pack(f"{len(row)}f", *row.tolist())


def from_bytes(b: bytes) -> np.ndarray:
    n = len(b) // 4
    return np.array(struct.unpack(f"{n}f", b), dtype=np.float32)
