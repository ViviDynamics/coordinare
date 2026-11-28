"""402: building stage-provider services and routing a stage to them.

The services are built once at startup from the root ``stage_providers`` catalog and
registered by performer id. Which one a dispatch uses is decided per dispatch from the
symphony's effective config, so one symphony can opt in while the others keep their
performers.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import structlog

from coordinare.lifecycle import ROLE_TO_STAGE
from coordinare.services.progettare import ProgettareProvider
from coordinare.services.qare import GitHubAccess, QAreProvider
from coordinare.services.scrutare import ScrutareProvider
from coordinare.services.stage_contract import (
    STAGE_MARKERS,
    WORK_ITEM_STAGES,
    ExternalStageService,
    PullRequestReader,
    PullRequestSnapshot,
    WorkItemReader,
    WorkItemSnapshot,
    WorkItemStageService,
    stage_provider_id,
    web_host,
)
from coordinare.services.stage_jobs import (
    SANDBOX_KUBERNETES_ISSUE,
    SANDBOX_RUNTIME,
    SANDBOX_WITH_EGRESS,
    DockerStageJobs,
    StageJobs,
    UnavailableStageJobs,
)
from coordinare.stage_provider_config import (
    PROVIDER_ROLES,
    ProgettareProviderConfig,
    QAreProviderConfig,
    StageProviderConfig,
    normalize_login,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from coordinare.config import ProjectConfiguration
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_STAGE_TO_ROLE = {stage: role for role, stage in ROLE_TO_STAGE.items()}


def build_stage_provider_services(
    config: ProjectConfiguration,
    pr_reader: PullRequestReader,
    work_item_reader: WorkItemReader | None = None,
    *,
    github: GitHubAccess | None = None,
) -> dict[str, ExternalStageService]:
    """One service per catalog entry and stage it can fill, keyed by performer id.

    403: a stage that plans a work item (architecting) gets a WorkItemStageService, which
    reads issues with ``work_item_reader``; without one, that stage is not served.
    """
    services: dict[str, ExternalStageService] = {}
    for name, provider_config in sorted(config.stage_providers.items()):
        provider = build_provider(name, provider_config, config, github=github)
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


def github_hosts(config: Any) -> frozenset[str]:
    """403: the host coordinare clones from (``git_base_url``), which a planned issue must be on.

    The same host the clone goes to, port included, so the token only ever goes where
    coordinare already sends it.
    """
    parts = urlsplit(str(getattr(config, "git_base_url", "") or "https://github.com"))
    # Issue URLs are http(s): a git:// base names the same host on another port.
    netloc = parts.netloc if parts.scheme in ("http", "https") else (parts.hostname or "")
    return frozenset({web_host(netloc) or "github.com"})


def build_provider(
    name: str,
    provider_config: StageProviderConfig,
    config: Any,
    *,
    github: GitHubAccess | None = None,
) -> ScrutareProvider | QAreProvider | ProgettareProvider:
    """The provider object for one catalog entry, by its kind."""
    if isinstance(provider_config, ProgettareProviderConfig):
        git_base_url = str(getattr(config, "git_base_url", "") or "https://github.com")
        jobs = stage_jobs_for(name, provider_config, config, role="architect")
        return ProgettareProvider(name, provider_config, git_base_url=git_base_url, jobs=jobs)
    if isinstance(provider_config, QAreProviderConfig):
        return QAreProvider(
            name, provider_config, work_dir=qare_work_dir(name, provider_config, config), github=github,
            jobs=stage_jobs_for(name, provider_config, config, role="qa"),
        )
    return ScrutareProvider(name, provider_config)


def stage_jobs_for(
    name: str, provider_config: QAreProviderConfig | ProgettareProviderConfig, config: Any, *, role: str,
) -> StageJobs:
    """423: where a provider's runs go: performer containers of the role's performer image.

    The image is the provider's own ``image``, else that of the performer endpoint that
    serves ``role``, else that of the first endpoint with one. The endpoint's isolation
    (its egress allowlist and extra hosts) applies to the runs as it does to the role's
    other work. Runs need Docker: under the Kubernetes transport they cannot start yet,
    and every use says so.

    435: a QAre provider with ``sandbox: sysbox`` runs its containers under the Sysbox runtime.
    """
    sandboxed = getattr(provider_config, "sandbox", None) == "sysbox"
    if str(getattr(config, "agent_transport", "") or "") == "kubernetes":
        sandbox_note = (
            f"; the QAre sandbox on Kubernetes is tracked in {SANDBOX_KUBERNETES_ISSUE}" if sandboxed else ""
        )
        return UnavailableStageJobs(
            f"stage provider {name!r} runs in a Docker performer container, and this coordinare "
            f"uses the Kubernetes transport; stage jobs on Kubernetes are not supported yet{sandbox_note}",
        )
    endpoints = [
        ep for ep in getattr(config, "performer_endpoints", None) or []
        if getattr(ep, "mode", "subprocess") != "subprocess" and getattr(ep, "image", None)
    ]
    endpoint = next((ep for ep in endpoints if role in (getattr(ep, "roles", None) or [])), None)
    endpoint = endpoint or (endpoints[0] if endpoints else None)
    if sandboxed and endpoint is not None and getattr(endpoint, "egress_allowlist", None):
        # 435: refused, naming the endpoint, which may be one that does not serve the role.
        return UnavailableStageJobs(
            f"stage provider {name!r} takes its isolation from performer endpoint "
            f"{getattr(endpoint, 'name', '?')!r} (it serves {role}, or is the first endpoint with an image), "
            f"and {SANDBOX_WITH_EGRESS}",
        )
    image = provider_config.image or (endpoint.image if endpoint is not None else None)
    if not image:
        return UnavailableStageJobs(
            f"stage provider {name!r} has no performer image to run in: set "
            f"stage_providers.{name}.image, or configure a performer endpoint with an image",
        )
    return DockerStageJobs(
        str(image),
        egress_allowlist=getattr(endpoint, "egress_allowlist", None) if endpoint is not None else None,
        extra_hosts=list(getattr(endpoint, "extra_hosts", None) or []) if endpoint is not None else [],
        runtime=SANDBOX_RUNTIME if sandboxed else None,
    )


def provider_fix(name: str, provider_config: StageProviderConfig) -> str:
    """What to do when a provider's health check fails, for doctor and the preflight."""
    if isinstance(provider_config, ProgettareProviderConfig):
        return (
            f"Install progettare {provider_config.version}, git and gh in the performer image its "
            f"runs use (stage_providers.{name}.image, else the architect's performer image; the full "
            f"performer image ships them), with Docker where coordinare runs; set "
            f"{provider_config.gh_token_env} and every pass_env variable; and make runs_dir "
            "writable and config_path exist."
        )
    if isinstance(provider_config, QAreProviderConfig):
        return (
            f"Install QAre {provider_config.version} in the performer image its runs use "
            f"(stage_providers.{name}.image, else the qa performer image; the full performer image "
            f"ships it), with Docker where coordinare runs; or correct stage_providers.{name}.command "
            "and .version; make work_dir writable."
            + (
                f" With sandbox: sysbox, the image must be the sandbox image (agent/performer/Dockerfile.sandbox) "
                f"and Docker must list the {SANDBOX_RUNTIME} runtime (Sysbox, Linux hosts only)."
                if provider_config.sandbox == "sysbox" else ""
            )
        )
    return (
        f"Start {provider_config.kind} or correct stage_providers.{name}.url (it must be "
        "reachable from coordinare), and set "
        f"{provider_config.token_env} to its performer API token."
    )


def qare_work_dir(name: str, provider_config: QAreProviderConfig, config: Any) -> Path:
    """404: where a QAre provider keeps its runs. Beside the state file unless configured."""
    if provider_config.work_dir:
        return Path(provider_config.work_dir).expanduser().absolute()
    state_file = Path(str(getattr(config, "state_file_path", "") or "coordinare.state.json")).expanduser()
    return state_file.absolute().parent / "stage-providers" / name


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


def github_access(resolve: Callable[[str], Any]) -> GitHubAccess:
    """404: a provider's token and REST base for a repository, from the owner's GitHubService."""

    def service(repository: str) -> Any:
        github = resolve(repository.split("/", 1)[0])
        if github is None:
            msg = f"no GitHub service can reach {repository}"
            raise RuntimeError(msg)
        return github

    async def token(repository: str) -> str:
        return str(await service(repository).current_token())

    def api_base(repository: str) -> str:
        base = getattr(service(repository), "rest_api_base", None)
        return str(base()) if callable(base) else "https://api.github.com"

    return GitHubAccess(token, api_base)


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
            base_sha=str(context.get("base_oid") or ""),
            base_ref=str(context.get("base_ref") or ""),
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
