"""arXiv vertical search. Direct API call via the `arxiv` package, optional rerank.

No infrastructure beyond the public arXiv API. Returns title + abstract + PDF
URL — pass the PDF URL through `url-ingest` (or a dedicated `paper-ingest`
playbook later) to get the full text into the KB.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import rerank


@dataclass
class ArxivResult:
    arxiv_id: str
    title: str
    abstract: str
    authors: list[str]
    published: str            # ISO date
    pdf_url: str
    abs_url: str
    score: float


def search(query: str, *, k: int = 5, fetch_multiplier: int = 3,
           do_rerank: bool = True, quote_phrase: bool = True) -> list[ArxivResult]:
    """Search arXiv with optional cross-encoder rerank.

    `quote_phrase=True` (default) wraps multi-word queries in quotes so arXiv
    treats the query as an exact phrase rather than a bag of OR'd terms. This
    dramatically improves precision for niche concepts ('reciprocal rank fusion'
    matches RRF papers, not arbitrary 'fusion' papers). Set False for broad
    discovery searches where keyword OR-matching is preferred.
    """
    import arxiv  # imported lazily — small dep, no infra

    effective_query = query
    has_field_prefix = any(prefix in query for prefix in ("au:", "ti:", "abs:", "cat:"))
    if quote_phrase and len(query.split()) > 1 and '"' not in query and not has_field_prefix:
        effective_query = f'"{query}"'

    # arXiv enforces ~3s between requests for the public API; below that → 429.
    client = arxiv.Client(page_size=k * fetch_multiplier, delay_seconds=3.0, num_retries=3)
    search_query = arxiv.Search(
        query=effective_query,
        max_results=k * fetch_multiplier,
        sort_by=arxiv.SortCriterion.Relevance,
    )

    results: list[ArxivResult] = []
    try:
        for r in client.results(search_query):
            try:
                published = r.published.isoformat() if isinstance(r.published, datetime) else str(r.published)
                results.append(ArxivResult(
                    arxiv_id=r.entry_id.rsplit("/", 1)[-1],
                    title=r.title.strip().replace("\n", " "),
                    abstract=(r.summary or "").strip().replace("\n", " "),
                    authors=[a.name for a in r.authors],
                    published=published,
                    pdf_url=r.pdf_url,
                    abs_url=r.entry_id,
                    score=0.0,
                ))
            except Exception:
                continue
    except Exception as e:
        if not results:
            raise RuntimeError(f"arXiv search failed for query {query!r}: {e}") from e

    if not results:
        return []

    if do_rerank:
        passages = [f"{r.title}\n\n{r.abstract}" for r in results]
        scores = rerank.score(query, passages)
        for r, s in zip(results, scores):
            r.score = s
        results.sort(key=lambda r: r.score, reverse=True)
    else:
        # Use arXiv-native order (relevance-sorted by API) and synthesize a
        # descending pseudo-score so the API contract stays the same.
        for i, r in enumerate(results):
            r.score = 1.0 - (i / max(1, len(results)))

    return results[:k]
