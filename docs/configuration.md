# Configuration

Engram reads a single config file: `~/.engram/config.yml` (override the path
with `$ENGRAM_CONFIG`). Secrets live separately in `~/.engram/.env`. Both are
written by `bin/eos-init` on first run from the templates
[`config.example.yml`](../config.example.yml) and [`.env.example`](../.env.example).

## LLM provider (optional)

engram's core needs no LLM — the kernel reasons. Only the synthesis playbooks
(research synthesis, daily digest) call one, and only when configured. Point them
at any OpenAI-compatible endpoint via `~/.engram/.env`:

- `ENGRAM_LLM_BASE_URL` — e.g. `https://api.openai.com/v1`, `http://localhost:1234/v1`
  (LM Studio), or any vendor's OpenAI-compatible `/v1` base URL
- `ENGRAM_LLM_API_KEY`
- `ENGRAM_LLM_MODEL` — e.g. `gpt-4o-mini`

With none set, synthesis falls back to structural (non-LLM) output.

## Common knobs

| Key | Default | What it does |
|---|---|---|
| `paths.root` | `~/.engram` | Where the database, vault, venv, and `.env` live. |
| `rag.embed_model` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model. **384-dim constraint applies — see below.** |
| `research.reranker_model` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder reranker (freely swappable). |
| `research.searxng_url` | `http://127.0.0.1:8888` | Your SearXNG endpoint for web search. |
| `confidence.source_tier_weights` | `{}` | Per-source-class trust weights feeding the ranker. |
| `confidence.recency_half_life_days` | `365` | Half-life for recency decay; per-entry `ttl_days` overrides. |
| `playbooks.default_runtime` | `jupyter` | `jupyter` (scratch) or `marimo` (curated). |

## Embedding & reranker models

Retrieval runs two swappable models, both loaded through `sentence-transformers`:

- **Embedding model** (`rag.embed_model`) — encodes text into vectors for ANN
  search. Contract: `SentenceTransformer(name).encode(texts,
  normalize_embeddings=True) -> float32[]`.
- **Reranker** (`research.reranker_model`) — a cross-encoder that re-scores the
  top retrieved/searched candidates. Contract: `CrossEncoder(name).predict(
  [(query, passage), ...]) -> float[]`.

### CPU vs GPU is an install-time choice, not a config flag

There is no `device:` setting. `sentence-transformers` and `CrossEncoder`
auto-select **CUDA when the installed PyTorch is a CUDA build, otherwise CPU**.
So the lane is decided by which torch wheel lives in `~/.engram/.venv`:

- **CPU lane (default).** `bin/eos-init` installs CPU torch; everything runs on
  the CPU. Keep to small models. Typical latency: embedding ~10–40 ms/batch,
  reranking <200 ms for 10–30 candidates. No GPU, no driver, fully portable.
- **GPU lane (opt-in).** Reinstall a CUDA torch build matching your box into the
  venv; the same code then runs on the GPU automatically (reranking drops to
  <30 ms). This is what makes the larger embedders below practical.

The model you choose should match the lane — a 7B embedder on CPU is unusable; a
384-dim MiniLM on a GPU just wastes the card.

### ⚠️ The embedding dimension is fixed at 384

The vector table is declared `vec0(... embedding FLOAT[384])` in
`schema/001_initial.sql`. **Only another 384-dimension model is a config-only
swap.** Moving to a model with a different output dimension means editing the
schema's `FLOAT[NNN]`, adding a migration, and replaying the log from event 0 to
re-embed everything. Treat that as a migration, not a config tweak.

The reranker has no such constraint — a cross-encoder emits a scalar score — so
you can swap it freely at any time.

### Compatible embedding models

**Drop-in (384-dim, config-only):**

| Model | Lane | Notes |
|---|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | CPU | Current default. Fast, no prompt prefixes, solid baseline. |
| `BAAI/bge-small-en-v1.5` | CPU | Stronger retrieval than MiniLM; benefits from a query instruction but works without. |
| `thenlper/gte-small` | CPU | Strong small general model; no prefixes needed. |
| `Snowflake/snowflake-arctic-embed-s` | CPU | Retrieval-tuned, 384-dim. |
| `intfloat/e5-small-v2` | CPU | Good, but expects `query:` / `passage:` prefixes that Engram does not add. |

**Requires a schema dim change + full re-embed (and realistically a GPU):**

| Model | Dim | Notes |
|---|---|---|
| `BAAI/bge-large-en-v1.5` | 1024 | Strong English retrieval. |
| `thenlper/gte-large` | 1024 | General-purpose. |
| `mixedbread-ai/mxbai-embed-large-v1` | 1024 | Top-tier open English embedder. |
| `BAAI/bge-m3` | 1024 | Multilingual, long-context (8k tokens). |
| `Qwen/Qwen3-Embedding-0.6B` | 1024 | 2025 SOTA-class, multilingual; small enough for a modest GPU. |

The bigger `Qwen3-Embedding-4B/8B` and `nvidia/NV-Embed-v2` top the MTEB charts
but are GPU-only and overkill for a single-user KB.

### Compatible rerankers

Freely swappable (no dimension constraint):

| Model | Lane | Notes |
|---|---|---|
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | CPU | Current default. Fast, English, great speed/quality balance. |
| `cross-encoder/ms-marco-MiniLM-L-12-v2` | CPU | A notch more accurate, ~2× slower. |
| `BAAI/bge-reranker-base` | CPU / GPU | Higher quality; workable on CPU, snappier on GPU. |
| `BAAI/bge-reranker-v2-m3` | GPU | Strong and multilingual (100+ langs), Apache-2.0; heavier. |
| `jina-reranker-v2-base-multilingual` | GPU | Multilingual, 1k-token; needs `trust_remote_code=True`. |

LLM-style rerankers (`Qwen/Qwen3-Reranker-*`, `mixedbread-ai/mxbai-rerank-*-v2`)
lead the 2026 benchmarks but are **not plain `CrossEncoder` drop-ins** — they use
a different call path, so adopting one means changing `src/engram/research/
rerank.py`, not just a config value.

### Changing a model

1. Edit `rag.embed_model` or `research.reranker_model` in `~/.engram/config.yml`.
2. For a reranker, or a same-dimension (384) embedder, restart the daemons — the
   models lazy-load on next use.
3. For a different-dimension embedder, also bump the `FLOAT[NNN]` in the schema,
   add a migration, and replay the log so every entry is re-embedded.
