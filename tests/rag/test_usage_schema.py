from tests.rag import fresh_conn


def test_content_usage_table_exists(tmp_path):
    conn = fresh_conn(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(content_usage)")}
    assert {"content_hash", "use_count", "last_cited_at"} <= cols
