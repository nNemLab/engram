"""FTS5 query sanitisation (#63): raw special characters must not crash retrieval."""
import pytest

from engram.rag.query import _bm25_hits, _fts_match_expr
from tests.rag import fresh_conn
from tests.rag.test_query_calibrated import _add

# Representative of every failure class the raw MATCH path crashed on.
GAUNTLET = [
    "where does it run?", "?", "??", "1 + 1 = 2?", "what is X? and why",
    'unbalanced "quote', '"balanced phrase"', '"',
    "prefix*", "*leadstar", "mid*star", "**",
    "(unbalanced", "balanced (group)", ")close", "()",
    "foo OR bar", "AND", "OR", "NOT", "NEAR(foo bar)",
    "col:foo", "foo-bar", "-leadingminus", "^initial",
    "100%", "c++ programming", "a & b", "#hashtag", "@mention",
    "it's fine", "semi;colon", "a.b.c", "<script>alert</script>",
    "emoji rocket", "{brace}", "[bracket]", "back\\slash", "pipe|pipe",
    "equals=sign", "tilde~", "dollar$", "   ", "",
]


@pytest.mark.parametrize("raw,expected", [
    ("foo bar", '"foo" "bar"'),
    ("it's fine", '"it" "s" "fine"'),
    ("foo-bar", '"foo" "bar"'),
    ("where does it run?", '"where" "does" "it" "run"'),
    ("café résumé", '"café" "résumé"'),
    ("", None),
    ("   ", None),
    ("?!.", None),
    ("()", None),
])
def test_fts_match_expr(raw, expected):
    assert _fts_match_expr(raw) == expected


def test_bm25_hits_survives_gauntlet(tmp_path):
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Dev instance", "the engram dev instance runs from source on the CPU lane")
    for q in GAUNTLET:
        out = _bm25_hits(conn, q, 5)   # must never raise, whatever the characters
        assert isinstance(out, list)


def test_bm25_matches_question_form(tmp_path):
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Dev instance", "the engram dev instance runs from source on the CPU lane")
    # All query tokens are present in the doc, so it matches; the trailing `?`
    # (which used to raise a syntax error) is now stripped, not fatal.
    assert _bm25_hits(conn, "engram dev instance", 5)
    assert _bm25_hits(conn, "engram dev instance?", 5)
    # Empty / punctuation-only -> no FTS hits, no crash.
    assert _bm25_hits(conn, "???", 5) == []
    assert _bm25_hits(conn, "", 5) == []
