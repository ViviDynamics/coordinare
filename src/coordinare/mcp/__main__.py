"""CLI entry point for the MCP management server (``python -m coordinare.mcp``).

Phase 1 (T014): streamable HTTP transport only; stdio arrives in spec 552.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn
from pydantic import ValidationError

from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.server import build_asgi_app


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="coordinare.mcp",
        description="MCP management server proxying the coordinare dashboard REST API",
    )
    parser.add_argument(
        "--transport",
        choices=("http",),
        default="http",
        help="Transport (streamable HTTP in phase 1; stdio arrives in spec 552)",
    )
    parser.add_argument("--listen-host", default=None, help="Bind host (env: COORDINARE_MCP_HOST)")
    parser.add_argument("--listen-port", type=int, default=None, help="Bind port (env: COORDINARE_MCP_PORT)")
    return parser.parse_args(argv)


def main() -> int:
    """Boot the MCP server; exit code 2 on misconfiguration, 0 otherwise."""
    args = _parse_args(sys.argv[1:])
    try:
        config = McpServerConfig.from_env()
    except ValidationError as exc:
        print(f"coordinare.mcp: misconfigured: {exc}", file=sys.stderr)
        return 2
    host = args.listen_host if args.listen_host is not None else config.listen_host
    port = args.listen_port if args.listen_port is not None else config.listen_port
    app = build_asgi_app(config)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
