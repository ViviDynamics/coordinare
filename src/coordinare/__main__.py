from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from coordinare import configure_logging
from coordinare.config import ProjectConfiguration
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.builder import CoordinareGraphBuilder


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the coordinare daemon")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to config file")
    return parser


def _create_health_app(daemon: CoordinareDaemon) -> FastAPI:
    app = FastAPI(title="coordinare-health")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "healthy" if daemon.running else "unhealthy",
            "phase": daemon.state.get("phase", "unknown"),
            "error_count": daemon.state.get("error_count", 0),
        }

    return app


async def _run(config: ProjectConfiguration) -> None:
    graph = CoordinareGraphBuilder().build()
    daemon = CoordinareDaemon(graph, poll_interval_seconds=config.poll_interval_seconds)

    app = _create_health_app(daemon)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=config.health_check_port, log_level="warning")
    )

    daemon_task = asyncio.create_task(daemon.start())
    server_task = asyncio.create_task(server.serve())

    try:
        await daemon_task
    finally:
        server.should_exit = True
        await server_task


def main() -> None:
    args = _build_arg_parser().parse_args()
    configure_logging()
    config = ProjectConfiguration.from_yaml(args.config)
    asyncio.run(_run(config))


if __name__ == "__main__":
    main()
