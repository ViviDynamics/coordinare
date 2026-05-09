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
import contextlib
import json
import uuid
from dataclasses import dataclass
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
        VolumeMount,
    )
    from coordinare.workspace import WorkspaceInfo

logger = structlog.get_logger(__name__)


_TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled"}


@dataclass
class _EphemeralJob:
    """State for one in-flight ephemeral container job."""
    container_id: str
    endpoint: str
    client: PerformerHTTPClient


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
        # Ephemeral:  per-job state tracked in _active_jobs keyed by job_id.
        self._persistent_endpoint: str | None = (
            str(config.endpoint).rstrip("/") if config.endpoint is not None else None
        )
        self._persistent_client: PerformerHTTPClient | None = client
        # Ephemeral jobs: job_id → _EphemeralJob (replaces shared _container_id/_endpoint/_client)
        self._active_jobs: dict[str, _EphemeralJob] = {}
        # Serialise concurrent dispatch_card calls: a performer registered in
        # multiple stage pools (multi-role config) must not accept two jobs at once.
        self._dispatch_lock = asyncio.Lock()
        # Rolling buffer of container stdout+stderr lines for the active job,
        # refreshed every ~5 s by _poll_container_logs(). Readable via get_agent_logs().
        self._log_buffer: list[str] = []
        # Per-job background poll tasks: job_id → Task (mirrors _active_jobs).
        self._log_poll_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def mode(self) -> str:
        return self._config.mode

    @property
    def devenv_root(self) -> str:
        return self._config.container_devenv_root

    def _auth_token(self) -> str | None:
        if self._config.auth_token is None:
            return None
        return self._config.auth_token.get_secret_value()

    def _ensure_client(self, job_id: str | None = None) -> PerformerHTTPClient:
        if self._injected_client is not None:
            return self._injected_client
        # Ephemeral: look up per-job client.
        if self._config.mode == "ephemeral" and job_id is not None:
            job = self._active_jobs.get(job_id)
            if job is not None:
                return job.client
        # Persistent: use shared client.
        if self._persistent_client is None:
            if self._persistent_endpoint is None:
                raise TransportError(
                    f"performer {self._config.id} has no endpoint resolved yet"
                )
            self._persistent_client = PerformerHTTPClient(
                self._persistent_endpoint,
                auth_token=self._auth_token(),
            )
        return self._persistent_client

    async def aclose(self) -> None:
        if self._persistent_client is not None and self._injected_client is None:
            await self._persistent_client.aclose()
            self._persistent_client = None
        # Cancel poll tasks first so they don't race against container teardown.
        for task in self._log_poll_tasks.values():
            task.cancel()
        self._log_poll_tasks.clear()
        # Cancel any in-flight ephemeral containers so they don't leak on shutdown.
        active = list(self._active_jobs.values())
        self._active_jobs.clear()
        for job in active:
            await self._cleanup_ephemeral_job(job)

    # ------------------------------------------------------------------
    # AgentServiceProtocol
    # ------------------------------------------------------------------
    async def check_health(self) -> dict[str, Any]:
        # Ephemeral performers have no persistent endpoint to probe — concurrency
        # is governed by SlotManager (checked before health), so always report idle.
        if self._config.mode == "ephemeral" and self._persistent_endpoint is None:
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
                endpoint=self._persistent_endpoint,
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
        extra_volumes: list[VolumeMount] | None = None,
    ) -> dict[str, Any]:
        async with self._dispatch_lock:
            return await self._dispatch_card_locked(card_context, workspace_info, extra_volumes)

    async def _dispatch_card_locked(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None = None,
        extra_volumes: list[VolumeMount] | None = None,
    ) -> dict[str, Any]:
        ephemeral_job: _EphemeralJob | None = None

        # Resolve the effective config: if extra_volumes are provided, create a
        # shallow copy with the updated volumes list (containerized performers only).
        effective_config = self._config
        if extra_volumes and self._config.mode != "subprocess":
            effective_config = self._config.model_copy(
                update={"volumes": list(self._config.volumes) + list(extra_volumes)}
            )

        # Ephemeral mode: spin up a fresh container before dispatching.
        if self._config.mode == "ephemeral":
            try:
                started = await performer_lifecycle.start_ephemeral(effective_config)
            except performer_lifecycle.LifecycleError as exc:
                logger.warning(
                    "http_performer.start_failed",
                    performer_id=self._config.id,
                    error=str(exc),
                )
                return {"status": "error", "reason": f"container start failed: {exc}"}

            # Use the injected client if present (tests); otherwise create one per container.
            ep_client = self._injected_client or PerformerHTTPClient(
                started.endpoint,
                auth_token=self._auth_token(),
            )
            ephemeral_job = _EphemeralJob(
                container_id=started.container_id,
                endpoint=started.endpoint,
                client=ep_client,
            )
            try:
                await performer_lifecycle.wait_ready(
                    started.endpoint,
                    self._auth_token(),
                    timeout=float(self._config.readiness_timeout_s),
                    performer_id=self._config.id,
                )
            except performer_lifecycle.ReadinessTimeoutError as exc:
                await self._cleanup_ephemeral_job(ephemeral_job)
                return {"status": "error", "reason": f"readiness timeout: {exc}"}

        # Build the job-init payload.
        try:
            payload = self._build_job_payload(card_context, workspace_info)
        except ValueError as exc:
            if ephemeral_job is not None:
                await self._cleanup_ephemeral_job(ephemeral_job)
            return {"status": "error", "reason": str(exc)}

        try:
            client = ephemeral_job.client if ephemeral_job is not None else self._ensure_client()
            response = await client.post_job(payload)
        except (PerformerAuthError, PerformerUnreachableError, TransportError) as exc:
            if ephemeral_job is not None:
                await self._cleanup_ephemeral_job(ephemeral_job)
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
            if ephemeral_job is not None:
                await self._cleanup_ephemeral_job(ephemeral_job)
            return {
                "status": "error",
                "reason": f"performer busy ({reason})" + (f": {detail}" if detail else ""),
            }

        job_id = response.job_id
        if ephemeral_job is not None:
            self._active_jobs[job_id] = ephemeral_job
            self._log_buffer = []
            self._log_poll_tasks[job_id] = asyncio.create_task(
                self._poll_container_logs(ephemeral_job.container_id, job_id),
                name=f"log_poll_{job_id[:8]}",
            )
        logger.info(
            "performer_endpoint.transition",
            performer_id=self._config.id,
            from_state="idle",
            to_state="busy",
            job_id=job_id,
        )
        return {
            "status": "ok",
            "session_id": job_id,
            "job_id": job_id,
            "accepted": True,
            "container_id": ephemeral_job.container_id if ephemeral_job is not None else None,
        }

    async def check_status(
        self,
        session_id: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            client = self._ensure_client(job_id=session_id)
        except TransportError:
            # No active job for this session_id — container was already cleaned up
            # or the session predates this coordinare instance. Re-raise so
            # monitor_performer can route through its transport-error retry path.
            raise
        try:
            status = await client.get_job(session_id)
        except (PerformerAuthError, PerformerUnreachableError, TransportError):
            # Auth failure, unreachability, or transport error — clean up the
            # stale _active_jobs entry so subsequent check_health() calls aren't
            # blocked by the dead container.
            if self._config.mode == "ephemeral":
                await self._cleanup_ephemeral_job_by_id(session_id)
            raise
        is_terminal = status.state in _TERMINAL_JOB_STATES
        if is_terminal:
            logger.info(
                "performer_endpoint.transition",
                performer_id=self._config.id,
                from_state="busy",
                to_state="idle",
                job_id=session_id,
                terminal_state=status.state,
            )
            if self._config.mode == "ephemeral":
                await self._cleanup_ephemeral_job_by_id(session_id)
            elif self._config.mode == "persistent":
                await self.call_reset()

        # Translate JobStatus → PerformerResponse-style dict that monitor_performer.py
        # expects (it reads `status.get("status", "working")`).
        if is_terminal and status.result is not None and status.result.summary:
            # _perform_job serialises the full PerformerResponse as JSON in summary.
            try:
                parsed = json.loads(status.result.summary)
                # Ensure parsed response includes JobState for job lifecycle tracking.
                if "state" not in parsed:
                    parsed["state"] = status.state
                return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            # Plain-text summary fallback: always "error" so monitor_performer can
            # terminate the session (it doesn't recognise plain-text responses).
            return {"status": "error", "reason": status.result.summary, "state": status.state}

        if not is_terminal:
            result: dict[str, Any] = {"status": "working", "job_id": session_id, "job_state": status.state}
            if status.events:
                result["events"] = status.events
            if status.metrics is not None:
                result["metrics"] = status.metrics
            return result

        # Terminal but no result (cancelled or internal failure).
        return {"status": "error", "reason": f"job ended in state '{status.state}' with no result", "job_id": session_id, "state": status.state}

    async def call_reset(self) -> bool:
        """POST /reset to the persistent performer endpoint.

        Returns True on 2xx, False (with a warning log) on any non-2xx or
        network error. Never raises — caller can always proceed with next
        dispatch regardless of reset outcome.
        """
        if self._config.mode != "persistent":
            return True
        try:
            client = self._ensure_client()
            resp = await client.post_reset()
            if resp.is_success:
                logger.info(
                    "http_performer.reset_ok",
                    performer_id=self._config.id,
                )
                return True
            logger.warning(
                "http_performer.reset_failed",
                performer_id=self._config.id,
                status_code=resp.status_code,
            )
            return False
        except Exception as exc:
            logger.warning(
                "http_performer.reset_error",
                performer_id=self._config.id,
                error=str(exc),
            )
            return False

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

    def get_agent_logs(self) -> list[str]:
        """Return buffered stdout+stderr lines from the active ephemeral container.

        Lines are refreshed every ~5 seconds by a background poller started when a
        job is dispatched. Returns an empty list if no container is active.
        """
        return list(self._log_buffer)

    async def _poll_container_logs(self, container_id: str, job_id: str) -> None:
        """Background task: poll `docker logs` every 5 seconds until the job ends."""
        while job_id in self._active_jobs:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "logs", "--tail", "200", container_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                lines = stdout.decode(errors="replace").splitlines()
                self._log_buffer = lines[-200:]
            except Exception as exc:  # pragma: no cover — best-effort
                logger.debug(
                    "http_performer.log_poll_failed",
                    performer_id=self._config.id,
                    container_id=container_id,
                    error=str(exc),
                )
            await asyncio.sleep(5.0)
        # One final capture after job completes
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "logs", "--tail", "200", container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            self._log_buffer = stdout.decode(errors="replace").splitlines()[-200:]
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _cleanup_ephemeral_job(self, job: _EphemeralJob) -> None:
        """Stop and release a specific ephemeral container + its client."""
        try:
            await performer_lifecycle.stop(job.container_id)
        except Exception as exc:  # pragma: no cover — best effort
            logger.warning(
                "http_performer.stop_failed",
                performer_id=self._config.id,
                container_id=job.container_id,
                error=str(exc),
            )
        if self._injected_client is None:
            with contextlib.suppress(Exception):
                await job.client.aclose()

    async def _cleanup_ephemeral_job_by_id(self, job_id: str) -> None:
        """Look up and clean up an ephemeral job by its job_id."""
        task = self._log_poll_tasks.pop(job_id, None)
        if task is not None:
            task.cancel()
        job = self._active_jobs.pop(job_id, None)
        if job is not None:
            await self._cleanup_ephemeral_job(job)

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

        import os

        secrets: dict[str, str] = {}
        if workspace_info is not None and workspace_info.github_token:
            secrets["GITHUB_TOKEN"] = workspace_info.github_token
        # Only inject the API key(s) required by the selected backend to avoid
        # leaking unrelated credentials into container environments.
        # opencode/junie/cursor read provider keys from env natively (no special
        # CLI flag needed), but still need secrets injected here so they reach
        # the subprocess when operators use the HTTP-payload secret path instead
        # of Docker env vars.
        backend = str(card_context.get("backend", "")).replace("-", "_")
        if backend == "codex":
            openai_key = os.environ.get("OPENAI_API_KEY", "")
            if openai_key:
                secrets["OPENAI_API_KEY"] = openai_key
        elif backend == "claude_code":
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if anthropic_key:
                secrets["ANTHROPIC_API_KEY"] = anthropic_key
        elif backend in {"opencode", "junie", "cursor"}:
            # These backends can use either Anthropic or OpenAI providers;
            # inject whichever keys are available so the subprocess can choose.
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                val = os.environ.get(key, "")
                if val:
                    secrets[key] = val

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
