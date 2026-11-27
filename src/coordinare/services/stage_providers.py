"""402: building stage-provider services and routing a stage to them.

The services are built once at startup from the root ``stage_providers`` catalog and
registered by performer id. Which one a dispatch uses is decided per dispatch from the
symphony's effective config, so one symphony can opt in while the others keep their
performers.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import structlog

from coordinare.lifecycle import ROLE_TO_STAGE
from coordinare.services.progettare import ProgettareProvider
from coordinare.services.scrutare import ScrutareProvider
from coordinare.services.stage_contract import (
    STAGE_MARKERS,
    WORK_ITEM_STAGES,
    ExternalStageService,
    PullRequestReader,
    PullRequestSnapshot,
    StageProvider,
    WorkItemReader,
    WorkItemSnapshot,
    WorkItemStageService,
    stage_provider_id,
    web_host,
)
from coordinare.stage_provider_config import (
    PROVIDER_ROLES,
    ProgettareProviderConfig,
    StageProviderConfig,
    normalize_login,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from coordinare.config import ProjectConfiguration
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_STAGE_TO_ROLE = {stage: role for role, stage in ROLE_TO_STAGE.items()}


def build_provider(
    name: str, provider_config: StageProviderConfig, *, git_base_url: str = "https://github.com",
) -> StageProvider:
    """The provider client for one catalog entry, by its kind."""
    if isinstance(provider_config, ProgettareProviderConfig):
        return ProgettareProvider(name, provider_config, git_base_url=git_base_url)
    return ScrutareProvider(name, provider_config)


def provider_fix(name: str, provider_config: StageProviderConfig) -> str:
    """What to do when a provider's health check fails, for doctor and the preflight."""
    if isinstance(provider_config, ProgettareProviderConfig):
        return (
            f"Install progettare {provider_config.version} where coordinare runs "
            f"(stage_providers.{name}.command, now {provider_config.command!r}), with git and gh "
            f"on PATH; set {provider_config.gh_token_env} and every pass_env variable; and make "
            "runs_dir writable and config_path exist."
        )
    return (
        f"Start {provider_config.kind} or correct stage_providers.{name}.url (it must be "
        "reachable from coordinare), and set "
        f"{provider_config.token_env} to its performer API token."
    )


def github_hosts(config: Any) -> frozenset[str]:
    """403: the host coordinare clones from (``git_base_url``), which a planned issue must be on.

    The same host the clone goes to, port included, so the token only ever goes where
    coordinare already sends it.
    """
    base = str(getattr(config, "git_base_url", "") or "https://github.com")
    return frozenset({web_host(urlsplit(base).netloc) or "github.com"})


def build_stage_provider_services(
    config: ProjectConfiguration,
    pr_reader: PullRequestReader,
    work_item_reader: WorkItemReader | None = None,
) -> dict[str, ExternalStageService]:
    """One service per catalog entry and stage it can fill, keyed by performer id.

    403: a stage that plans a work item (architecting) gets a WorkItemStageService, which
    reads issues with ``work_item_reader``; without one, that stage is not served.
    """
    services: dict[str, ExternalStageService] = {}
    for name, provider_config in sorted(config.stage_providers.items()):
        provider = build_provider(
            name, provider_config, git_base_url=str(getattr(config, "git_base_url", "") or "https://github.com"),
        )
        for role in sorted(PROVIDER_ROLES.get(provider_config.kind, frozenset())):
            stage = ROLE_TO_STAGE[role]
            if stage not in STAGE_MARKERS:
                continue
            service: ExternalStageService
            if stage in WORK_ITEM_STAGES:
                if work_item_reader is None:
                    continue
                service = WorkItemStageService(
                    provider, stage=stage, work_item_reader=work_item_reader,
                    github_hosts=github_hosts(config),
                )
            else:
                service = ExternalStageService(provider, stage=stage, pr_reader=pr_reader)
            services[service.performer_id] = service
    return services


def role_provider(config: Any, stage: str) -> str | None:
    """The provider the effective config names for ``stage``, or None for a performer."""
    role = _STAGE_TO_ROLE.get(stage)
    performers = getattr(config, "performers", None)
    if role is None or performers is None:
        return None
    resolved = performers.resolved_role(role)
    provider = getattr(resolved, "provider", None) if resolved is not None else None
    return provider or None


def stage_provider_for(state: Any, stage: str) -> tuple[ExternalStageService | None, str | None]:
    """The provider service the effective config names for ``stage``.

    ``(None, None)`` when the stage runs on a performer; ``(None, reason)`` when it names
    a provider whose service was never started. Called before a performer slot is taken:
    a provider-backed stage holds no performer slot while the provider works.
    """
    name = role_provider(state.get("config"), stage)
    if name is None:
        return None, None
    by_id = state.get("performer_services_by_id") or {}
    target = by_id.get(stage_provider_id(name, stage))
    if not isinstance(target, ExternalStageService):
        return None, (
            f"performers.{_STAGE_TO_ROLE.get(stage, stage)}.provider names {name!r}, but no "
            f"{name!r} service was started; stage_providers is read from the global config, "
            "so declare it there"
        )
    return target, None


async def claim_stage_provider(
    state: CoordinareState, ctx: dict[str, Any],
) -> tuple[bool, CoordinareState | None]:
    """Route a dispatch to the stage's provider, before any performer slot is taken.

    ``(False, None)``: the stage runs on a performer, carry on. ``(True, None)``: the
    provider is ``ctx["service"]``. ``(True, state)``: the card is blocked with the reason.
    """
    provider, unroutable = stage_provider_for(state, str(ctx.get("performer_stage") or ""))
    if unroutable is not None:
        return True, await block_unroutable_stage(state, ctx, unroutable)
    if provider is None:
        return False, None
    from coordinare.graph.nodes.dispatch_performer import _restart_requires_dispatch

    # Re-derived on every dispatch from the persisted restart override, so a permission
    # from a dispatch that never reached the provider cannot linger.
    provider.set_fresh_attempt(
        str(ctx.get("card_id") or ""),
        allowed=_restart_requires_dispatch(state, ctx.get("performer_stage")),
    )
    state["slot_queued_since"] = None
    ctx["service"] = provider
    ctx["slot_manager"] = None  # no slot was taken, so none is released on an error path
    return True, None


async def refuse_provider_default(
    state: CoordinareState, ctx: dict[str, Any], service: Any, slot_manager: Any,
) -> CoordinareState | None:
    """Block a stage that names no provider but resolved to one; None when it did not.

    A provider is a stage's default only when no performer serves the stage. A symphony
    that names no provider must not be sent to it.
    """
    if not isinstance(service, ExternalStageService):
        return None
    stage = str(ctx.get("performer_stage") or "")
    if slot_manager is not None and hasattr(slot_manager, "release"):
        slot_manager.release(stage, str(ctx.get("card_id") or ""))
    return await block_unroutable_stage(state, ctx, (
        f"the {stage} stage names no provider for this symphony, and no performer "
        f"serves it; the global default is stage provider {service.provider_name!r}"
    ))


def provider_review_logins(config: Any) -> set[str]:
    """Normalised logins of the providers this config's roles use, for monitor_pr to skip.

    Only providers a role names here: a symphony that keeps its performer reviewer still
    treats the same service's webhook reviews like any other bot's.
    """
    providers = getattr(config, "stage_providers", None) or {}
    used = {role_provider(config, stage) for stage in _STAGE_TO_ROLE}
    return {
        normalize_login(p.review_login)
        for name, p in providers.items()
        if name in used and getattr(p, "review_login", None)
    }


def github_pr_reader(resolve: Callable[[str], Any]) -> PullRequestReader:
    """A pull request reader over GitHubService. ``resolve`` picks the service by owner."""

    async def read(repository: str, pr_node_id: str) -> PullRequestSnapshot:
        github = resolve(repository.split("/", 1)[0])
        if github is None:
            msg = f"no GitHub service can read {repository}"
            raise RuntimeError(msg)
        context = await github.get_pr_review_context(pr_node_id)
        return PullRequestSnapshot(
            head_sha=str(context.get("head_oid") or ""),
            reviews=list(context.get("reviews") or []),
        )

    return read


def github_work_item_reader(resolve: Callable[[str], Any]) -> WorkItemReader:
    """403: an issue reader over GitHubService. ``resolve`` picks the service by owner."""

    async def read(repository: str, issue_node_id: str) -> WorkItemSnapshot:
        github = resolve(repository.split("/", 1)[0])
        if github is None:
            msg = f"no GitHub service can read {repository}"
            raise RuntimeError(msg)
        details = await github.get_issue_details(issue_node_id)
        if not isinstance(details, dict) or not details:
            msg = f"GitHub returned no issue for {repository} ({issue_node_id})"
            raise RuntimeError(msg)
        return WorkItemSnapshot(
            title=str(details.get("title") or ""),
            body=str(details.get("body") or ""),
            state=str(details.get("state") or ""),
        )

    return read


async def block_unroutable_stage(
    state: CoordinareState, ctx: dict[str, Any], reason: str,
) -> CoordinareState:
    """Block the card with ``reason``: its stage cannot reach the service it names."""
    from coordinare.lib.redaction import redact_state_value
    from coordinare.services.board_provider import board_of, move_card_or_warn

    card_id = str(ctx.get("card_id") or "")
    logger.error(
        "dispatch_performer.stage_provider_unroutable",
        card_id=card_id,
        performer_stage=ctx.get("performer_stage"),
        reason=reason,
    )
    try:
        await move_card_or_warn(board_of(state), card_id, "BLOCKED")
    except Exception as exc:  # the block below still stands
        logger.warning("dispatch_performer.move_card_to_blocked_failed", card_id=card_id, error=str(exc))
    state["phase"] = "blocked"
    state["open_questions"] = redact_state_value([reason])
    return state
