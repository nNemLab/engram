"""Entry point: engram-mcp. Default transport stdio; --http serves streamable HTTP."""
from __future__ import annotations

import argparse
import logging
import os


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="engram-mcp")
    p.add_argument("--http", action="store_true",
                   default=os.environ.get("ENGRAM_MCP_TRANSPORT") == "http",
                   help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default=os.environ.get("ENGRAM_MCP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("ENGRAM_MCP_PORT", "8765")))
    return p.parse_args(argv)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = parse_args()
    if args.http:
        import uvicorn

        from .http_app import build_http_app
        uvicorn.run(build_http_app(), host=args.host, port=args.port)
    else:
        from .server import main as stdio_main
        stdio_main()


if __name__ == "__main__":
    main()
