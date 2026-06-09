"""Generate starter playbooks in ~/.engram/playbooks/scratch/.

Produces:
  - url-ingest.ipynb       fetch URL → trafilatura → kb.write(kind=research)
  - vault-audit.ipynb      surface stale, low-confidence, orphan, near-dup, contradictions
  - topic-synthesis.ipynb  rag.query → optional LLM synth → kb.write(kind=kb)
  - paper-ingest.ipynb     arxiv search → PDF fetch → pymupdf extract → kb.write(kind=research)
  - daily-digest.ipynb     N-hour event log → structural summary → optional LLM narrative → kb.write(kind=episode)

All notebooks target the 'engram' kernel. Run with:
    ~/.engram/.venv/bin/python scripts/seed_starter_playbooks.py
"""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf

from engram.common.config import load_config

KERNEL = {
    "kernelspec": {"display_name": "Engram", "language": "python", "name": "engram"},
    "language_info": {"name": "python"},
}


def _md(text: str) -> nbf.NotebookNode:
    return nbf.v4.new_markdown_cell(text)


def _code(src: str, *, parameters: bool = False) -> nbf.NotebookNode:
    cell = nbf.v4.new_code_cell(src)
    if parameters:
        cell.metadata["tags"] = ["parameters"]
    return cell


def _write(path: Path, cells: list) -> None:
    nb = nbf.v4.new_notebook(cells=cells, metadata=KERNEL)
    nb.nbformat_minor = 5
    path.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(nb, str(path))
    print(f"wrote {path}")


# ---------- url-ingest ----------

URL_INGEST = [
    _md("# url-ingest\n"
        "\n"
        "Fetch a URL, extract clean text, push it through the dedup gate as `kind=research`.\n"
        "On success the projector renders a markdown note in `vault/030-research/` on its next tick.\n"),
    _code(
        '# parameters\n'
        'url = "https://example.com"\n'
        'source_tier = "blog"   # peer-reviewed | vendor-doc | blog | forum\n'
        'ttl_days = 180\n'
        'title = None           # if None, taken from page metadata\n'
        'confidence = 0.5\n',
        parameters=True,
    ),
    _code(
        'import httpx, trafilatura\n'
        'from datetime import datetime, timezone\n'
        'from engram.common.db import connect\n'
        'from engram import dedup\n'
        '\n'
        'resp = httpx.get(url, timeout=30, follow_redirects=True,\n'
        '                 headers={"User-Agent": "engram-playbook/0.1"})\n'
        'resp.raise_for_status()\n'
        'extracted = trafilatura.extract(resp.text, include_comments=False, include_tables=True)\n'
        'if not extracted:\n'
        '    raise RuntimeError(f"trafilatura returned no content for {url}")\n'
        'meta = trafilatura.extract_metadata(resp.text)\n'
        'effective_title = title or (meta.title if meta else None) or url\n'
        'print(f"extracted {len(extracted)} chars; title={effective_title!r}")\n'
    ),
    _code(
        'with connect() as conn:\n'
        '    result = dedup.gate(\n'
        '        conn, body=extracted, title=effective_title,\n'
        '        source_url=url, source_tier=source_tier,\n'
        '        ttl_days=ttl_days, kind="research", confidence=confidence,\n'
        '        actor="playbook:url-ingest",\n'
        '    )\n'
        '    if result.outcome == "new":\n'
        '        conn.execute(\n'
        '            "UPDATE content SET fetched_at = ? WHERE hash = ?",\n'
        '            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), result.hash),\n'
        '        )\n'
        'print(result)\n'
    ),
    _md("Outcome:\n"
        "- `new` — content stored, embedding will appear within seconds (reactor)\n"
        "- `exact_dup` — already in KB, no-op\n"
        "- `near_dup` — merged into existing entry"),
]

# ---------- vault-audit ----------

VAULT_AUDIT = [
    _md("# vault-audit\n"
        "\n"
        "Surface things in the KB that need attention: stale entries past TTL, low-confidence content,\n"
        "orphan vault files (on disk but not tracked), unresolved contradictions, near-duplicate pairs\n"
        "the reactor's post-hoc check might have missed.\n"),
    _code(
        '# parameters\n'
        'min_confidence = 0.3\n'
        'near_dup_threshold = 0.92\n'
        'top_n_per_section = 20\n',
        parameters=True,
    ),
    _code(
        'import struct\n'
        'import numpy as np\n'
        'from engram.common.db import connect\n'
        'from engram.common.config import load_config\n'
        '\n'
        'cfg = load_config()\n'
        'vault = cfg.paths.vault\n'
        '\n'
        'with connect() as conn:\n'
        '    stale = conn.execute(f"""\n'
        '        SELECT hash, title, fetched_at, ttl_days, confidence, staleness_score\n'
        '        FROM content\n'
        '        WHERE tombstoned = 0 AND fetched_at IS NOT NULL AND ttl_days IS NOT NULL\n'
        '          AND julianday(\'now\') - julianday(fetched_at) > ttl_days\n'
        '        ORDER BY (julianday(\'now\') - julianday(fetched_at)) / ttl_days DESC\n'
        '        LIMIT {top_n_per_section}\n'
        '    """).fetchall()\n'
        '\n'
        '    low_conf = conn.execute(\n'
        '        "SELECT hash, title, confidence, source_tier FROM content "\n'
        '        "WHERE tombstoned = 0 AND confidence < ? "\n'
        '        "ORDER BY confidence ASC LIMIT ?",\n'
        '        (min_confidence, top_n_per_section),\n'
        '    ).fetchall()\n'
        '\n'
        '    known = {r["vault_path"] for r in conn.execute("SELECT vault_path FROM vault_state").fetchall()}\n'
        '    on_disk = set()\n'
        '    for p in vault.rglob("*.md"):\n'
        '        rel = str(p.relative_to(vault))\n'
        '        if rel.startswith(".obsidian/") or rel.startswith(".trash/") or rel.startswith("_templates/"):\n'
        '            continue\n'
        '        on_disk.add(rel)\n'
        '    orphans = sorted(on_disk - known)[:top_n_per_section]\n'
        '\n'
        '    contras = conn.execute(\n'
        '        "SELECT id, hash_a, hash_b, detected_at, detected_by FROM contradictions "\n'
        '        "WHERE resolved = 0 ORDER BY detected_at DESC LIMIT ?",\n'
        '        (top_n_per_section,),\n'
        '    ).fetchall()\n'
        '\n'
        '    rows = conn.execute("SELECT content_hash, embedding FROM embeddings").fetchall()\n'
        '    hashes = [r["content_hash"] for r in rows]\n'
        '    if hashes:\n'
        '        embs = np.vstack([\n'
        '            np.frombuffer(r["embedding"], dtype=np.float32) for r in rows\n'
        '        ])\n'
        '        sims = embs @ embs.T\n'
        '        nears = []\n'
        '        for i in range(len(hashes)):\n'
        '            for j in range(i + 1, len(hashes)):\n'
        '                if sims[i, j] >= near_dup_threshold:\n'
        '                    nears.append((hashes[i], hashes[j], float(sims[i, j])))\n'
        '        nears.sort(key=lambda t: -t[2])\n'
        '        nears = nears[:top_n_per_section]\n'
        '    else:\n'
        '        nears = []\n'
        '\n'
        'print(f"stale={len(stale)} low_conf={len(low_conf)} orphans={len(orphans)} '
        'contradictions={len(contras)} near_dups={len(nears)}")\n'
    ),
    _code(
        'lines = ["# Vault audit", ""]\n'
        '\n'
        'lines += [f"## Stale ({len(stale)})", ""]\n'
        'for r in stale:\n'
        '    lines.append(f"- `{r[\'hash\'][:12]}` **{r[\'title\'] or \'(untitled)\'}** "\n'
        '                 f"— fetched {r[\'fetched_at\']}, ttl={r[\'ttl_days\']}d, '
        'staleness={r[\'staleness_score\']:.2f}")\n'
        'if not stale: lines.append("_none_")\n'
        '\n'
        'lines += ["", f"## Low confidence (<{min_confidence}) ({len(low_conf)})", ""]\n'
        'for r in low_conf:\n'
        '    lines.append(f"- `{r[\'hash\'][:12]}` **{r[\'title\'] or \'(untitled)\'}** "\n'
        '                 f"— confidence={r[\'confidence\']:.2f}, tier={r[\'source_tier\']}")\n'
        'if not low_conf: lines.append("_none_")\n'
        '\n'
        'lines += ["", f"## Orphan vault files ({len(orphans)})", ""]\n'
        'lines += [f"- `{p}`" for p in orphans] if orphans else ["_none_"]\n'
        '\n'
        'lines += ["", f"## Unresolved contradictions ({len(contras)})", ""]\n'
        'for r in contras:\n'
        '    lines.append(f"- `{r[\'hash_a\'][:12]}` ⇄ `{r[\'hash_b\'][:12]}` — '
        'flagged {r[\'detected_at\']} by {r[\'detected_by\']}")\n'
        'if not contras: lines.append("_none_")\n'
        '\n'
        'lines += ["", f"## Near-duplicate pairs (cos≥{near_dup_threshold}) ({len(nears)})", ""]\n'
        'for ha, hb, s in nears:\n'
        '    lines.append(f"- `{ha[:12]}` ⇄ `{hb[:12]}` — sim={s:.3f}")\n'
        'if not nears: lines.append("_none_")\n'
        '\n'
        'report = "\\n".join(lines)\n'
        'print(report)\n'
        '\n'
        'from pathlib import Path\n'
        'Path("audit.md").write_text(report)\n'
    ),
    _md("The audit is intentionally read-only. To act on findings:\n"
        "- **Stale**: re-run `url-ingest` for the affected `source_url`\n"
        "- **Low confidence**: hand-edit in Obsidian (watcher records the edit) or update `source_tier`\n"
        "- **Orphans**: either ingest properly (drop into `vault/000-inbox/`) or delete\n"
        "- **Near-dups**: call `kb.flag_contradiction` if they actually disagree, or delete one\n"
        "- **Contradictions**: human resolution via Obsidian + manual SQL"),
]

# ---------- topic-synthesis ----------

TOPIC_SYNTHESIS = [
    _md("# topic-synthesis\n"
        "\n"
        "Run a RAG query, optionally synthesize via Claude, write the result back through the dedup gate\n"
        "as a new `kind=kb` entry tagged `agent-derived`. Falls back to a structural concat if no API key.\n"),
    _code(
        '# parameters\n'
        'query = "what is the dedup gate"\n'
        'top_k = 12\n'
        'output_title = None        # defaults to the query\n'
        'write_to_kb = True\n'
        'model = "claude-haiku-4-5-20251001"\n'
        'use_llm = True\n'
        '# Loop guard: by default, exclude prior agent-derived content (other syntheses)\n'
        '# from the retrieval pool. Set to [] to allow them.\n'
        'exclude_source_tiers = ["agent-derived"]\n',
        parameters=True,
    ),
    _code(
        'import os, textwrap\n'
        'from engram.common.db import connect\n'
        'from engram.rag.query import hybrid_search\n'
        'from engram import dedup\n'
        '\n'
        'with connect() as conn:\n'
        '    hits = hybrid_search(\n'
        '        conn, query, top_k=top_k,\n'
        '        exclude_source_tiers=exclude_source_tiers or None,\n'
        '    )\n'
        'print(f"retrieved {len(hits)} hits for: {query!r}")\n'
        'for i, h in enumerate(hits, 1):\n'
        '    print(f"  [{i}] {h.score:.3f}  {h.title or \'(untitled)\'}  hash={h.hash[:12]}")\n'
        '\n'
        'if not hits:\n'
        '    raise SystemExit("no hits — nothing to synthesize")\n'
    ),
    _code(
        'api_key = os.environ.get("ENGRAM_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")\n'
        'synth = None\n'
        '\n'
        'if use_llm and api_key:\n'
        '    try:\n'
        '        from anthropic import Anthropic\n'
        '        client = Anthropic(api_key=api_key)\n'
        '        ctx = "\\n\\n".join(\n'
        '            f"[{i}] {h.title or \'(untitled)\'}\\n{h.body}"\n'
        '            for i, h in enumerate(hits, 1)\n'
        '        )\n'
        '        prompt = (\n'
        '            f"Synthesize a focused answer to: {query}\\n\\n"\n'
        '            f"Use ONLY the sources below. Cite as [N]. "\n'
        '            f"If the sources are insufficient, say so explicitly.\\n\\n{ctx}"\n'
        '        )\n'
        '        msg = client.messages.create(\n'
        '            model=model, max_tokens=2000,\n'
        '            messages=[{"role": "user", "content": prompt}],\n'
        '        )\n'
        '        synth = msg.content[0].text\n'
        '        print(f"LLM synthesis: {len(synth)} chars")\n'
        '    except Exception as e:\n'
        '        print(f"LLM synthesis failed ({e}); using structural fallback")\n'
        '\n'
        'if not synth:\n'
        '    synth = "\\n\\n".join(\n'
        '        f"## [{i}] {h.title or \'(untitled)\'}\\n\\n{h.body}"\n'
        '        for i, h in enumerate(hits, 1)\n'
        '    )\n'
    ),
    _code(
        'effective_title = output_title or f"Synthesis: {query}"\n'
        'body = (\n'
        '    f"# {effective_title}\\n\\n"\n'
        '    f"_synthesis from {len(hits)} retrieved sources_\\n\\n"\n'
        '    f"{synth}\\n\\n"\n'
        '    f"## Sources\\n\\n"\n'
        '    + "\\n".join(\n'
        '        f"[{i}] `{h.hash[:12]}` — {h.title or \'(untitled)\'}"\n'
        '        f" (confidence={h.confidence:.2f})"\n'
        '        for i, h in enumerate(hits, 1)\n'
        '    )\n'
        ')\n'
        '\n'
        'print(body[:1500] + ("\\n...\\n[truncated]" if len(body) > 1500 else ""))\n'
        '\n'
        'from pathlib import Path\n'
        'Path("synthesis.md").write_text(body)\n'
        '\n'
        'if write_to_kb:\n'
        '    with connect() as conn:\n'
        '        result = dedup.gate(\n'
        '            conn, body=body, title=effective_title,\n'
        '            source_tier="agent-derived", confidence=0.6, kind="kb",\n'
        '            actor="playbook:topic-synthesis",\n'
        '        )\n'
        '    print(result)\n'
    ),
    _md("The synthesis becomes a first-class KB entry. Subsequent `rag.query` calls can retrieve it,\n"
        "which means future syntheses can cite past syntheses — be aware of this loop and don't\n"
        "treat synthesized content as authoritative for facts."),
]


PAPER_INGEST = [
    _md("# paper-ingest\n"
        "\n"
        "Search arXiv for a topic, download top-k PDFs, extract text via pymupdf,\n"
        "push each through the dedup gate as `kind=research`, `source_tier=peer-reviewed`.\n"
        "Failures on individual papers (network, parse) skip that paper and continue.\n"),
    _code(
        '# parameters\n'
        'query = "reciprocal rank fusion"\n'
        'k = 5\n'
        'max_chars_per_paper = 80000   # truncate PDFs longer than this\n'
        'confidence = 0.85             # peer-reviewed via arXiv\n'
        'ttl_days = 1825               # 5y — papers age slowly\n'
        'source_tier = "peer-reviewed"\n',
        parameters=True,
    ),
    _code(
        'import io, time\n'
        'import httpx, fitz\n'
        'from datetime import datetime, timezone\n'
        'from engram.common.db import connect\n'
        'from engram.research import arxiv as arxiv_mod\n'
        'from engram import dedup\n'
        '\n'
        'papers = arxiv_mod.search(query, k=k)\n'
        'print(f"found {len(papers)} papers")\n'
        'for p in papers:\n'
        '    print(f"  [{p.score:.2f}] {p.arxiv_id}  {p.title[:80]}")\n'
        'if not papers:\n'
        '    raise SystemExit("no arxiv hits")\n'
    ),
    _code(
        'def fetch_and_extract(pdf_url: str) -> str:\n'
        '    r = httpx.get(pdf_url, timeout=60, follow_redirects=True,\n'
        '                  headers={"User-Agent": "engram-paper-ingest/0.1"})\n'
        '    r.raise_for_status()\n'
        '    doc = fitz.open(stream=r.content, filetype="pdf")\n'
        '    try:\n'
        '        return "\\n\\n".join(page.get_text("text") for page in doc)\n'
        '    finally:\n'
        '        doc.close()\n'
        '\n'
        'now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")\n'
        'outcomes = []\n'
        '\n'
        'for p in papers:\n'
        '    print(f"\\n→ {p.arxiv_id}  {p.title[:60]}")\n'
        '    try:\n'
        '        text = fetch_and_extract(p.pdf_url)\n'
        '    except Exception as e:\n'
        '        print(f"  fetch/extract failed: {type(e).__name__}: {e}")\n'
        '        outcomes.append((p.arxiv_id, "fetch_failed", None))\n'
        '        continue\n'
        '    if not text or len(text.strip()) < 500:\n'
        '        print(f"  too little text extracted ({len(text)} chars); skipping")\n'
        '        outcomes.append((p.arxiv_id, "empty_extract", None))\n'
        '        continue\n'
        '    if len(text) > max_chars_per_paper:\n'
        '        text = text[:max_chars_per_paper]\n'
        '\n'
        '    title = f"{p.title} ({p.arxiv_id})"\n'
        '    body = (\n'
        '        f"# {p.title}\\n\\n"\n'
        '        f"**arXiv ID:** {p.arxiv_id}  \\n"\n'
        '        f"**Authors:** {\', \'.join(p.authors)}  \\n"\n'
        '        f"**Published:** {p.published[:10]}  \\n"\n'
        '        f"**PDF:** {p.pdf_url}  \\n"\n'
        '        f"**Abstract URL:** {p.abs_url}\\n\\n"\n'
        '        f"## Abstract\\n\\n{p.abstract}\\n\\n"\n'
        '        f"## Full text\\n\\n{text}"\n'
        '    )\n'
        '\n'
        '    with connect() as conn:\n'
        '        result = dedup.gate(\n'
        '            conn, body=body, title=title,\n'
        '            source_url=p.abs_url, source_tier=source_tier,\n'
        '            confidence=confidence, ttl_days=ttl_days,\n'
        '            kind="research", actor="playbook:paper-ingest",\n'
        '        )\n'
        '        if result.outcome == "new":\n'
        '            conn.execute(\n'
        '                "UPDATE content SET fetched_at = ? WHERE hash = ?",\n'
        '                (now, result.hash),\n'
        '            )\n'
        '    print(f"  {result.outcome}  hash={result.hash[:12]}  ({len(body)} chars)")\n'
        '    outcomes.append((p.arxiv_id, result.outcome, result.hash))\n'
        '    # Polite rate-limit: arXiv recommends ≥3s between requests.\n'
        '    time.sleep(1)\n'
    ),
    _code(
        'from collections import Counter\n'
        'from pathlib import Path\n'
        '\n'
        'tally = Counter(o for _id, o, _h in outcomes)\n'
        'lines = [\n'
        '    f"# paper-ingest run",\n'
        '    "",\n'
        '    f"**Query:** {query}",\n'
        '    f"**Requested k:** {k}",\n'
        '    f"**Returned:** {len(papers)}",\n'
        '    f"**Outcomes:** " + "  ".join(f"{k_}={v}" for k_, v in tally.items()),\n'
        '    "",\n'
        '    "## Per paper",\n'
        '    "",\n'
        ']\n'
        'for arxiv_id, outcome, h in outcomes:\n'
        '    tag = f"`{h[:12]}`" if h else "—"\n'
        '    lines.append(f"- `{arxiv_id}` — **{outcome}** {tag}")\n'
        '\n'
        'report = "\\n".join(lines)\n'
        'print(report)\n'
        'Path("paper-ingest.md").write_text(report)\n'
    ),
    _md("Notes:\n"
        "- New entries get `confidence=0.85` and `source_tier=peer-reviewed` (weight 1.0). "
        "They will outrank blog content for matching queries.\n"
        "- The reactor embeds the first ~16K chars; longer papers retain their full body in the KB "
        "but the embedding may miss late-section content. Until chunk-level RAG ships, "
        "consider this when querying for material that lives deep in long papers.\n"
        "- Re-running the same query is cheap — exact-dup outcomes are no-ops."),
]


DAILY_DIGEST = [
    _md("# daily-digest\n"
        "\n"
        "Read N hours of events, build a structural summary, optionally rewrite as narrative\n"
        "via Claude, write back as `kind=episode`. Lands in `vault/010-episodes/`.\n"
        "\n"
        "Designed to answer: *what happened in my knowledge system since I last looked?*\n"),
    _code(
        '# parameters\n'
        'window_hours = 24\n'
        'use_llm = True\n'
        'model = "claude-haiku-4-5-20251001"\n'
        'write_to_kb = True\n'
        'date_label = None         # e.g. "2026-05-06"; defaults to today\n'
        'max_per_section = 30\n',
        parameters=True,
    ),
    _code(
        'import os, json\n'
        'from collections import Counter, defaultdict\n'
        'from datetime import datetime, timezone, timedelta\n'
        'from engram.common.db import connect\n'
        'from engram import dedup\n'
        '\n'
        'now = datetime.now(timezone.utc)\n'
        'window_start = now - timedelta(hours=window_hours)\n'
        'window_iso = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")\n'
        'effective_label = date_label or now.strftime("%Y-%m-%d")\n'
        'print(f"window: {window_iso} → {now.strftime(\'%Y-%m-%dT%H:%M:%SZ\')}")\n'
        '\n'
        'with connect() as conn:\n'
        '    rows = conn.execute(\n'
        '        "SELECT id, ts, type, payload, actor FROM events "\n'
        '        "WHERE ts >= ? ORDER BY id",\n'
        '        (window_iso,),\n'
        '    ).fetchall()\n'
        '    events = [dict(r, payload=json.loads(r["payload"])) for r in rows]\n'
        '\n'
        '    # Pull active goals + recently-touched content for context.\n'
        '    active_goals = [\n'
        '        dict(r) for r in conn.execute(\n'
        '            "SELECT id, text, priority, created_at, updated_at FROM goals "\n'
        '            "WHERE status = \'active\' ORDER BY priority DESC, updated_at DESC"\n'
        '        ).fetchall()\n'
        '    ]\n'
        '    # Hashes referenced in this window\'s events; resolve to titles.\n'
        '    hashes_in_window = set()\n'
        '    for e in events:\n'
        '        for k in ("hash", "hash_kept", "hash_tombstoned", "hash_a", "hash_b"):\n'
        '            if k in e["payload"] and isinstance(e["payload"][k], str):\n'
        '                hashes_in_window.add(e["payload"][k])\n'
        '    title_by_hash = {}\n'
        '    if hashes_in_window:\n'
        '        ph = ",".join("?" * len(hashes_in_window))\n'
        '        for r in conn.execute(\n'
        '            f"SELECT hash, title, kind, source_url FROM content WHERE hash IN ({ph})",\n'
        '            list(hashes_in_window),\n'
        '        ).fetchall():\n'
        '            title_by_hash[r["hash"]] = dict(r)\n'
        '\n'
        'print(f"events in window: {len(events)}")\n'
        'print(f"event types: {Counter(e[\'type\'] for e in events)}")\n'
        'print(f"active goals: {len(active_goals)}")\n'
    ),
    _code(
        '# Group events by type for structured rendering.\n'
        'by_type = defaultdict(list)\n'
        'for e in events:\n'
        '    by_type[e["type"]].append(e)\n'
        '\n'
        'def _t(h):\n'
        '    """Pretty title for a hash."""\n'
        '    rec = title_by_hash.get(h)\n'
        '    if not rec:\n'
        '        return f"`{h[:12]}` (gone)"\n'
        '    return f"**{rec[\'title\'] or \'(untitled)\'}** `{h[:12]}`"\n'
        '\n'
        'def _section(title, lines):\n'
        '    if not lines:\n'
        '        return [f"## {title}", "", "_none_", ""]\n'
        '    return [f"## {title}", ""] + lines + [""]\n'
        '\n'
        '# ----- Ingests -----\n'
        'ingest_lines = []\n'
        'for e in by_type.get("ingested", [])[:max_per_section]:\n'
        '    p = e["payload"]\n'
        '    src = p.get("source_url") or "no source"\n'
        '    ingest_lines.append(\n'
        '        f"- `{p.get(\'kind\',\'kb\')}` {_t(p[\'hash\'])} — tier={p.get(\'source_tier\',\'?\')} — {src}"\n'
        '    )\n'
        '\n'
        '# ----- Merges -----\n'
        'merge_lines = []\n'
        'merge_reasons = Counter()\n'
        'for e in by_type.get("merged", []):\n'
        '    p = e["payload"]\n'
        '    merge_reasons[p.get("reason", "?")] += 1\n'
        '    kept = p.get("hash_kept")\n'
        '    tomb = p.get("hash_tombstoned")\n'
        '    if kept:\n'
        '        merge_lines.append(f"- {_t(kept)} ← {_t(tomb)} ({p.get(\'reason\',\'?\')})")\n'
        '    else:\n'
        '        merge_lines.append(f"- purged {_t(tomb)} ({p.get(\'reason\',\'?\')})")\n'
        '\n'
        '# ----- Retrievals -----\n'
        'retr_events = by_type.get("retrieved", [])\n'
        'queries = Counter(e["payload"].get("query", "?") for e in retr_events)\n'
        'retrieval_hashes = []\n'
        'for e in retr_events:\n'
        '    p = e["payload"]\n'
        '    if "hashes" in p:\n'
        '        retrieval_hashes.extend(p["hashes"])\n'
        '    elif "hash" in p:\n'
        '        retrieval_hashes.append(p["hash"])\n'
        'top_retrieved_hashes = Counter(retrieval_hashes).most_common(10)\n'
        'total_retrieval_hits = sum(e["payload"].get("count", 0) or len(e["payload"].get("hashes", []) or []) for e in retr_events)\n'
        'retr_lines = [f"- {n}× `{q}`" for q, n in queries.most_common(max_per_section)]\n'
        '\n'
        '# ----- Goals -----\n'
        'goal_lines = []\n'
        'for e in by_type.get("goal_set", []):\n'
        '    goal_lines.append(f"- **set** {e[\'payload\'].get(\'text\',\'?\')[:120]}")\n'
        'for e in by_type.get("goal_resolved", []):\n'
        '    goal_lines.append(f"- **resolved** goal {e[\'payload\'].get(\'goal_id\',\'?\')}")\n'
        '\n'
        '# ----- Vault edits -----\n'
        'edit_lines = [\n'
        '    f"- `{e[\'payload\'].get(\'path\',\'?\')}` ({_t(e[\'payload\'].get(\'hash\',\'\'))})"\n'
        '    for e in by_type.get("vault_edit", [])[:max_per_section]\n'
        ']\n'
        '\n'
        '# ----- Playbook runs -----\n'
        'pb_lines = []\n'
        'for e in by_type.get("playbook_run", []):\n'
        '    p = e["payload"]\n'
        '    ec = p.get("exit_code", "?")\n'
        '    flag = "✓" if ec == 0 else "✗"\n'
        '    pb_lines.append(f"- {flag} `{p.get(\'playbook\',\'?\')}` ({p.get(\'run_id\',\'?\')[:30]}) exit={ec}")\n'
        '\n'
        '# ----- Contradictions -----\n'
        'contra_lines = [\n'
        '    f"- {_t(e[\'payload\'].get(\'hash_a\',\'\'))}  ⇄  {_t(e[\'payload\'].get(\'hash_b\',\'\'))}"\n'
        '    for e in by_type.get("contradicted", [])\n'
        ']\n'
        '\n'
        '# ----- Stale / refresh -----\n'
        'stale_lines = [\n'
        '    f"- {_t(e[\'payload\'].get(\'hash\',\'\'))} score={e[\'payload\'].get(\'score\',\'?\'):.2f}"\n'
        '    if isinstance(e[\'payload\'].get(\'score\'), (int, float))\n'
        '    else f"- {_t(e[\'payload\'].get(\'hash\',\'\'))}"\n'
        '    for e in by_type.get("stale_marked", [])\n'
        ']\n'
        '\n'
        '# ----- Source curation -----\n'
        'from engram.common.db import connect as _db_connect\n'
        'with _db_connect() as _src_conn:\n'
        '    src_rows = list(_src_conn.execute(\n'
        '        "SELECT id, name, last_polled_at, last_success_at, paused, error_count, "\n'
        '        "last_error, next_poll_at FROM sources ORDER BY id"\n'
        '    ).fetchall())\n'
        '\n'
        '# Per-source counts within window from source_polled events\n'
        'src_counts = {}\n'
        'for e in by_type.get("source_polled", []):\n'
        '    p = e["payload"]\n'
        '    sid = p.get("source_id")\n'
        '    if not sid:\n'
        '        continue\n'
        '    c = src_counts.setdefault(sid, {"ingested": 0, "superseded": 0, "errors": 0, "candidates": 0})\n'
        '    c["ingested"]   += p.get("ingested", 0)\n'
        '    c["superseded"] += p.get("superseded", 0)\n'
        '    c["errors"]     += p.get("errors", 0)\n'
        '    c["candidates"] += p.get("candidates_seen", 0)\n'
        '\n'
        'src_lines = []\n'
        'for s in src_rows:\n'
        '    if s["paused"]:\n'
        '        icon = "⛔"\n'
        '    elif s["error_count"]:\n'
        '        icon = "⚠"\n'
        '    else:\n'
        '        icon = "✓"\n'
        '    c = src_counts.get(s["id"], {})\n'
        '    if c.get("ingested") or c.get("superseded"):\n'
        '        fragment = f"{c.get(\'ingested\', 0)} new, {c.get(\'superseded\', 0)} superseded"\n'
        '    else:\n'
        '        fragment = "0 changes"\n'
        '    err = f" — {s[\'last_error\']}" if s["error_count"] and s["last_error"] else ""\n'
        '    src_lines.append(\n'
        '        f"- {icon} `{s[\'id\']}`: {fragment} (last poll {s[\'last_polled_at\'] or \'never\'}, "\n'
        '        f"next {s[\'next_poll_at\'] or \'unscheduled\'}){err}"\n'
        '    )\n'
    ),
    _code(
        '# Heuristic anomaly detection. Flags things worth a human eye.\n'
        'anomalies = []\n'
        '\n'
        '# Failed playbook runs.\n'
        'failed = [e for e in by_type.get("playbook_run", []) if e["payload"].get("exit_code") not in (0, None)]\n'
        'if failed:\n'
        '    anomalies.append(f"- {len(failed)} playbook run(s) exited non-zero — see playbooks/runs/ for details")\n'
        '\n'
        '# Stagnant goals (active >7 days, no resolution).\n'
        'cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")\n'
        'stagnant = [g for g in active_goals if g["updated_at"] < cutoff]\n'
        'if stagnant:\n'
        '    for g in stagnant:\n'
        '        anomalies.append(f"- goal stagnant >7d: \\"{g[\'text\'][:80]}\\" (last touched {g[\'updated_at\'][:10]})")\n'
        '\n'
        '# Lots of dedups in a row may indicate the gate is preventing useful re-ingestion.\n'
        'if merge_reasons.get("manual_purge", 0) > 0:\n'
        '    anomalies.append(f"- {merge_reasons[\'manual_purge\']} manual purge(s) — query refinement or playbook tuning may be needed")\n'
        '\n'
        '# Self-citation: agent-derived content being retrieved alongside its own kind.\n'
        'agent_derived_hits = sum(\n'
        '    1 for h, _ in top_retrieved_hashes\n'
        '    if h in title_by_hash and "Synthesis:" in (title_by_hash[h]["title"] or "")\n'
        ')\n'
        'if agent_derived_hits >= 3:\n'
        '    anomalies.append(f"- {agent_derived_hits} top retrieved entries are syntheses; loop guard is fine but worth noting")\n'
        '\n'
        '# Idle vault.\n'
        'if not by_type.get("vault_edit") and window_hours <= 48:\n'
        '    anomalies.append("- no vault_edit events — Obsidian is not being used directly")\n'
        '\n'
        '# Unresolved contradictions.\n'
        'open_contras = by_type.get("contradicted", [])\n'
        'if open_contras:\n'
        '    anomalies.append(f"- {len(open_contras)} new contradiction(s) flagged — needs resolution")\n'
        '\n'
        'if not anomalies:\n'
        '    anomalies.append("- _none_")\n'
        '\n'
        'print("\\n".join(anomalies))\n'
    ),
    _code(
        '# Assemble structural digest.\n'
        'header = [\n'
        '    f"# {effective_label} — daily digest",\n'
        '    "",\n'
        '    f"_window: {window_hours}h ending {now.strftime(\'%Y-%m-%dT%H:%M:%SZ\')}_",\n'
        '    "",\n'
        '    "## Activity at a glance",\n'
        '    "",\n'
        '    f"- Total events: **{len(events)}**",\n'
        '    f"- Ingests: {len(by_type.get(\'ingested\', []))}",\n'
        '    f"- Merges/purges: {len(by_type.get(\'merged\', []))} ({dict(merge_reasons) or \'—\'})",\n'
        '    f"- Retrievals: {len(retr_events)} queries returning {total_retrieval_hits} total hits "\n'
        '    f"({len(queries)} distinct queries)",\n'
        '    f"- Goal events: set={len(by_type.get(\'goal_set\', []))} resolved={len(by_type.get(\'goal_resolved\', []))}",\n'
        '    f"- Vault edits: {len(by_type.get(\'vault_edit\', []))}",\n'
        '    f"- Playbook runs: {len(by_type.get(\'playbook_run\', []))}",\n'
        '    f"- Active goals (now): {len(active_goals)}",\n'
        '    "",\n'
        ']\n'
        '\n'
        'goals_section = ["## Active goals", ""]\n'
        'if active_goals:\n'
        '    for g in active_goals:\n'
        '        goals_section.append(f"- (p={g[\'priority\']}) {g[\'text\']} _(set {g[\'created_at\'][:10]})_")\n'
        'else:\n'
        '    goals_section.append("_none_")\n'
        'goals_section.append("")\n'
        '\n'
        'sections = (\n'
        '    header + goals_section\n'
        '    + _section(f"Ingested ({len(by_type.get(\'ingested\', []))})", ingest_lines)\n'
        '    + _section(f"Merged / purged ({len(by_type.get(\'merged\', []))})", merge_lines)\n'
        '    + _section(f"Top retrieval queries", retr_lines[:max_per_section])\n'
        '    + _section(f"Goals", goal_lines)\n'
        '    + _section(f"Vault edits", edit_lines)\n'
        '    + _section(f"Playbook runs", pb_lines)\n'
        '    + _section(f"Contradictions", contra_lines)\n'
        '    + _section(f"Stale / refresh", stale_lines)\n'
        '    + _section(f"Source curation", src_lines)\n'
        '    + ["## Anomalies", ""] + anomalies + [""]\n'
        ')\n'
        'structural = "\\n".join(sections)\n'
        'print(structural[:1200] + ("\\n...\\n[truncated for preview]" if len(structural) > 1200 else ""))\n'
    ),
    _code(
        '# Optional Claude rewrite into a narrative summary.\n'
        'narrative = None\n'
        'api_key = os.environ.get("ENGRAM_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")\n'
        'if use_llm and api_key and len(events) > 0:\n'
        '    try:\n'
        '        from anthropic import Anthropic\n'
        '        client = Anthropic(api_key=api_key)\n'
        '        prompt = (\n'
        '            "You are writing a daily digest for a personal knowledge system. The structured\\n"\n'
        '            "summary below lists what happened in the last " + str(window_hours) + " hours.\\n\\n"\n'
        '            "Rewrite as a focused 300-500 word narrative organized as:\\n"\n'
        '            "  ## What happened — describe the threads of activity, naming sources by title\\n"\n'
        '            "  ## What was learned — what did the day produce; reference titles not hashes\\n"\n'
        '            "  ## Open threads — work that started but did not finish\\n"\n'
        '            "  ## Suggested next steps — concrete, optional\\n\\n"\n'
        '            "Rules: do not invent facts. If a section has no signal, omit it. Lean dry.\\n\\n"\n'
        '            "Structured summary:\\n\\n" + structural\n'
        '        )\n'
        '        msg = client.messages.create(\n'
        '            model=model, max_tokens=1500,\n'
        '            messages=[{"role": "user", "content": prompt}],\n'
        '        )\n'
        '        narrative = msg.content[0].text.strip()\n'
        '        print(f"narrative: {len(narrative)} chars")\n'
        '    except Exception as e:\n'
        '        print(f"LLM narrative failed ({e}); using structural only")\n'
        'else:\n'
        '    print("LLM narrative skipped (use_llm or api_key)")\n'
    ),
    _code(
        '# Final body: narrative on top (if produced) + structural detail underneath.\n'
        'body_parts = [f"# {effective_label} — daily digest", ""]\n'
        'body_parts.append(f"_window: {window_hours}h ending {now.strftime(\'%Y-%m-%dT%H:%M:%SZ\')}_  \\n")\n'
        'body_parts.append(f"_{len(events)} events, {len(active_goals)} active goal(s)_")\n'
        'body_parts.append("")\n'
        'if narrative:\n'
        '    body_parts += [narrative, "", "---", ""]\n'
        'body_parts += sections[5:]  # everything after the header (we already wrote our own header)\n'
        'body = "\\n".join(body_parts)\n'
        '\n'
        'from pathlib import Path\n'
        'Path("digest.md").write_text(body)\n'
        'print(f"digest.md: {len(body)} chars")\n'
        '\n'
        'if write_to_kb:\n'
        '    with connect() as conn:\n'
        '        result = dedup.gate(\n'
        '            conn, body=body, title=f"{effective_label} — daily digest",\n'
        '            source_tier="agent-derived", confidence=0.7, kind="episode",\n'
        '            actor="playbook:daily-digest",\n'
        '        )\n'
        '    print(f"\\nKB outcome: {result.outcome}  hash={result.hash[:12]}")\n'
        'else:\n'
        '    print("\\nwrite_to_kb=False, digest written to disk only")\n'
    ),
    _md("Run cadence: hourly cron for short windows, daily for canonical digest, weekly for retros.\n"
        "\n"
        "Filtering at retrieval: digests are `kind=episode`, `source_tier=agent-derived`. They are\n"
        "*excluded* from the synthesis playbook by default (loop guard) but appear in `rag.query`\n"
        "and `kb.list(kind='episode')`. To exclude them from a query: `exclude_kinds=['episode']`."),
]


def main() -> None:
    cfg = load_config()
    target = cfg.paths.playbooks_scratch
    target.mkdir(parents=True, exist_ok=True)
    _write(target / "url-ingest.ipynb",       URL_INGEST)
    _write(target / "vault-audit.ipynb",      VAULT_AUDIT)
    _write(target / "topic-synthesis.ipynb",  TOPIC_SYNTHESIS)
    _write(target / "paper-ingest.ipynb",     PAPER_INGEST)
    _write(target / "daily-digest.ipynb",     DAILY_DIGEST)
    print("\ndone. Run via:")
    print("  playbook.run name='daily-digest.ipynb' params={window_hours:24}")


if __name__ == "__main__":
    main()
