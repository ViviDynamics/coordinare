"""HTTP-mode adapter implementing AgentServiceProtocol (spec 056, T027).

Wraps :class:`~coordinare.transport.http_transport.PerformerHTTPClient` and the
container lifecycle service so the existing dispatch/monitor flow can route
ephemeral and persistent containerized performers through the same call sites
as the subprocess transport.

Mode handling:

* ``persistent`` — ``endpoint`` is fixed at registration; ``dispatch_card``
  sends ``POST /jobs`` directly.
* ``ephemeral``  — ``dispatch_card`` first calls
  :func:`performer_lifecycle.start_ephemeral` + ``wait_ready`` to acquire a
  one-shot container, then dispatches. The container_id is recorded so
  :meth:`check_status` can call :func:`performer_lifecycle.stop` when the
  job reaches a terminal state, regardless of success/failure (FR-003).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services import performer_lifecycle
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import (
    PerformerAuthError,
    PerformerHTTPClient,
    PerformerUnreachableError,
)

if TYPE_CHECKING:
    from coordinare.models.performer_endpoint import (
        JobInitPayload,
        PerformerEndpointConfig,
    )
    from coordinare.workspace import WorkspaceInfo

logger = structlog.get_logger(__name__)


_TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled"}


class HTTPPerformerService:
    """AgentService-compatible adapter for ephemeral / persistent performers."""

    def __init__(
        self,
        config: PerformerEndpointConfig,
        *,
        client: PerformerHTTPClient | None = None,
    ) -> None:
        self._config = config
        self._injected_client = client
        # Persistent: endpoint is known at construction time.
        # Ephemeral:  endpoint is resolved on dispatch.
        self._endpoint: str | None = (
            str(config.endpoint).rstrip("/") if config.endpoint is not None else None
        )
        self._client: PerformerHTTPClient | None = client
        self._container_id: str | None = None
        self._current_job_id: str | None = None
        # Serialise concurrent dispatch_card calls: a performer registered in
        # multiple stage pools (multi-role config) must not accept two jobs at once.
        self._dispatch_lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        return self._config.mode

    def _auth_token(self) -> str | None:
        if self._config.auth_token is None:
            return None
        return self._config.auth_token.get_secret_value()

    def _ensure_client(self) -> PerformerHTTPClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._client is None:
            if self._endpoint is None:
                raise TransportError(
                    f"performer {self._config.id} has no endpoint resolved yet"
                )
            self._client = PerformerHTTPClient(
                self._endpoint,
                auth_token=self._auth_token(),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._injected_client is None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # AgentServiceProtocol
    # ------------------------------------------------------------------
    async def check_health(self) -> dict[str, Any]:
        # Ephemeral performers have no live endpoint until dispatch — report
        # idle so dispatch_performer doesn't loop the health-retry budget.
        if self._endpoint is None and self._config.mode == "ephemeral":
            return {"status": "idle", "availability": "idle"}
        try:
            client = self._ensure_client()
            status = await client.get_status()
        except PerformerAuthError as exc:
            logger.warning("http_performer.auth_failed", performer_id=self._config.id, error=str(exc))
            return {"status": "error", "reason": str(exc)}
        except PerformerUnreachableError as exc:
            logger.warning(
                "http_performer.unreachable",
                performer_id=self._config.id,
                endpoint=self._endpoint,
                error=str(exc),
            )
            return {"status": "unreachable", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover — defensive
            return {"status": "unknown", "reason": str(exc)}
        # Map performer availability → AgentService health status.
        availability = status.availability
        caps = status.capabilities.model_dump(mode="json")
        if availability in {"idle", "busy"}:
            return {"status": "idle", "availability": availability, "capabilities": caps}
        if availability == "starting":
            return {"status": "unknown", "availability": availability, "capabilities": caps}
        if availability == "draining":
            return {"status": "draining", "availability": availability, "capabilities": caps}
        return {"status": "unknown", "availability": availability, "capabilities": caps}  # pragma: no cover — PerformerStatus.availability is Literal["starting","idle","busy","draining"], all handled above

    async def dispatch_card(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None = None,
    ) -> dict[str, Any]:
        async with self._dispatch_lock:
            return await self._dispatch_card_locked(card_context, workspace_info)

    async def _dispatch_card_locked(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None = None,
    ) -> dict[str, Any]:
        # Ephemeral mode: spin up a fresh container before dispatching.
        if self._config.mode == "ephemeral":
            try:
                started = await performer_lifecycle.start_ephemeral(self._config)
            except performer_lifecycle.LifecycleError as exc:
                logger.warning(
                    "http_performer.start_failed",
                    performer_id=self._config.id,
                    error=str(exc),
                )
                return {"status": "error", "reason": f"container start failed: {exc}"}
            self._container_id = started.container_id
            self._endpoint = started.endpoint
            # Drop any stale client; new endpoint requires a fresh one.
            if self._client is not None and self._injected_client is None:
                await self._client.aclose()
                self._client = None
            try:
                await performer_lifecycle.wait_ready(
                    self._endpoint,
                    self._auth_token(),
                    timeout=float(self._config.readiness_timeout_s),
                    performer_id=self._config.id,
                )
            except performer_lifecycle.ReadinessTimeoutError as exc:
                await self._cleanup_ephemeral()
                return {"status": "error", "reason": f"readiness timeout: {exc}"}

        # Build the job-init payload.
        try:
            payload = self._build_job_payload(card_context, workspace_info)
        except ValueError as exc:
            await self._cleanup_ephemeral()
            return {"status": "error", "reason": str(exc)}

        try:
            client = self._ensure_client()
            response = await client.post_job(payload)
        except (PerformerAuthError, PerformerUnreachableError, TransportError) as exc:
            await self._cleanup_ephemeral()
            logger.warning(
                "http_performer.dispatch_failed",
                performer_id=self._config.id,
                error=str(exc),
            )
            return {"status": "error", "reason": str(exc)}

        # 409 busy → surface as error so the dispatch loop retries next cycle.
        if not getattr(response, "accepted", False):
            reason = getattr(response, "reason", "busy")
            detail = getattr(response, "detail", None)
            await self._cleanup_ephemeral()
            return {
                "status": "error",
                "reason": f"performer busy ({reason})" + (f": {detail}" if detail else ""),
            }

        self._current_job_id = response.job_id
        logger.info(
            "performer_endpoint.transition",
            performer_id=self._config.id,
            from_state="idle",
            to_state="busy",
            job_id=response.job_id,
        )
        return {
            "status": "ok",
            "session_id": response.job_id,
            "job_id": response.job_id,
            "accepted": True,
        }

    async def check_status(
        self,
        session_id: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        status = await client.get_job(session_id)
        result: dict[str, Any] = status.model_dump(mode="json")
        if status.state in _TERMINAL_JOB_STATES:
            logger.info(
                "performer_endpoint.transition",
                performer_id=self._config.id,
                from_state="busy",
                to_state="idle",
                job_id=session_id,
                terminal_state=status.state,
            )
            if self._current_job_id == session_id:
                self._current_job_id = None
            if self._config.mode == "ephemeral":
                await self._cleanup_ephemeral()
        return result

    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]:
        # Containerized performers receive feedback as part of the next
        # dispatch payload (card_context["relay_feedback"]).  The wire
        # protocol has no in-flight feedback channel.
        logger.warning(
            "http_performer.relay_feedback_buffered",
            performer_id=self._config.id,
            comments=len(review_payload.get("comments", [])),
        )
        return {"status": "ok", "buffered": True}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _cleanup_ephemeral(self) -> None:
        if self._config.mode != "ephemeral":
            return
        container_id = self._container_id
        self._container_id = None
        if container_id is not None:
            try:
                await performer_lifecycle.stop(container_id)
            except Exception as exc:  # pragma: no cover — best effort
                logger.warning(
                    "http_performer.stop_failed",
                    performer_id=self._config.id,
                    container_id=container_id,
                    error=str(exc),
                )
        if self._client is not None and self._injected_client is None:
            await self._client.aclose()
            self._client = None
        self._endpoint = None

    def _build_job_payload(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None,
    ) -> JobInitPayload:
        from coordinare.models.performer_endpoint import JobInitPayload

        repo_url = (
            str(workspace_info.repo_url)
            if workspace_info is not None and workspace_info.repo_url
            else str(card_context.get("repo_url") or "")
        )
        branch = (
            workspace_info.branch
            if workspace_info is not None and workspace_info.branch
            else str(card_context.get("branch") or "")
        )
        if not repo_url or not branch:
            raise ValueError("workspace context missing repo_url or branch")

        secrets: dict[str, str] = {}
        if workspace_info is not None and workspace_info.github_token:
            secrets["GITHUB_TOKEN"] = workspace_info.github_token

        # Stash card_context as metadata so the performer has full access to
        # role/persona/relay_feedback/etc. without us forking the schema here.
        metadata = json.loads(json.dumps(dict(card_context), default=str))

        return JobInitPayload.model_validate(
            {
                "job_id": str(uuid.uuid4()),
                "card_id": str(card_context.get("id", "")),
                "role": str(card_context.get("role", "")),
                "backend": str(card_context.get("backend", "")),
                "persona": str(card_context.get("persona_instructions", "")),
                "repo_url": repo_url,
                "branch": branch,
                "secrets": secrets,
                "metadata": metadata,
            }
        )


__all__ = ["HTTPPerformerService"]
