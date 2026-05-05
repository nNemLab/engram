# Research

Self-hosted search stack: SearXNG + trafilatura + cross-encoder reranker.

## Bring up SearXNG

```bash
cd research/searxng

# 1. Generate a real secret key (one-time).
sed -i "s|REPLACE_ME_WITH_RANDOM_HEX_64|$(openssl rand -hex 32)|" settings.yml

# 2. Start.
docker compose up -d

# 3. Verify.
curl -s 'http://127.0.0.1:8888/search?q=test&format=json' | head -c 500
```

The container binds to `127.0.0.1:8888` only. Don't change that without
understanding the tradeoffs — exposing SearXNG to the LAN means every device
can use you as an open proxy for search engines, which the engines will notice
and rate-limit.

Logs:
```bash
docker compose logs -f
```

Stop:
```bash
docker compose down
```

## How the pipeline works

```
  query
    │
    ▼
  SearXNG  ── returns 20 candidate URLs ──▶
                                            ▼
                                    parallel fetch
                                    (httpx + trafilatura)
                                            │
                                            ▼
                                  cross-encoder rerank
                                  (ms-marco-MiniLM-L-6-v2)
                                            │
                                            ▼
                                  top-k + dedup
                                            │
                                            ▼
                                    return to caller
```

Pipeline lives in `src/engram/research/web.py`. Reranker in
`src/engram/research/rerank.py`. arXiv (separate vertical, no SearXNG
involvement) in `src/engram/research/arxiv.py`.

## arXiv

No infrastructure needed — uses the `arxiv` Python package directly against
arXiv's public API. Same reranker is applied to abstracts.

## Tuning

If specific engines (Google in particular) start returning 0 results, they're
captcha-blocking the SearXNG container. Either:
- disable that engine in `settings.yml` and rely on the others, or
- proxy SearXNG through a residential VPN (out of scope for this repo).

The reranker model is downloaded once to `~/.cache/huggingface/` on first use
(~80MB). Override the model in `~/.engram/config.yml` under `research.reranker_model`.
