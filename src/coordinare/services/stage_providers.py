"""402: building stage-provider services and routing a stage to them.

The services are built once at startup from the root ``stage_providers`` catalog and
registered by performer id. Which one a dispatch uses is decided per dispatch from the
symphony's effective config, so one symphony can opt in while the others keep their
performers.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from coordinare.lifecycle import ROLE_TO_STAGE
from coordinare.services.scrutare import ScrutareProvider
from coordinare.services.stage_contract import (
    STAGE_MARKERS,
    ExternalStageService,
    PullRequestReader,
    PullRequestSnapshot,
    stage_provider_id,
)
from coordinare.stage_provider_config import PROVIDER_ROLES, normalize_login

if TYPE_CHECKING:
    from collections.abc import Callable

    from coordinare.config import ProjectConfiguration
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_STAGE_TO_ROLE = {stage: role for role, stage in ROLE_TO_STAGE.items()}


def build_stage_provider_services(
    config: ProjectConfiguration,
    pr_reader: PullRequestReader,
) -> dict[str, ExternalStageService]:
    """One service per catalog entry and stage it can fill, keyed by performer id."""
    services: dict[str, ExternalStageService] = {}
    for name, provider_config in sorted(config.stage_providers.items()):
        provider = ScrutareProvider(name, provider_config)
        for role in sorted(PROVIDER_ROLES.get(provider_config.kind, frozenset())):
            stage = ROLE_TO_STAGE[role]
            if stage not in STAGE_MARKERS:
                continue
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

    if _restart_requires_dispatch(state, ctx.get("performer_stage")):
        # An operator restarted the stage: a blocked attempt may give way to a new one.
        provider.allow_fresh_attempt(str(ctx.get("card_id") or ""))
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
