from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import uvicorn

from coordinare import configure_logging
from coordinare.config import ProjectConfiguration
from coordinare.daemon import CoordinareDaemon, RuntimeExecutionError
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.health import create_health_app
from coordinare.metrics import METRICS
from coordinare.services.agent_service import AgentService
from coordinare.services.claude import ClaudeService
from coordinare.services.email import EmailService
from coordinare.services.github import GitHubService
from coordinare.services.slack import SlackService
from coordinare.state_store import StateStore
from coordinare.transport.kubernetes_transport import KubernetesTransport
from coordinare.transport.ssh_transport import SshTransport
from coordinare.transport.subprocess_transport import SubprocessTransport

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from coordinare.graph.state import CoordinareState
    from coordinare.transport import AgentTransport


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
    return create_health_app(daemon)


def _build_transport(config: ProjectConfiguration) -> AgentTransport:
    match config.agent_transport:
        case "subprocess":
            return SubprocessTransport(config.agent_executable, config.transport_timeout_seconds)
        case "ssh":
            return SshTransport()
        case "kubernetes":
            return KubernetesTransport()
        case _:
            msg = f"Unknown transport: {config.agent_transport!r}"
            raise ValueError(msg)


async def _bootstrap_services(config: ProjectConfiguration) -> CoordinareState:
    github = GitHubService(
        token=config.github_token.get_secret_value(),
        org=config.github_org,
        project_number=config.github_project_number,
    )
    await github.initialize()

    try:
        transport = _build_transport(config)
    except NotImplementedError as exc:
        logger.error(
            "transport_not_implemented",
            transport=config.agent_transport,
            message=str(exc),
        )
        sys.exit(1)

    service_state: CoordinareState = {
        "github_service": github,
        "agent_service": AgentService(transport),
        "claude_service": ClaudeService(api_key=os.getenv("ANTHROPIC_API_KEY")),
        "email_service": EmailService(
            host=config.smtp_host,
            port=config.smtp_port,
            username=config.smtp_username,
            password=config.smtp_password.get_secret_value() if config.smtp_password else None,
            sender=config.notification_email,
        ),
        "slack_service": SlackService(
            webhook_url=config.slack_webhook_url.get_secret_value(),
            channel=config.slack_channel,
        ),
        "human_reviewers": config.human_reviewers,
        "notification_email": config.notification_email,
        "blocked_reminder_hours": config.blocked_reminder_hours,
    }
    return service_state


async def _run(config: ProjectConfiguration) -> None:
    run_mode = os.getenv("COORDINARE_RUN_MODE", "shell").strip().lower() or "shell"
    graph = CoordinareGraphBuilder().build()
    # SC-005: heartbeat must not exceed 30s; cap here enforces the spec constraint
    # regardless of what config.heartbeat_interval_seconds is set to.
    effective_heartbeat = min(config.heartbeat_interval_seconds, 30)
    if effective_heartbeat < config.heartbeat_interval_seconds:
        logger.info(
            "heartbeat_interval_capped",
            configured_seconds=config.heartbeat_interval_seconds,
            effective_seconds=effective_heartbeat,
        )

    # T022: Construct StateStore and verify writable before daemon start
    state_store = StateStore(path=config.state_file_path, metrics=METRICS)
    try:
        state_store.verify_writable()
    except OSError as exc:
        logger.error(
            "state_path_not_writable",
            path=str(config.state_file_path),
            error=str(exc),
        )
        raise SystemExit(1) from exc

    daemon = CoordinareDaemon(
        graph,
        run_mode=run_mode,
        poll_interval_seconds=config.poll_interval_seconds,
        heartbeat_interval_seconds=effective_heartbeat,
        max_cycles=config.max_cycles,
        state_store=state_store,
    )

    daemon.state.update(await _bootstrap_services(config))

    app = _create_health_app(daemon)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=config.health_check_port, log_level="warning")
    )

    daemon_task = asyncio.create_task(daemon.start())
    server_task = asyncio.create_task(server.serve())

    try:
        METRICS.up.set(1)
        logger.info("startup_mode_selected", run_mode=run_mode)
        await daemon_task
    finally:
        METRICS.up.set(0)
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
