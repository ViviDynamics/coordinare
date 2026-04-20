from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import platform
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn

from coordinare import configure_logging
from coordinare.auth import build_auth, validate_auth_config
from coordinare.config import ProjectConfiguration, ServiceCircuitConfig, ServiceRetryConfig
from coordinare.config_validation import _load_raw_yaml, validate_config
from coordinare.daemon import CoordinareDaemon, RuntimeExecutionError
from coordinare.dashboard import DashboardStore, check_port_available, create_dashboard_app
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.health import create_health_app
from coordinare.lifecycle import CANONICAL_ORDER as _CANONICAL_ORDER
from coordinare.lifecycle import ROLE_TO_STAGE as _ROLE_TO_STAGE
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
from coordinare.workspace import WorkspaceManager

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
    # Subcommand: `coordinare dry-run --card <card_id> [--config PATH]`
    dry_run_parser = subparsers.add_parser(
        "dry-run",
        help="Preview what coordinare would do for a card without side effects",
    )
    dry_run_parser.add_argument(
        "--card",
        required=True,
        help="Card/issue ID to preview",
    )
    dry_run_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        dest="config",
        help="Explicit path to config file",
    )

    # Ensure attributes always exist on the namespace regardless of which path is taken
    parser.set_defaults(command=None, config_action=None, strict=False, card=None)
    return parser


def _cmd_dry_run(args: argparse.Namespace) -> None:
    """Handle ``coordinare dry-run --card <card_id>`` subcommand."""
    from coordinare.dry_run import execute_dry_run

    explicit_path: Path | None = getattr(args, "config", None)
    result = validate_config(explicit_path)
    if not result.passed:
        for err in result.errors:
            print(f"Config error: {err.field_path} — {err.fix_hint}", file=sys.stderr)
        sys.exit(2)

    try:
        if result.config_file_path is not None:
            raw = _load_raw_yaml(result.config_file_path)
            config = ProjectConfiguration(**raw)
        else:
            config = ProjectConfiguration()
    except Exception as exc:
        print(f"Failed to load config: {exc}", file=sys.stderr)
        sys.exit(2)

    card_id: str = args.card
    dry_result = asyncio.run(execute_dry_run(card_id, config))

    # Print human-readable output to stdout
    print(f"\n=== Dry-Run Preview for card {card_id} ===\n")
    print(f"Assessment: {dry_result.assessment_result}")
    print(f"\nLifecycle stages ({len(dry_result.lifecycle_stages)}):")
    for i, stage in enumerate(dry_result.lifecycle_stages, 1):
        print(f"  {i}. {stage}")
    print(f"\nPlanned actions ({len(dry_result.planned_actions)}):")
    for action in dry_result.planned_actions:
        print(f"  - {action}")
    print(f"\nBoard transitions ({len(dry_result.board_transitions)}):")
    for t in dry_result.board_transitions:
        print(f"  {t['stage']} -> {t['column']}")
    print()


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
        wait_exp_base=src.wait_exp_base,
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
                "summary": f"🔴 {service_name} service is down — circuit breaker tripped ({reason})",
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


# ---------------------------------------------------------------------------
# 019 — Performer Lifecycle: sequence derivation and service registry
# ---------------------------------------------------------------------------

def _build_lifecycle_sequence(config: ProjectConfiguration) -> list[str]:
    """Derive the ordered lifecycle sequence from configured performer roles.

    Returns the list of performer_stage values (e.g. ["implementing", "reviewing"])
    in canonical order, filtered by which roles are non-None in config.performers.

    Falls back to ["implementing"] when no roles are configured but a legacy
    agent_service transport is available. Raises ValueError when no roles
    are configured and no legacy fallback exists.
    """
    sequence: list[str] = []
    for role in _CANONICAL_ORDER:
        role_config = getattr(config.performers, role, None)
        if role_config is not None:
            stage = _ROLE_TO_STAGE[role]
            sequence.append(stage)

    if sequence:
        return sequence

    # No roles configured — check for legacy backward-compatible fallback.
    # If the config has an agent_transport (the pre-019 single-implementer field),
    # fall back to implementer-only.  Note: agent_transport defaults to
    # "subprocess", so this branch is always taken for empty performers.
    # The ValueError below is a safety net for defensive coding.
    if config.agent_transport:
        return ["implementing"]

    msg = (
        "No performer roles configured. Add a 'performers' section to config.yaml "
        "with at least one role, or ensure the legacy agent_transport is set."
    )
    raise ValueError(msg)


def _build_transport_for_role(
    role_config: object,
    config: ProjectConfiguration,
) -> AgentTransport:
    """Build the appropriate transport for a performer role config.

    Uses role-specific overrides when available, falling back to the
    global config defaults.
    """
    from coordinare.config import PerformerRoleConfig

    if not isinstance(role_config, PerformerRoleConfig):
        return _build_transport(config)

    transport_type = role_config.transport or config.agent_transport
    executable = role_config.executable or config.agent_executable
    timeout = role_config.timeout_seconds or config.transport_timeout_seconds

    match transport_type:
        case "subprocess":
            return SubprocessTransport(executable, timeout)
        case "ssh":
            return SshTransport()
        case "kubernetes":
            return KubernetesTransport()
        case _:
            msg = f"Unknown transport: {transport_type!r}"
            raise ValueError(msg)


def _build_performer_services(
    config: ProjectConfiguration,
    circuit_breakers: dict[str, CircuitBreaker],
) -> dict[str, Any]:
    """Build the performer_services registry from config.

    Returns a mapping of stage name → ResilientAgentService for each
    configured performer role.  The first service instance per role is
    used as the primary for backward-compatible single-service lookup.

    048: Also builds ``max_concurrency`` transport instances per role for
    the SlotManager.  The full list is stashed on
    ``_build_performer_services._service_lists`` (stage →
    list[ResilientAgentService]) so the caller can read it after the
    function returns and register pools on the SlotManager.

    Note: ``PerformerRoleConfig.backend`` and ``image``/``host``/``port``
    are stored in config for operator documentation and future use.
    Transport selection currently uses ``transport`` and ``executable``;
    additional fields will be wired in when provider-specific transport
    constructors are added (e.g. a Claude-Code-specific subprocess mode).
    """
    from coordinare.lifecycle import SINGLETON_STAGES

    r = config.resilience
    services: dict[str, Any] = {}
    # 048: full service lists for SlotManager — stashed as a function
    # attribute so the caller can build SlotManager pools after return.
    service_lists: dict[str, list[Any]] = {}

    for role in _CANONICAL_ORDER:
        role_config = getattr(config.performers, role, None)
        if role_config is None:
            continue

        stage = _ROLE_TO_STAGE[role]
        max_concurrency = getattr(role_config, "max_concurrency", 1)
        if stage in SINGLETON_STAGES:
            max_concurrency = min(max_concurrency, 1)
        if max_concurrency <= 0:
            logger.info(
                "performer_role_disabled",
                role=role,
                stage=stage,
                msg=f"Role {role!r} disabled (max_concurrency=0)",
            )
            continue

        role_services: list[Any] = []
        for i in range(max_concurrency):
            try:
                transport = _build_transport_for_role(role_config, config)
            except (NotImplementedError, ValueError) as exc:
                if i == 0:
                    logger.warning(
                        "performer_transport_build_failed.role_skipped",
                        role=role,
                        stage=stage,
                        transport=getattr(role_config, "transport", None),
                        error=str(exc),
                        msg=f"Skipping performer role {role!r} — transport is not available",
                    )
                else:
                    logger.warning(
                        "performer_transport_build_failed.partial_concurrency",
                        role=role,
                        stage=stage,
                        instance=i,
                        max_concurrency=max_concurrency,
                        built=len(role_services),
                        error=str(exc),
                        msg=f"Role {role!r} built only {len(role_services)}/{max_concurrency} transport(s)",
                    )
                break
            agent_svc = AgentService(transport)
            resilient = ResilientAgentService(
                inner=agent_svc,
                retry_config=_retry_config_from(r.agent_retry),
                circuit_breaker=circuit_breakers["agent"],
            )
            role_services.append(resilient)

        if role_services:
            services[stage] = role_services[0]  # primary for backward compat
            service_lists[stage] = role_services

    # Stash the full lists for SlotManager construction
    _build_performer_services._service_lists = service_lists  # type: ignore[attr-defined]

    return services


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
    config_path: Path | None = None,
) -> CoordinareState:
    r = config.resilience

    _auth = build_auth(config)
    github = GitHubService(
        auth=_auth,
        org=config.github_org,
        project_number=config.github_project_number,
        endpoint=config.github_graphql_url,
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

    from coordinare.services.assessment import build_assessment_backend
    assessment_backend = build_assessment_backend(
        config,
        circuit_breaker=circuit_breakers["anthropic"],
        retry_kwargs=_retry_config_from(r.anthropic_retry).to_stamina_kwargs(),
    )

    workspace_manager = WorkspaceManager(config, auth=_auth)

    # 019 — Build performer lifecycle registry
    lifecycle_sequence = _build_lifecycle_sequence(config)
    performer_services = _build_performer_services(config, circuit_breakers)

    # 048 — Build SlotManager from the per-role service lists
    from coordinare.services.slot_manager import SlotManager
    slot_manager = SlotManager()
    service_lists: dict[str, list] = getattr(
        _build_performer_services, "_service_lists", {},
    )
    for stage, svc_list in service_lists.items():
        role_name = None
        for r_name, s_name in _ROLE_TO_STAGE.items():
            if s_name == stage:
                role_name = r_name
                break
        max_c = 1
        if role_name is not None:
            rc = getattr(config.performers, role_name, None)
            if rc is not None:
                max_c = getattr(rc, "max_concurrency", 1)
        slot_manager.register_pool(stage, svc_list, max_c)

    # If no explicit performer roles are configured but legacy agent_service exists,
    # register it as the implementer service for backward compatibility.
    if not performer_services and "implementing" in lifecycle_sequence:
        performer_services["implementing"] = resilient_agent

    # Warn when lifecycle expects stages that have no service (e.g. all
    # configured transports failed to build).  These stages will be skipped
    # by dispatch_performer at runtime — this is by design (FR-013).
    missing_stages = [s for s in lifecycle_sequence if s not in performer_services]
    if missing_stages:
        logger.warning(
            "performer_services.missing_stages",
            missing=missing_stages,
            available=list(performer_services.keys()),
            msg="Some lifecycle stages have no service — they will be skipped at dispatch time",
        )
        # Remove missing stages from lifecycle_sequence so dispatch_performer
        # doesn't need to skip them on every cycle.
        lifecycle_sequence = [s for s in lifecycle_sequence if s in performer_services]
        if not lifecycle_sequence:
            # All configured transports failed — fall back to legacy mode
            # only if the legacy agent_service is available.
            if "implementing" not in performer_services:
                logger.error(
                    "performer_services.all_transports_failed",
                    msg="All configured performer transports failed to build and no legacy fallback is available",
                )
            lifecycle_sequence = ["implementing"]

    # 027: Build per-role timeout mapping (only for explicitly configured roles)
    role_timeouts: dict[str, int] = {}
    for role in _CANONICAL_ORDER:
        role_config = getattr(config.performers, role, None)
        if role_config is not None and role_config.timeout_seconds is not None:
            stage = _ROLE_TO_STAGE[role]
            role_timeouts[stage] = role_config.timeout_seconds

    service_state: CoordinareState = {
        "config": config,
        "config_path": config_path,
        "github_service": github,
        "agent_service": resilient_agent,
        "claude_service": claude_service,
        "assessment_backend": assessment_backend,
        "notification_service": notification_service,
        "human_reviewers": config.human_reviewers,
        "trusted_bot_reviewers": config.trusted_bot_reviewers,
        "blocked_reminder_hours": config.blocked_reminder_hours,
        "workspace_manager": workspace_manager,
        "performer_services": performer_services,
        "lifecycle_sequence": lifecycle_sequence,
        "performer_stage": lifecycle_sequence[0] if lifecycle_sequence else "implementing",
        "role_timeouts": role_timeouts,
        "slot_manager": slot_manager,
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


async def _run(config: ProjectConfiguration, config_path: Path | None = None) -> None:
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

    # Probe both ports before starting any servers so a conflict produces a single
    # clean error rather than uvicorn's full asyncio traceback.
    check_port_available("0.0.0.0", config.health_check_port, label="health")
    check_port_available(config.dashboard_host, config.dashboard_port, label="dashboard")

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

    daemon.state.update(await _bootstrap_services(config, circuit_breakers, config_path=config_path))

    app = _create_health_app(daemon, circuit_breakers=circuit_breakers)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=config.health_check_port,
            log_level="warning",
            # Disable uvicorn's own signal handlers — we install a unified
            # handler below so Ctrl+C cancels the daemon task immediately.
        )
    )
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    dashboard_app = create_dashboard_app(dashboard_store, daemon, METRICS, HEALTH, config_path=config_path)

    if config.webhooks.enabled and config.webhooks.secret:
        from coordinare.dashboard import register_webhook_route
        register_webhook_route(
            dashboard_app,
            path=config.webhooks.path,
            secret=config.webhooks.secret.get_secret_value(),
            trigger=daemon._webhook_trigger,
        )

    dashboard_server = uvicorn.Server(
        uvicorn.Config(
            dashboard_app,
            host=config.dashboard_host,
            port=config.dashboard_port,
            log_level="warning",
            timeout_graceful_shutdown=3,  # max wait after SSE streams are signalled
        )
    )
    dashboard_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    daemon_task = asyncio.create_task(daemon.start())
    server_task = asyncio.create_task(server.serve())
    dashboard_task = asyncio.create_task(dashboard_server.serve())
    logger.info(
        "dashboard_started",
        host=config.dashboard_host,
        port=config.dashboard_port,
    )

    # Unified signal handler: stop the daemon (which cancels its task) and
    # signal both HTTP servers to exit.  Installed after tasks are created so
    # it is never overwritten by uvicorn's own handler installation.
    def _on_signal() -> None:
        daemon.stop()
        server.should_exit = True
        dashboard_store.shutdown()  # wake SSE streams so they exit before uvicorn times out
        dashboard_server.should_exit = True

    loop = asyncio.get_running_loop()
    for _sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(_sig, _on_signal)

    try:
        METRICS.daemon_up.set(1)
        logger.info("startup_mode_selected", run_mode=run_mode)
        await daemon_task
    finally:
        METRICS.daemon_up.set(0)
        server.should_exit = True
        dashboard_store.shutdown()
        dashboard_server.should_exit = True
        await server_task
        await dashboard_task


def main() -> None:
    args = _build_arg_parser().parse_args()

    # Dispatch config subcommand before any daemon startup
    if getattr(args, "command", None) == "config" and getattr(args, "config_action", None) == "validate":
        _cmd_config_validate(args)
        return  # _cmd_config_validate always calls sys.exit; this is belt-and-suspenders

    # Dispatch dry-run subcommand before daemon startup
    if getattr(args, "command", None) == "dry-run":
        _cmd_dry_run(args)
        return

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

    # Step 4b: Validate auth config (key file exists for App mode)
    validate_auth_config(config)

    # Step 4c: Warn when polling is disabled but no webhook trigger is configured
    if config.poll_interval_seconds == 0 and not config.webhooks.enabled:
        logger.warning("no_trigger_source_configured")

    # Step 5: Record build info (static metadata; set once at startup)
    _started_at = datetime.now(UTC).isoformat()
    METRICS.build_info.info({
        "version": _coordinare_version(),
        "python_version": platform.python_version(),
        "started_at": _started_at,
    })

    # Step 6: Initialize HEALTH registry with configured subsystems
    from coordinare.observability import HealthStatus as _HealthStatus

    # Stale-detection timeout must exceed the poll cycle duration so a healthy
    # probe doesn't flip to "degraded" between normal cycles.
    HEALTH.configure(timeout_seconds=config.poll_interval_seconds + 10)
    # github, agent, config: required unless opted out — daemon actively updates these probes
    for _subsystem in ("github", "agent", "config"):
        _required = _subsystem not in config.optional_subsystems
        HEALTH.register(_subsystem, required=_required)
    # notifications: mark healthy at startup if channels are configured.
    # The slack/smtp circuit breakers reflect delivery failures; this probe
    # simply shows whether the notification system is configured.
    HEALTH.register("notifications", required=False)
    if config.notifications.channels:
        HEALTH.update("notifications", _HealthStatus.healthy)
    # config subsystem is healthy once we reach this point
    HEALTH.update("config", _HealthStatus.healthy)

    try:
        asyncio.run(_run(config, config_path=result.config_file_path))
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
