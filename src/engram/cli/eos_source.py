"""CLI mirror of the sources.* MCP tools, for shell use."""
from __future__ import annotations

import argparse
import json
import sys

from ..common.db import get_connection
from ..mcp_server.tools.sources import register


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eos-source")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="register a new source")
    a.add_argument("id")
    a.add_argument("--name", required=True)
    a.add_argument("--adapter", choices=["sitemap", "github-repo"], required=True)
    a.add_argument("--url", required=True)
    a.add_argument("--include", action="append", default=[])
    a.add_argument("--exclude", action="append", default=[])
    a.add_argument("--schedule")
    a.add_argument("--source-tier")
    a.add_argument("--paused", action="store_true")

    sub.add_parser("list", help="list sources").add_argument(
        "--with-errors", action="store_true",
    )

    g = sub.add_parser("get", help="show one source")
    g.add_argument("id")

    rm = sub.add_parser("remove", help="delete a source")
    rm.add_argument("id")

    fn = sub.add_parser("fetch-now", help="force immediate poll")
    fn.add_argument("id")

    s = sub.add_parser("set", help="update fields")
    s.add_argument("id")
    s.add_argument("--paused", choices=["true", "false"])
    s.add_argument("--schedule")
    s.add_argument("--source-tier")
    s.add_argument("--include", action="append")
    s.add_argument("--exclude", action="append")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn = get_connection()
    tools = register(conn)

    if args.cmd == "add":
        cfg = {}
        if args.include:
            cfg["include"] = args.include
        if args.exclude:
            cfg["exclude"] = args.exclude
        out = tools["sources.add"]["handler"]({
            "id": args.id, "name": args.name, "adapter": args.adapter,
            "url": args.url, "config": cfg,
            "schedule": args.schedule,
            "source_tier": args.source_tier,
            "paused": args.paused,
        })
    elif args.cmd == "list":
        out = tools["sources.list"]["handler"]({"with_errors": args.with_errors})
    elif args.cmd == "get":
        out = tools["sources.get"]["handler"]({"id": args.id})
    elif args.cmd == "remove":
        out = tools["sources.remove"]["handler"]({"id": args.id})
    elif args.cmd == "fetch-now":
        out = tools["sources.fetch_now"]["handler"]({"id": args.id})
    elif args.cmd == "set":
        body: dict = {"id": args.id}
        if args.paused is not None:
            body["paused"] = args.paused == "true"
        if args.schedule:
            body["schedule"] = args.schedule
        if args.source_tier:
            body["source_tier"] = args.source_tier
        if args.include or args.exclude:
            body["config"] = {}
            if args.include: body["config"]["include"] = args.include
            if args.exclude: body["config"]["exclude"] = args.exclude
        out = tools["sources.set"]["handler"](body)
    else:
        return 2

    json.dump(out, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
