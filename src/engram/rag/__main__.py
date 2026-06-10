"""CLI: engram-rag query 'your question' [-k 12]"""
from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.table import Table

from ..common.db import connect
from .query import hybrid_search


@click.group()
def cli(): ...


@cli.command()
@click.argument("query")
@click.option("-k", "top_k", type=int, default=None)
def query(query: str, top_k: int | None) -> None:
    console = Console()
    with connect() as conn:
        hits = hybrid_search(conn, query, top_k=top_k)
    if not hits:
        console.print("[yellow]No results.[/yellow]")
        sys.exit(1)
    t = Table(show_lines=True)
    t.add_column("score", justify="right")
    t.add_column("title")
    t.add_column("source")
    t.add_column("snippet")
    for h in hits:
        snippet = (h.body[:160] + "…") if len(h.body) > 160 else h.body
        t.add_row(f"{h.score:.3f}", h.title or "(untitled)", h.source_url or "", snippet)
    console.print(t)


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=None, help="default: grounding.port from config")
def serve(host: str, port: int | None) -> None:
    """Run the warm grounding daemon (/grounding, /prime, /healthz)."""
    from .serve import serve as run_serve
    run_serve(host=host, port=port)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
