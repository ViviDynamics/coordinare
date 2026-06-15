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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.nodes.handle_system_error import classify_upstream
from coordinare.services import performer_lifecycle
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import (
    PerformerAuthError,
    PerformerHTTPClient,
    PerformerUnreachableError,
)
from coordinare.upstream_errors import UpstreamHTTPError, strip_base_url_credentials

if TYPE_CHECKING:
    from coordinare.models.performer_endpoint import (
        JobInitPayload,
        PerformerEndpointConfig,
        VolumeMount,
    )
    from coordinare.workspace import WorkspaceInfo

logger = structlog.get_logger(__name__)


_TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled"}


def _inject_claude_code_secrets(
    secrets: dict[str, str],
    role_base_url: Any,
    role_api_key_env: Any,
    role_auth_token_env: Any,
) -> None:
    """Inject Anthropic/proxy secrets for the claude_code backend.

    Proxy-only auth: when auth_token_env is set, inject ANTHROPIC_AUTH_TOKEN
    (Bearer) and SKIP ANTHROPIC_API_KEY. claude CLI prefers x-api-key over
    Bearer, so leaking ANTHROPIC_API_KEY here causes the proxy to forward the
    real Anthropic key upstream — defeating per-role routing entirely.
    """
    import os

    if role_auth_token_env:
        token_val = os.environ.get(str(role_auth_token_env), "")
        if token_val:
            secrets["ANTHROPIC_AUTH_TOKEN"] = token_val
    else:
        key_env_name = str(role_api_key_env) if role_api_key_env else "ANTHROPIC_API_KEY"
        anthropic_key = os.environ.get(key_env_name, "")
        if anthropic_key:
            secrets["ANTHROPIC_API_KEY"] = anthropic_key
    if role_base_url:
        secrets["ANTHROPIC_BASE_URL"] = str(role_base_url)


@dataclass
class _EphemeralJob:
    """State for one in-flight ephemeral container job.

    076 (T017): the ``_active_jobs`` dict is keyed on ``session_id``
    (coordinare-allocated UUID, also written as the
    ``coordinare.session_id`` Docker label).  The job-runner's reply
    ``job_id`` is preserved here as a side-by-side field for diagnostics
    and for the per-job HTTP calls (``GET /jobs/<job_id>`` etc.).
    """

    container_id: str
    endpoint: str
    client: PerformerHTTPClient
    job_id: str | None = None


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
        # 088 US6 (FR-012): sessions whose secret refresh degraded (PATCH
        # failed twice in a row) — session_id → time of the second failure.
        self._secret_refresh_failed: dict[str, datetime] = {}
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

    def has_live_session(self, session_id: str) -> bool:
        """Return True if this service can still reach the container for ``session_id``.

        Ephemeral: a job is live only while the per-container client is tracked
        in ``_active_jobs`` — after a daemon restart the dict is empty and any
        snapshot-restored session_id refers to a container the new process
        cannot reach.

        Persistent: there is one shared endpoint; "live" simply means the
        endpoint URL is resolved.
        """
        if self._injected_client is not None:
            return True
        if self._config.mode == "ephemeral":
            return session_id in self._active_jobs
        return self._persistent_endpoint is not None

    def _ensure_client(self, job_id: str | None = None) -> PerformerHTTPClient:
        if self._injected_client is not None:
            return self._injected_client
        # Ephemeral: look up per-job client.
        if self._config.mode == "ephemeral" and job_id is not None:
            job = self._active_jobs.get(job_id)
            if job is not None:
                return job.client
            # 069 diagnostic: ephemeral lookup miss — about to fall through to
            # the persistent branch, which has no endpoint for ephemeral configs
            # and will raise TransportError. Log instance identity + known job_ids
            # so we can tell whether the polling caller resolved to a different
            # service instance than the one that ran dispatch_card.
            logger.warning(
                "http_performer.ephemeral_job_lookup_miss",
                performer_id=self._config.id,
                requested_job_id=job_id,
                known_job_ids=list(self._active_jobs.keys()),
                service_instance_id=id(self),
            )
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

        # 076 (T016) — pre-allocate the coordinare-side session_id BEFORE
        # spinning up the container so it can be set as the
        # ``coordinare.session_id`` Docker label.  The reconciliation pass
        # uses this label on subsequent daemon restarts to match a
        # running container to its persisted session.  Distinct from the
        # job-runner's ``response.job_id`` (which is used as the
        # ``/jobs/<id>/...`` URL component for HTTP calls).
        session_id = str(uuid.uuid4())

        # Resolve the effective config: if extra_volumes are provided, create a
        # shallow copy with the updated volumes list (containerized performers only).
        effective_config = self._config
        if extra_volumes and self._config.mode != "subprocess":
            effective_config = self._config.model_copy(
                update={"volumes": list(self._config.volumes) + list(extra_volumes)}
            )

        # Ephemeral mode: spin up a fresh container before dispatching.
        if self._config.mode == "ephemeral":
            # 076 (T015 + FR-009): build the 5 coordinare.* labels the
            # reconciliation pass requires on every performer container.
            from coordinare.daemon import get_daemon_started_at
            extra_labels: dict[str, str] = {
                "coordinare.session_id": session_id,
                "coordinare.daemon_started_at": get_daemon_started_at(),
                "coordinare.spec_version": "076",
            }
            # 076 regression fix: the env_bootstrap performer is symphony-scoped
            # and carries no card context, so ``id``/``role`` are absent.  Only
            # emit the card-scoped labels when populated — an empty-string label
            # value fails ``_validate_extra_label`` and would abort the launch,
            # deadlocking dispatch (coordinare believes a bootstrap is in-flight
            # while no container ever started).  Reconciliation adopts strictly
            # by ``coordinare.session_id``, so omitting these on card-less
            # containers is safe.
            card_id_label = str(card_context.get("id") or "")
            stage_label = str(card_context.get("role") or "")
            if card_id_label:
                extra_labels["coordinare.card_id"] = card_id_label
            if stage_label:
                extra_labels["coordinare.performer_stage"] = stage_label

            try:
                started = await performer_lifecycle.start_ephemeral(
                    effective_config, extra_labels=extra_labels
                )
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
            # 076 (T016/T017) — _active_jobs is keyed on coordinare-allocated
            # session_id (not the job-runner's job_id); job_id stays on
            # _EphemeralJob as a sub-field for HTTP-URL construction.
            ephemeral_job.job_id = job_id
            self._active_jobs[session_id] = ephemeral_job
            self._log_buffer = []
            self._log_poll_tasks[session_id] = asyncio.create_task(
                self._poll_container_logs(ephemeral_job.container_id, session_id),
                name=f"log_poll_{session_id[:8]}",
            )
        else:
            # Persistent mode: no per-container session.  Use the job-runner's
            # job_id as the session_id (status quo for persistent endpoints).
            session_id = job_id
        logger.info(
            "performer_endpoint.transition",
            performer_id=self._config.id,
            from_state="idle",
            to_state="busy",
            session_id=session_id,
            job_id=job_id,
        )
        return {
            "status": "ok",
            "session_id": session_id,
            "job_id": job_id,
            "accepted": True,
            "container_id": ephemeral_job.container_id if ephemeral_job is not None else None,
        }

    def secret_refresh_failed_at(self, session_id: str) -> datetime | None:
        """088 US6 (FR-012): when a refreshed-secret PATCH failed twice in a
        row for this session, return the time of the second failure so
        monitor_performer can attribute a later auth failure to stale
        credentials. ``None`` means the session's secrets are not degraded.
        """
        return self._secret_refresh_failed.get(session_id)

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
        # 076 (T016) — session_id is coordinare's identifier (the dict key for
        # _active_jobs); the HTTP URLs need the job-runner's job_id, which
        # we stored on _EphemeralJob.  For persistent mode the two are the
        # same value (no _EphemeralJob); preserve that fallback.
        ephemeral_job = self._active_jobs.get(session_id) if self._config.mode == "ephemeral" else None
        url_job_id = ephemeral_job.job_id if (ephemeral_job and ephemeral_job.job_id) else session_id
        # Forward refreshed secrets (e.g. github_token from monitor_performer)
        # to the running job before polling status. Best-effort: a PATCH
        # failure is logged but must not block the status poll, because the
        # running job may still succeed without the refreshed token if it
        # completes before the next expiry boundary.
        if payload:
            refreshed_token = payload.get("github_token")
            if refreshed_token:
                # 088 US6 (FR-012): retry once immediately on failure; a
                # second failure marks the session degraded so
                # monitor_performer can attribute later auth failures to
                # stale credentials instead of a generic system error.
                for attempt in (1, 2):
                    try:
                        await client.update_job_secrets(
                            url_job_id, {"github_token": refreshed_token}
                        )
                        self._secret_refresh_failed.pop(session_id, None)
                        break
                    except Exception as exc:
                        if attempt == 1:
                            logger.warning(
                                "http_performer.secret_refresh_failed",
                                performer_id=self._config.id,
                                session_id=session_id,
                                exc_type=type(exc).__name__,
                            )
                            continue
                        self._secret_refresh_failed[session_id] = datetime.now(UTC)
                        logger.error(
                            "http_performer.secret_refresh_degraded",
                            performer_id=self._config.id,
                            session_id=session_id,
                            exc_type=type(exc).__name__,
                        )
        try:
            status = await client.get_job(url_job_id)
        except (PerformerAuthError, PerformerUnreachableError, TransportError):
            # Auth failure, unreachability, or transport error — clean up the
            # stale _active_jobs entry so subsequent check_health() calls aren't
            # blocked by the dead container.
            if self._config.mode == "ephemeral":
                await self._cleanup_ephemeral_job_by_id(session_id)
            raise
        is_terminal = status.state in _TERMINAL_JOB_STATES
        if is_terminal:
            error_reason: str | None = None
            if status.state in ("failed", "error") and status.result is not None and status.result.summary:
                try:
                    parsed_summary = json.loads(status.result.summary)
                    if isinstance(parsed_summary, dict):
                        # The performer serialises its PerformerResponse with
                        # model_dump_json(exclude_none=True), so a None `reason`
                        # vanishes from the summary entirely. Fall back through the
                        # other diagnostic fields before degrading to the status
                        # token (and finally the raw summary) so a terminal failure
                        # never logs error_reason=None and discards every signal.
                        error_reason = (
                            parsed_summary.get("reason")
                            or parsed_summary.get("inference_skipped_reason")
                        )
                        if not error_reason:
                            status_token = parsed_summary.get("status")
                            error_reason = (
                                f"status={status_token}"
                                if status_token
                                else status.result.summary
                            )
                except (json.JSONDecodeError, TypeError):
                    error_reason = status.result.summary
                if isinstance(error_reason, str):
                    error_reason = error_reason[:500]
            logger.info(
                "performer_endpoint.transition",
                performer_id=self._config.id,
                from_state="busy",
                to_state="idle",
                job_id=session_id,
                terminal_state=status.state,
                error_reason=error_reason,
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
                self._log_upstream_http_error(parsed)
                return parsed
            except (json.JSONDecodeError, ValueError) as exc:
                # 088 US6 (FR-014): surface the parse failure before mapping
                # to a generic error so operators can see why the verdict
                # could not be read from the performer's summary.
                logger.warning(
                    "http_performer.job_result_malformed_json",
                    performer_id=self._config.id,
                    session_id=session_id,
                    parse_error=str(exc),
                    summary=status.result.summary[:200],
                )
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

    def _log_upstream_http_error(self, response: dict[str, Any]) -> None:
        """Emit verbatim upstream-body log line when the performer reports a
        non-2xx envelope (spec 067 FR-004). One structlog line per envelope at
        WARN (transient) or ERROR (permanent), kv-ordered per data-model.md §2.
        """
        metrics = response.get("metrics")
        envelope: dict[str, Any] | None = None
        if isinstance(metrics, dict):
            raw = metrics.get("upstream_http_error")
            if isinstance(raw, dict):
                envelope = raw
        if envelope is None:
            return
        try:
            error = UpstreamHTTPError(**envelope)
        except Exception as exc:  # malformed envelope — log once and move on
            logger.warning(
                "http_performer.upstream_envelope_malformed",
                performer_id=self._config.id,
                error=str(exc),
            )
            return
        verdict = classify_upstream(error)
        log_fn = logger.error if verdict == "permanent" else logger.warning
        log_fn(
            "http_performer.upstream_http_error",
            status=error.status,
            route=error.route,
            base_url=strip_base_url_credentials(error.base_url),
            upstream_body=error.body,
            body_truncated=error.body_truncated,
            elapsed_ms=error.elapsed_ms,
            upstream_request_id=error.upstream_request_id,
            performer_id=self._config.id,
            classification=verdict,
        )

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
        host_log_dir = None
        for vol in self._config.volumes:
            if str(vol.container_path) == "/var/log/performer":
                host_log_dir = vol.host_path
                break
        try:
            await performer_lifecycle.stop(
                job.container_id,
                host_log_dir=host_log_dir,
                performer_id=self._config.id,
            )
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

        # 060/Option A: env_bootstrap dispatches do not flow through
        # WorkspaceManager (no card, no workspace prep). Synthesize the
        # workspace fields from BootstrapJobPayload so the performer's
        # standard clone+dispatch flow can pick up the symphony repo and
        # run install steps from env_spec_contents into cache_mount_path.
        if card_context.get("job_type") == "env_bootstrap":
            return self._build_env_bootstrap_payload(card_context)

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
        # opencode/junie read provider keys from env natively (no special
        # CLI flag needed), but still need secrets injected here so they reach
        # the subprocess when operators use the HTTP-payload secret path instead
        # of Docker env vars.
        backend = str(card_context.get("backend", "")).replace("-", "_")
        # Per-role overrides plumbed from PerformerRoleConfig via dispatch_performer.
        # base_url + api_key_env let an operator point a backend at a proxy
        # (e.g. LiteLLM) instead of the vendor's native endpoint.
        role_base_url = card_context.get("base_url")
        role_api_key_env = card_context.get("api_key_env")
        role_auth_token_env = card_context.get("auth_token_env")
        if backend == "codex":
            # 080: a self-hosted mode resolves to auth_token_env (proxy bearer);
            # prefer it so the catalog's endpoint key reaches codex, falling back
            # to api_key_env then the default. Native modes set api_key_env only.
            key_env_name = str(role_auth_token_env or role_api_key_env or "OPENAI_API_KEY")
            openai_key = os.environ.get(key_env_name, "")
            if openai_key:
                secrets["OPENAI_API_KEY"] = openai_key
            if role_base_url:
                secrets["OPENAI_BASE_URL"] = str(role_base_url)
        elif backend == "claude_code":
            _inject_claude_code_secrets(
                secrets, role_base_url, role_api_key_env, role_auth_token_env
            )
        elif backend in {"opencode", "junie"}:
            # These backends can use either Anthropic or OpenAI providers;
            # inject whichever keys are available so the subprocess can choose.
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                val = os.environ.get(key, "")
                if val:
                    secrets[key] = val
            # 080: a self-hosted mode's endpoint key (auth_token_env) — inject its
            # value under both provider keys so the subprocess's provider config
            # can authenticate against the proxy regardless of which it selects.
            if role_auth_token_env:
                token = os.environ.get(str(role_auth_token_env), "")
                if token:
                    secrets["ANTHROPIC_API_KEY"] = token
                    secrets["OPENAI_API_KEY"] = token

        # Stash card_context as metadata so the performer has full access to
        # role/persona/relay_feedback/etc. without us forking the schema here.
        metadata = json.loads(json.dumps(dict(card_context), default=str))

        env_cache_path = card_context.get("env_cache_path")
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
                "env_cache_path": env_cache_path if env_cache_path else None,
            }
        )

    def _build_env_bootstrap_payload(
        self, card_context: dict[str, Any]
    ) -> JobInitPayload:
        """Synthesize a JobInitPayload for an env_bootstrap dispatch.

        BootstrapJobPayload is coordinare-internal and does not carry the
        repo_url/branch/secrets that JobInitPayload requires, so we derive
        them here: the repo is the symphony's own GitHub repo (the agent
        clones it to read env_spec_files), branch defaults to 'main', the
        GitHub token comes from the coordinare's environment.
        """
        import os

        from coordinare.models.performer_endpoint import JobInitPayload

        symphony_org = str(card_context.get("symphony_org") or "")
        symphony_repo = str(card_context.get("symphony_repo") or "")
        if not symphony_org or not symphony_repo:
            raise ValueError(
                "env_bootstrap card_context missing symphony_org or symphony_repo"
            )

        repo_url = f"https://github.com/{symphony_org}/{symphony_repo}.git"
        # Synthetic branch — bootstrap never pushes/PRs. clone_repository fetches
        # ``origin {branch}:{branch}`` and, if the ref doesn't exist remotely,
        # falls back to ``checkout -b`` from the shallow-cloned default branch.
        # Using a real branch (e.g. "main") triggers git's "refusing to fetch
        # into current branch" error, which surfaces as WorkspaceSetupError.
        branch = f"env-bootstrap-{uuid.uuid4().hex[:8]}"

        cache_mount_path = str(card_context.get("cache_mount_path") or "")
        env_spec_files = list(card_context.get("env_spec_files") or [])
        env_spec_contents: dict[str, str] = dict(
            card_context.get("env_spec_contents") or {}
        )
        # 077: coordinare-derived authoritative manifest artifacts.
        dependency_checklist = str(card_context.get("dependency_checklist") or "").strip()
        verify_provided = bool(card_context.get("verify_provided"))
        activate_provided = bool(card_context.get("activate_provided"))

        secrets: dict[str, str] = {}
        # Prefer the daemon-injected token (fresh App installation token or
        # configured PAT, sourced via WorkspaceManager.get_fresh_github_token);
        # fall back to the coordinare process env only when not provided.
        gh_token = str(card_context.get("_github_token") or "") or os.environ.get(
            "GITHUB_TOKEN", ""
        )
        if gh_token:
            secrets["GITHUB_TOKEN"] = gh_token
        # Honor per-role auth_token_env (proxy-only mode) the same way the regular
        # claude_code path does: when set, inject ANTHROPIC_AUTH_TOKEN and SKIP
        # ANTHROPIC_API_KEY so the proxy receives only the Bearer token.
        bootstrap_backend = str(card_context.get("backend") or "").replace("-", "_")
        role_base_url = card_context.get("base_url")
        role_api_key_env = card_context.get("api_key_env")
        role_auth_token_env = card_context.get("auth_token_env")
        if bootstrap_backend == "claude_code":
            _inject_claude_code_secrets(
                secrets, role_base_url, role_api_key_env, role_auth_token_env
            )
            openai_key = os.environ.get("OPENAI_API_KEY", "")
            if openai_key:
                secrets["OPENAI_API_KEY"] = openai_key
        else:
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                val = os.environ.get(key, "")
                if val:
                    secrets[key] = val

        spec_block = "\n\n".join(
            f"=== {path} ===\n{content}"
            for path, content in env_spec_contents.items()
        )
        # 077: feedback-injection — if the previous bootstrap failed verification,
        # put the exact failure FIRST so the agent fixes those specific checks
        # instead of repeating the same miss (retries otherwise run blind).
        last_failure = str(card_context.get("last_failure") or "").strip()
        retry_block = ""
        if last_failure:
            retry_block = (
                "⚠ RETRY — your PREVIOUS attempt FAILED verification. These are the "
                "EXACT checks verify.sh re-runs; fix them specifically this time "
                "(do not just repeat the same steps):\n"
                f"{last_failure}\n\n"
                "Frequent root cause: a spec-PINNED tool version (e.g. the Ruby "
                "version in .ruby-version) was not actually installed (a different "
                "version was used), so bundler and every gem cascaded to failure. "
                "Install the EXACT pinned versions the spec/.tool-version files "
                "require, then re-run your own verify.sh until it passes.\n\n"
            )
        # 077: the coordinare-derived checklist goes right after any retry failure
        # so the agent has the authoritative, itemised list of what to install
        # (with exact pinned versions) before the free-form spec files.
        checklist_block = f"{dependency_checklist}\n\n" if dependency_checklist else ""
        # 077: when coordinare has written an authoritative verify.sh from the
        # manifest, the agent must RUN it (not author it) — coordinare owns the
        # verification contract. Otherwise fall back to having the agent write one.
        if verify_provided:
            verify_block = (
                "VERIFICATION (coordinare-owned): an authoritative verify.sh has "
                f"ALREADY been written at {cache_mount_path}/verify.sh — it is the "
                "contract that decides whether this bootstrap succeeded. Do NOT "
                "create, overwrite, or delete it. After installing, RUN it yourself "
                f"(`bash {cache_mount_path}/verify.sh`); if it exits non-zero, FIX "
                "the missing/broken dependency it reports (especially a pinned "
                "language-runtime version) and re-run until it passes. Do NOT end "
                "your turn until verify.sh passes — confirm the installation before "
                "exiting.\n\n"
            )
        else:
            verify_block = (
                "MANDATORY: also write an executable verification script at "
                f"{cache_mount_path}/verify.sh that, AFTER sourcing "
                f"{cache_mount_path}/activate.sh, asserts every dependency the spec "
                "files require is actually present and runnable — e.g. "
                "`command -v chromium >/dev/null && chromium --version`, "
                "`command -v bundle && bundle -v`, a language/runtime version check, "
                "etc. It MUST `exit 1` (loudly, to stderr) on the FIRST missing or "
                "broken dependency, and `exit 0` only when ALL are verified. This "
                "script is the contract: the bootstrap is considered successful ONLY "
                "if verify.sh passes — a silent install failure here fails the whole "
                "bootstrap and triggers a retry, so make the checks real (actually "
                "invoke the tools, do not just test for file existence).\n\n"
                "FINALLY, before you finish: run verify.sh yourself "
                f"(`sh {cache_mount_path}/verify.sh`). If it exits non-zero, FIX the "
                "missing/broken dependency and re-run it. Do NOT end your turn until "
                "verify.sh passes — confirm the installation before exiting.\n\n"
            )
        # 087: coordinare owns activate.sh too (like verify.sh) — an auto-discovering
        # activation rendered from the manifest. The agent installs the pinned
        # toolchain into the cache but must NOT hand-write the activation paths: a
        # forgetful model fumbled the .rbenv-vs-rbenv / .nvm-vs-nvm dot-prefix every
        # run, leaving a fully-built cache that verify.sh couldn't see. Fallback (no
        # manifest → activate_provided False): the agent writes activate.sh itself.
        if activate_provided:
            activate_block = (
                "ACTIVATION (coordinare-owned): an authoritative activate.sh has "
                f"ALREADY been written at {cache_mount_path}/activate.sh. It "
                "AUTO-DISCOVERS the toolchain you install — it probes BOTH dotted and "
                "non-dotted version-manager roots under the cache (.rbenv/rbenv "
                "versions/<ver>, .nvm/nvm node versions, asdf, pyenv) and any "
                "extracted-deb */usr/bin directories, and puts them on PATH. So just "
                "install the pinned runtimes with a standard version manager / build "
                "tool (rbenv+ruby-build, nvm, asdf, pyenv) INTO "
                f"{cache_mount_path} (use it as the install prefix, e.g. RBENV_ROOT="
                f"{cache_mount_path}/.rbenv) and extract any deb binaries INTO "
                f"{cache_mount_path} — coordinare's activate.sh will find them. Do NOT "
                f"create, overwrite, or delete {cache_mount_path}/activate.sh — "
                "coordinare owns the activation contract and editing it would clobber "
                "the auto-discovery.\n\n"
            )
        else:
            activate_block = (
                "MANDATORY: After installing, write a sourceable shell script at "
                f"{cache_mount_path}/activate.sh that consumer agents will source "
                "before running their tools. It MUST export PATH (prepending any "
                "bin directories you created, e.g. virtualenv bin, node_modules/.bin, "
                "language toolchain bin), and any other env vars needed to use the "
                "installed tooling (VIRTUAL_ENV, NODE_PATH, etc.). Without this "
                "file the cache is unusable and downstream performers will reinstall. "
                "Make it idempotent and safe to source repeatedly.\n\n"
                "CRITICAL — activate.sh is SOURCED into EVERY shell in the container "
                "(via BASH_ENV / /etc/profile.d), including the backend-CLI installer "
                "and every tool call. It MUST therefore NEVER call `exit`, `return` "
                "with a non-zero status, or `set -e`/`set -u` that aborts — doing so "
                "TERMINATES the calling shell and breaks the whole container (a sourced "
                "`exit 1` has knocked out CLI installs and deadlocked bootstraps). On "
                "ANY failure inside activate.sh, print a warning to stderr and CONTINUE; "
                "never abort. Hard assertions go ONLY in verify.sh, which is RUN "
                "standalone (never sourced), so its `exit 1` is safe.\n\n"
            )
        persona = (
            retry_block
            + checklist_block
            + "You are an environment bootstrap agent. Your job is to install "
            f"all build/test/runtime dependencies for this project into "
            f"the directory {cache_mount_path!r}, which is a writable volume "
            "shared with later performer containers as a read-only devenv root.\n\n"
            "Read the install/setup instructions from the following project "
            "spec files and execute the commands they describe. Use "
            f"{cache_mount_path} (NOT the repo root and NOT $HOME) as the "
            "install prefix for language toolchains, virtualenvs, node_modules, "
            "and any other generated artefacts that should persist across "
            "performer runs. Do NOT modify the cloned repo working tree, do "
            "NOT push commits, and do NOT open a PR — this job exists only to "
            "populate the cache directory.\n\n"
            "This cache directory MAY already be partially populated by a "
            "previous bootstrap. Do NOT skip installation just because a "
            "toolchain, activate.sh, or some packages are already present — "
            "RE-RUN the install/setup commands from the spec files every time. "
            "Standard package managers (apt-get, bundle, npm/yarn, pip, etc.) "
            "are idempotent and skip anything already installed at the correct "
            "version, so re-running is cheap and installs ONLY missing or "
            "newly-added dependencies (for example, a system package — like a "
            "headless browser — that a spec file now documents but a prior "
            "bootstrap predated). The spec files are the source of truth for "
            "what MUST be present in the cache.\n\n"
            "SYSTEM PACKAGES (apt) — CRITICAL: this image ships NO apt package "
            "lists and consumer containers may have NO network egress, so a bare "
            "`apt-get install <pkg>` (especially one placed in activate.sh that "
            "runs at consumer activation) FAILS SILENTLY ('E: Unable to locate "
            "package') and the binary never lands. Do NOT install system packages "
            "that way. Instead make them self-contained in the cache, the SAME way "
            "PostgreSQL/Redis are handled: during THIS bootstrap run `apt-get "
            "update`, then download the package AND its full dependency closure as "
            f".deb files into {cache_mount_path}/debs/ (e.g. `cd {cache_mount_path}/debs "
            "&& apt-get download $(apt-cache depends --recurse --no-recommends "
            "--no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances "
            "-i <pkg> | grep '^\\w' | sort -u)`). SHARED LIBRARIES are handled FOR "
            "you: the coordinare-owned profile automatically extracts the *.so files "
            f"from {cache_mount_path}/debs/*.deb and prepends them to LD_LIBRARY_PATH "
            "before activate.sh is sourced, so you do NOT need to `dpkg -i` or "
            "`apt-get install` the debs in activate.sh just to make native libraries "
            "loadable — that now happens deterministically. For a package whose "
            "BINARY must be on PATH (for example a headless browser such as chromium "
            "and its chromedriver), extract the deb into the cache without touching "
            f"the system (`dpkg-deb -x {cache_mount_path}/debs/<pkg>.deb "
            f"{cache_mount_path}/<prefix>`). NOTE: `dpkg-deb -x` recreates the "
            "deb's absolute layout under <prefix>, so the binaries land in "
            f"{cache_mount_path}/<prefix>/usr/bin (and sometimes "
            f"{cache_mount_path}/<prefix>/usr/lib/<pkg>/), NOT {cache_mount_path}/"
            "<prefix>/bin — confirm the real path with `find "
            f"{cache_mount_path}/<prefix> -name <binary> -type f`. Putting that "
            "extracted bin dir on PATH is handled per the ACTIVATION section below "
            "(coordinare's activate.sh auto-discovers */usr/bin dirs under the cache); "
            "do NOT bake PATH edits into the install commands. Never "
            "`dpkg -i` (it needs root and mutates the "
            "container) and never install from the network. The hard pass/fail "
            "assertion that the binary is on PATH belongs in verify.sh (below).\n\n"
            "PINNED LANGUAGE RUNTIMES — CRITICAL: when a spec file pins an EXACT "
            "language/runtime version (e.g. .ruby-version, .tool-versions, .nvmrc, "
            ".python-version, or a Gemfile / package.json `engines` field), the "
            "distro package manager almost NEVER provides that exact version — "
            "`apt-get install ruby` yields the distro's Ruby (e.g. 3.3.x), NOT a "
            "pinned 3.4.2, and every gem/bundler step then cascades to failure. You "
            "MUST install the EXACT pinned version with a version manager or build "
            "tool (rbenv + ruby-build, asdf, pyenv, nvm, or a ruby-build/source "
            f"compile) INTO {cache_mount_path} (use it as the install prefix, e.g. "
            f"RBENV_ROOT={cache_mount_path}/.rbenv, or asdf/nvm/pyenv under "
            f"{cache_mount_path}). Putting that runtime's bin on PATH is handled per "
            "the ACTIVATION section below (coordinare's activate.sh auto-discovers the "
            "pinned runtime under the cache). Do NOT accept the system/distro default — verify.sh "
            "asserts the exact version (e.g. `ruby -v` matches .ruby-version) and a "
            "mismatch FAILS the whole bootstrap. Install the pinned runtime FIRST, "
            "before bundler/gems/node modules, since those build against it.\n\n"
            + activate_block
            + verify_block
            + f"Spec files ({', '.join(env_spec_files) or 'none'}):\n\n"
            f"{spec_block}\n"
        )

        meta_src = {k: v for k, v in card_context.items() if k != "_github_token"}
        # Score (performer-side dispatch payload model) requires a `title`
        # field; BootstrapJobPayload doesn't carry one, so synthesize it from
        # the symphony name. Also seed `description` from the spec block so the
        # backend agent has user-facing context if it inspects the Score.
        symphony_name = str(card_context.get("symphony_name") or "")
        meta_src.setdefault(
            "title", f"env_bootstrap: {symphony_name}" if symphony_name else "env_bootstrap"
        )
        meta_src.setdefault(
            "description",
            f"Install dev environment into {cache_mount_path} per project spec files.",
        )
        metadata = json.loads(json.dumps(meta_src, default=str))

        return JobInitPayload.model_validate(
            {
                "job_id": str(uuid.uuid4()),
                "card_id": f"env_bootstrap:{card_context.get('symphony_name', '')}",
                "role": "env_bootstrap",
                "backend": str(card_context.get("backend", "") or "claude_code"),
                "persona": persona,
                "repo_url": repo_url,
                "branch": branch,
                "secrets": secrets,
                "metadata": metadata,
                # The bootstrap *writes into* the cache mount, so the
                # container-side env_cache_path is the same directory the
                # performer will populate. Without this, service inference
                # short-circuits with skipped_reason=no_env_cache_path.
                "env_cache_path": cache_mount_path or None,
            }
        )


__all__ = ["HTTPPerformerService"]
