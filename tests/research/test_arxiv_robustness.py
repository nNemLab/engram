import sys
from types import SimpleNamespace

import pytest

from engram.research import arxiv


def test_search_skips_bad_result_entries(monkeypatch, caplog):
    class FakeClient:
        def results(self, _query):
            yield SimpleNamespace(
                entry_id="http://arxiv.org/abs/1234.5678",
                title="Good paper",
                summary="Good abstract",
                authors=[SimpleNamespace(name="Alice")],
                published="2024-01-01",
                pdf_url="http://arxiv.org/pdf/1234.5678.pdf",
            )
            yield SimpleNamespace(
                entry_id="http://arxiv.org/abs/bad",
                title="Bad paper",
                summary="Bad abstract",
                authors=None,
                published="2024-01-02",
                pdf_url="http://arxiv.org/pdf/bad.pdf",
            )

    class FakeSearch:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeArxiv:
        Client = lambda self, **kwargs: FakeClient()  # noqa: E731
        Search = FakeSearch

        class SortCriterion:
            Relevance = object()

    monkeypatch.setitem(sys.modules, "arxiv", FakeArxiv())

    results = arxiv.search("query", k=5, do_rerank=False, quote_phrase=False)

    assert len(results) == 1
    assert results[0].arxiv_id == "1234.5678"
    assert any("dropping malformed arXiv entry" in rec.message for rec in caplog.records)


def test_search_does_not_quote_field_prefixed_query(monkeypatch):
    captured: dict[str, str] = {}

    class FakeClient:
        def results(self, _query):
            return iter([])

    class FakeSearch:
        def __init__(self, **kwargs):
            captured["query"] = kwargs["query"]

    class FakeArxiv:
        Client = lambda self, **kwargs: FakeClient()  # noqa: E731
        Search = FakeSearch

        class SortCriterion:
            Relevance = object()

    monkeypatch.setitem(sys.modules, "arxiv", FakeArxiv())

    arxiv.search("ti:graph neural networks", k=5, do_rerank=False, quote_phrase=True)

    assert captured["query"] == "ti:graph neural networks"


def test_search_wraps_top_level_arxiv_iteration_error(monkeypatch):
    class FakeClient:
        def results(self, _query):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    class FakeSearch:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeArxiv:
        Client = lambda self, **kwargs: FakeClient()  # noqa: E731
        Search = FakeSearch

        class SortCriterion:
            Relevance = object()

    monkeypatch.setitem(sys.modules, "arxiv", FakeArxiv())

    with pytest.raises(RuntimeError, match="arXiv search failed"):
        arxiv.search("query", k=5, do_rerank=False, quote_phrase=False)
