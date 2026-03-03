from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

import structlog
import uvicorn

from coordinare import configure_logging
from coordinare.config import ProjectConfiguration, ServiceCircuitConfig, ServiceRetryConfig
from coordinare.config_validation import _load_raw_yaml, validate_config
from coordinare.daemon import CoordinareDaemon, RuntimeExecutionError
from coordinare.dashboard import DashboardStore, check_port_available, create_dashboard_app
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.health import create_health_app
from coordinare.metrics import METRICS, _coordinare_version
from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity
from coordinare.observability import HEALTH
from coordinare.resilience import CircuitBreaker, ResilientAgentService, RetryConfig
from coordinare.services.advocate import AdvocateService
from coordinare.services.agent_service import AgentService
from coordinare.services.claude import ClaudeService
from coordinare.services.github import GitHubService
from coordinare.services.notification import NotificationService, build_notification_service
from coordinare.state_store import StateStore
from coordinare.transport.kubernetes_transport import KubernetesTransport
from coordinare.transport.ssh_transport import SshTransport
from coordinare.transport.subprocess_transport import SubprocessTransport

logger = structlog.get_logger(__name__)

# Background tasks set — keeps strong references so tasks aren't GC'd before completion.
_background_tasks: set[asyncio.Task[object]] = set()

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from coordinare.graph.state import CoordinareState
    from coordinare.transport import AgentTransport


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the coordinare daemon")
    # Daemon flags (top-level)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config file (overrides discovery order)",
    )
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
    # Subcommands: `coordinare config validate [--config PATH] [--strict]`
    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="Config management subcommands")
    config_subparsers = config_parser.add_subparsers(dest="config_action")
    validate_parser = config_subparsers.add_parser(
        "validate",
        help="Validate config against the current schema without starting the daemon",
    )
    validate_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        dest="config",
        help="Explicit path to config file",
    )
    validate_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat deprecated fields as errors (exit 1)",
    )
    # Ensure attributes always exist on the namespace regardless of which path is taken
    parser.set_defaults(command=None, config_action=None, strict=False)
    return parser


def _searched_paths_lines(explicit_path: Path | None) -> list[str]:
    """Build the 4-path search order display for the 'no config file found' output."""
    lines = []
    if explicit_path is not None:
        lines.append(f"  1. {explicit_path} — not found (or not a file)")
    else:
        lines.append("  1. (--config flag not provided)")

    env_path_str = os.environ.get("COORDINARE_CONFIG_PATH")
    if env_path_str:
        lines.append(f"  2. $COORDINARE_CONFIG_PATH={env_path_str} — not found (or not a file)")
    else:
        lines.append("  2. (COORDINARE_CONFIG_PATH not set)")

    lines.append("  3. ./config.yaml — not found")
    lines.append("  4. ~/.coordinare/config.yaml — not found")
    return lines


def _cmd_config_validate(args: argparse.Namespace) -> None:
    """Handle `coordinare config validate` subcommand. Always calls sys.exit."""
    explicit_path: Path | None = getattr(args, "config", None)
    strict: bool = getattr(args, "strict", False)

    result = validate_config(explicit_path)

    if result.passed:
        if result.config_file_path is not None:
            print(f"✓ Config valid — loaded from {result.config_file_path}")
        else:
            print("✓ Config valid — no config file (all required fields supplied via environment variables)")

        env_count = result.env_var_fields_count
        if result.config_file_path is not None:
            print(
                f"  Fields resolved: {env_count} from environment variables, "
                "remaining from file or default values"
            )
        else:
            print(f"  Fields resolved: {env_count} from environment variables")

        if result.warnings:
            header = "DEPRECATION ERRORS" if strict else "DEPRECATION WARNINGS (use --strict to treat as errors)"
            print(f"\n{header}:")
            for w in result.warnings:
                print(f"  [DEPRECATED] {w.field_name}")
                print(f"    Removed in: {w.removed_in}")
                print(f"    Replace with: {w.replacement_path}")
                print(f"    Migration: {w.migration_hint}")

        sys.exit(1 if (strict and result.warnings) else 0)

    # Validation failed
    if result.config_file_path is not None:
        print(f"✗ Config validation failed — loaded from {result.config_file_path}")
    else:
        # Check whether this is an explicit-path error or a "no file found" case
        config_file_error = next(
            (e for e in result.errors if e.field_path == "config_file"), None
        )
        if config_file_error:
            print(f"✗ Config validation failed — {config_file_error.fix_hint}")
        else:
            print("✗ Config validation failed — no config file found")
            print("\nSearched paths:")
            for line in _searched_paths_lines(explicit_path):
                print(line)
            print(
                "\nFix: Create a config file at one of the above paths, or supply "
                "all required fields via COORDINARE_* environment variables."
            )

    # Print non-config-file errors
    non_file_errors = [e for e in result.errors if e.field_path != "config_file"]
    if non_file_errors:
        print("\nERRORS:")
        for e in non_file_errors:
            label = e.error_type.value.upper()
            print(f"  [{label}] {e.field_path}")
            if e.source and e.source.startswith("env_var:"):
                print(f"    Source: {e.source}")
            print(f"    {e.fix_hint}")

    if result.warnings:
        header = "DEPRECATION ERRORS" if strict else "DEPRECATION WARNINGS (use --strict to treat as errors)"
        print(f"\n{header}:")
        for w in result.warnings:
            print(f"  [DEPRECATED] {w.field_name}")
            print(f"    Removed in: {w.removed_in}")
            print(f"    Replace with: {w.replacement_path}")
            print(f"    Migration: {w.migration_hint}")

    sys.exit(1)


def _create_health_app(
    daemon: CoordinareDaemon,
    circuit_breakers: dict[str, CircuitBreaker] | None = None,
) -> FastAPI:
    return create_health_app(daemon, circuit_breakers=circuit_breakers or {})


def _retry_config_from(src: ServiceRetryConfig) -> RetryConfig:
    return RetryConfig(
        attempts=src.attempts,
        wait_initial=src.wait_initial_seconds,
        wait_max=src.wait_max_seconds,
        wait_jitter=src.wait_jitter_seconds,
    )


def _circuit_breaker_from(name: str, cfg: ServiceCircuitConfig) -> CircuitBreaker:
    return CircuitBreaker(
        service_name=name,
        failure_threshold=cfg.failure_threshold,
        recovery_window=cfg.recovery_window_seconds,
        observation_window=cfg.observation_window_seconds,
    )


def _make_trip_callback(
    notification_service: NotificationService,
) -> Callable[[str, str], None]:
    def callback(service_name: str, reason: str) -> None:
        event = NotificationEvent(
            event_type=EventType.circuit_breaker_trip,
            severity=NotificationSeverity.critical,
            payload={
                "event_type": EventType.circuit_breaker_trip.value,
                "severity": NotificationSeverity.critical.value,
                "source": "resilience",
                "summary": f"Circuit breaker OPEN: {service_name} ({reason})",
                "service_name": service_name,
                "reason": reason,
            },
            source="resilience",
            dedup_key=f"circuit_breaker_trip:{service_name}",
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No event loop running — skip dispatch
        task = loop.create_task(notification_service.dispatch(event))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return callback


def _build_circuit_breakers(config: ProjectConfiguration) -> dict[str, CircuitBreaker]:
    r = config.resilience
    return {
        "github": _circuit_breaker_from("github", r.github_circuit),
        "slack": _circuit_breaker_from("slack", r.slack_circuit),
        "smtp": _circuit_breaker_from("smtp", r.smtp_circuit),
        "anthropic": _circuit_breaker_from("anthropic", r.anthropic_circuit),
        "agent": _circuit_breaker_from("agent", r.agent_circuit),
    }


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


async def _bootstrap_services(
    config: ProjectConfiguration,
    circuit_breakers: dict[str, CircuitBreaker],
) -> CoordinareState:
    r = config.resilience

    github = GitHubService(
        token=config.github_token.get_secret_value(),
        org=config.github_org,
        project_number=config.github_project_number,
        circuit_breaker=circuit_breakers["github"],
        retry_kwargs=_retry_config_from(r.github_retry).to_stamina_kwargs(),
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

    agent_service = AgentService(transport)
    resilient_agent = ResilientAgentService(
        inner=agent_service,
        retry_config=_retry_config_from(r.agent_retry),
        circuit_breaker=circuit_breakers["agent"],
    )

    notification_service = build_notification_service(config.notifications, METRICS)
    trip_callback = _make_trip_callback(notification_service)
    for cb in circuit_breakers.values():
        cb.on_open_callback = trip_callback

    claude_service = ClaudeService(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        circuit_breaker=circuit_breakers["anthropic"],
        retry_kwargs=_retry_config_from(r.anthropic_retry).to_stamina_kwargs(),
    )

    service_state: CoordinareState = {
        "github_service": github,
        "agent_service": resilient_agent,
        "claude_service": claude_service,
        "notification_service": notification_service,
        "human_reviewers": config.human_reviewers,
        "blocked_reminder_hours": config.blocked_reminder_hours,
    }

    if config.advocate.enabled:
        try:
            label_ids = await github.ensure_labels_exist(
                config.github_org,
                config.advocate.github_repo,
                config.advocate.handled_label,
                config.advocate.escalation_label,
            )
        except Exception as exc:
            logger.warning("advocate_label_setup_failed", error=str(exc))
            label_ids = {}

        from coordinare.services.scoring import ClaudeScorer

        # V1: scoring_models config is reserved for future multi-provider support
        # (OpenAI, GitHub Copilot). Until additional ScoringProviderProtocol
        # implementations exist, ClaudeScorer is always the sole provider.
        # Adding a new provider in V2 requires registering it here; the
        # advocate_scan node itself requires no changes (FR-005).
        advocate_service = AdvocateService(
            github=github,
            notification_service=notification_service,
            config=config.advocate,
            github_org=config.github_org,
            label_ids=label_ids,
            scorers=[ClaudeScorer(claude_service)],
        )
        service_state["advocate_service"] = advocate_service
        service_state["advocate_handled_label"] = config.advocate.handled_label
        service_state["advocate_escalation_label"] = config.advocate.escalation_label
    else:
        service_state["advocate_service"] = None

    return service_state


async def _run(config: ProjectConfiguration) -> None:
    run_mode = os.getenv("COORDINARE_RUN_MODE", "shell").strip().lower() or "shell"
    graph = CoordinareGraphBuilder().build()

    # Build circuit breakers and register stamina retry counter hook
    circuit_breakers = _build_circuit_breakers(config)
    import stamina

    def _on_retry(details: stamina.instrumentation.RetryDetails) -> None:
        METRICS.service_retries_total.labels(service=details.name, action="retry").inc()
        logger.warning(
            "service.retry_attempt",
            service=details.name,
            attempt=details.retry_num,
            error_type=type(details.caused_by).__name__,
            error=str(details.caused_by),
            wait_seconds=details.wait_for,
            waited_so_far=details.waited_so_far,
        )

    stamina.instrumentation.set_on_retry_hooks([_on_retry])
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

    # Dashboard: check port availability before starting servers (FR-001)
    check_port_available(config.dashboard_host, config.dashboard_port)

    dashboard_store = DashboardStore()

    daemon = CoordinareDaemon(
        graph,
        run_mode=run_mode,
        poll_interval_seconds=config.poll_interval_seconds,
        heartbeat_interval_seconds=effective_heartbeat,
        max_cycles=config.max_cycles,
        state_store=state_store,
        idle_threshold_seconds=config.notifications.prolonged_idle_threshold_seconds,
        dashboard_store=dashboard_store,
    )

    daemon.state.update(await _bootstrap_services(config, circuit_breakers))

    app = _create_health_app(daemon, circuit_breakers=circuit_breakers)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=config.health_check_port, log_level="warning")
    )

    dashboard_app = create_dashboard_app(dashboard_store, daemon, METRICS, HEALTH)
    dashboard_server = uvicorn.Server(
        uvicorn.Config(
            dashboard_app,
            host=config.dashboard_host,
            port=config.dashboard_port,
            log_level="warning",
        )
    )

    daemon_task = asyncio.create_task(daemon.start())
    server_task = asyncio.create_task(server.serve())
    dashboard_task = asyncio.create_task(dashboard_server.serve())
    logger.info(
        "dashboard_started",
        host=config.dashboard_host,
        port=config.dashboard_port,
    )

    try:
        METRICS.daemon_up.set(1)
        logger.info("startup_mode_selected", run_mode=run_mode)
        await daemon_task
    finally:
        METRICS.daemon_up.set(0)
        server.should_exit = True
        dashboard_server.should_exit = True
        await server_task
        await dashboard_task


def main() -> None:
    args = _build_arg_parser().parse_args()

    # Dispatch config subcommand before any daemon startup
    if getattr(args, "command", None) == "config" and getattr(args, "config_action", None) == "validate":
        _cmd_config_validate(args)
        return  # _cmd_config_validate always calls sys.exit; this is belt-and-suspenders

    # --- Daemon startup path ---
    # Step 1: Validate config (discover + parse + env var merge) in one pass
    _config_t0 = perf_counter()
    result = validate_config(getattr(args, "config", None))
    if not result.passed:
        configure_logging(
            log_level=args.log_level or "error",
            structured=getattr(args, "structured_output", False),
        )
        for err in result.errors:
            logger.error(
                "config_validation_error",
                field=err.field_path,
                error_type=err.error_type.value,
                hint=err.fix_hint,
                failing_step="configuration_load",
                failure_phase="startup",
            )
        raise SystemExit(2)

    # Step 2: Re-instantiate ProjectConfiguration from resolved path
    # (validate_config already verified this succeeds; re-instantiate to get the typed object)
    try:
        if result.config_file_path is not None:
            raw = _load_raw_yaml(result.config_file_path)
            config = ProjectConfiguration(**raw)
        else:
            config = ProjectConfiguration()
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

    # Step 3: Log deprecation warnings at startup (non-strict; daemon always warns)
    for warning in result.warnings:
        logger.warning(
            "config_deprecated_field",
            field=warning.field_name,
            removed_in=warning.removed_in,
            replacement=warning.replacement_path,
        )

    # Step 4: SC-006 structured startup log entry (T024)
    _config_elapsed = perf_counter() - _config_t0
    METRICS.config_load_duration_seconds.observe(_config_elapsed)
    logger.info(
        "config_loaded",
        config_file=str(result.config_file_path) if result.config_file_path else "none",
        env_var_fields_count=result.env_var_fields_count,
        deprecated_fields_detected=bool(result.warnings),
    )

    # Step 5: Record build info (static metadata; set once at startup)
    _started_at = datetime.now(UTC).isoformat()
    METRICS.build_info.info({
        "version": _coordinare_version(),
        "python_version": platform.python_version(),
        "started_at": _started_at,
    })

    # Step 6: Initialize HEALTH registry with configured subsystems
    from coordinare.observability import HealthStatus as _HealthStatus

    HEALTH.configure(timeout_seconds=config.health_check_timeout_seconds)
    # github, agent_ssh, config: required unless opted out — daemon actively updates these probes
    for _subsystem in ("github", "agent_ssh", "config"):
        _required = _subsystem not in config.optional_subsystems
        HEALTH.register(_subsystem, required=_required)
    # notifications: daemon does not call HEALTH.update for this subsystem;
    # registering as required=False ensures it never blocks readiness.
    HEALTH.register("notifications", required=False)
    # config subsystem is healthy once we reach this point
    HEALTH.update("config", _HealthStatus.healthy)

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
