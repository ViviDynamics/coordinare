from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI

from coordinare import configure_logging
from coordinare.config import ProjectConfiguration
from coordinare.daemon import CoordinareDaemon, RuntimeExecutionError
from coordinare.graph.builder import CoordinareGraphBuilder

logger = structlog.get_logger(__name__)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the coordinare daemon")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to config file")
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default=None,
        help="Override runtime log level",
    )
    parser.add_argument(
        "--structured-output",
        action="store_true",
        help="Emit JSON structured logs instead of human-readable logs",
    )
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
    run_mode = os.getenv("COORDINARE_RUN_MODE", "shell").strip().lower() or "shell"
    graph = CoordinareGraphBuilder().build()
    daemon = CoordinareDaemon(
        graph,
        run_mode=run_mode,
        poll_interval_seconds=config.poll_interval_seconds,
        heartbeat_interval_seconds=min(config.heartbeat_interval_seconds, 30),
        max_cycles=config.max_cycles,
    )

    app = _create_health_app(daemon)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=config.health_check_port, log_level="warning")
    )

    daemon_task = asyncio.create_task(daemon.start())
    server_task = asyncio.create_task(server.serve())

    try:
        logger.info("startup_mode_selected", run_mode=run_mode)
        await daemon_task
    finally:
        server.should_exit = True
        await server_task


def main() -> None:
    args = _build_arg_parser().parse_args()
    try:
        config = ProjectConfiguration.from_yaml(args.config)
    except Exception as exc:
        configure_logging(log_level=args.log_level or "error", structured=False)
        logger.error(
            "startup_configuration_failed",
            error=str(exc),
            failing_step="configuration_load",
            failure_phase="startup",
        )
        raise SystemExit(2) from exc

    resolved_output_mode = "structured" if args.structured_output else config.output_mode
    resolved_log_level = args.log_level or config.log_level
    configure_logging(log_level=resolved_log_level, structured=resolved_output_mode == "structured")

    try:
        asyncio.run(_run(config))
    except RuntimeExecutionError as exc:
        logger.error(
            "runtime_failure",
            error=str(exc),
            failing_step=exc.step,
            failure_phase=exc.phase,
        )
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.error(
            "startup_runtime_bootstrap_failed",
            error=str(exc),
            failing_step="bootstrap",
            failure_phase="startup",
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
