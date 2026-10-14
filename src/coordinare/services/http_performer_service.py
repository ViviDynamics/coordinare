"""HTTP-mode adapter implementing AgentServiceProtocol (spec 056, T027).

Wraps :class:`~coordinare.transport.http_transport.PerformerHTTPClient` and the
container lifecycle service so the existing dispatch/monitor flow can route
ephemeral and persistent containerized performers through the same call sites
as the subprocess transport.

Mode handling:

* ``persistent`` — ``endpoint`` is fixed at registration; ``dispatch_card``
  sends ``POST /jobs`` directly.
* ``ephemeral``  — ``dispatch_card`` first calls
  the injected :class:`PerformerRuntime` (Docker by default, Kubernetes when
  configured) + the shared ``performer_lifecycle.wait_ready`` to acquire a
  one-shot container, then dispatches. The container_id is recorded so
  :meth:`check_status` can call the runtime's ``stop`` when the
  job reaches a terminal state, regardless of success/failure (FR-003).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog

from coordinare.graph.nodes.handle_system_error import classify_upstream
from coordinare.services import performer_lifecycle
from coordinare.services.docker_executor import DockerExecutor
from coordinare.services.docker_runtime import DockerRuntime
from coordinare.services.run_header import build_run_header
from coordinare.services.run_log import RunLogBusyError, redact_log_text
from coordinare.services.run_transcript import (
    UNATTRIBUTED,
    RootGuard,
    transcript_root_conflicts,
)
from coordinare.services.system_health import SYSTEM_HEALTH
from coordinare.transport.base import TransportError, TransportTimeoutError
from coordinare.transport.http_transport import (
    PerformerAuthError,
    PerformerHTTPClient,
    PerformerUnreachableError,
)
from coordinare.upstream_errors import UpstreamHTTPError, strip_base_url_credentials

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable

    from coordinare.models.performer_endpoint import (
        JobAcceptResponse,
        JobInitPayload,
        JobStatus,
        PerformerEndpointConfig,
        VolumeMount,
    )
    from coordinare.services.performer_runtime import PerformerRuntime
    from coordinare.services.run_log import RunLogCollector, RunLogWriter
    from coordinare.services.run_transcript import RunTranscriptWriter, TranscriptCollector
    from coordinare.workspace import WorkspaceInfo

logger = structlog.get_logger(__name__)


_TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled"}

# 129: records requested per transcript delivery. Ordinary status polls take
# one batch; the terminal drain loops until the performer's last seq is written.
_TRANSCRIPT_BATCH_LIMIT = 200
_TRANSCRIPT_DRAIN_LIMIT = 500
_TRANSCRIPT_DRAIN_MAX_ROUNDS = 5000
# Wall-clock bounds. The terminal drain runs inline, before the terminal state is
# reported and before the container is removed or reset, so no new dispatch can
# replace the finished job first. The teardown drain runs before a cancelled, reaped
# or stopped container is removed. Both are short: a tail beyond the bound is
# recorded as a truncation marker, not waited for (review round 4, items 3 and 4).
_TRANSCRIPT_DRAIN_SECONDS = 5.0
_TRANSCRIPT_TEARDOWN_SECONDS = 5.0
# The share of a drain's bound kept for the truncation marker, so that the drain and
# its marker together never exceed the bound (review 5, item 4).
_TRANSCRIPT_MARKER_SECONDS = 1.0
# The most aclose waits for teardown steps still running (review 6, F2).
_CLOSE_TEARDOWN_SECONDS = 10.0
# How many finished runs are remembered, so that a late poll or teardown never
# recreates a writer for them (129 review, item 7).
_TRANSCRIPT_FINALISED_MEMORY = 4096
# The most a status poll waits for one transcript operation (review item 4). A slow
# disk delays the transcript, never completion detection.
_TRANSCRIPT_POLL_SECONDS = 2.0
# 130: the most a dispatch waits for its run header to reach disk. The header is one
# small append. A slow disk delays the header (the first poll still writes it first),
# never the dispatch.
RUN_HEADER_WAIT_SECONDS = 2.0
# 132: the most the teardown waits for a run's full log before the container is
# stopped anyway. With the teardown drain (_TRANSCRIPT_TEARDOWN_SECONDS, 5 s) that is at
# most 20 s before a stop. A large log that is still arriving is kept only as far as it got
# (the partial is dropped), so the bound delays the stop, never prevents it.
_RUN_LOG_TEARDOWN_SECONDS = 15.0


def _known_missing_job(exc: httpx.HTTPStatusError) -> bool:
    """The performer's job-not-found response proves absence; generic 404 does not."""
    if exc.response.status_code != httpx.codes.NOT_FOUND:
        return False
    try:
        body = exc.response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("detail") == "job not found"


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


def _runtime_accepts_backend(runtime: Any) -> bool:
    """True when *runtime*'s ``start_ephemeral`` accepts the ``backend`` kwarg.

    ``PerformerRuntime`` is ``@runtime_checkable``, which verifies method
    presence only — an injected implementation predating the ``backend`` kwarg
    (issue #486) still satisfies the protocol and would fail at the call site
    with TypeError if the kwarg were passed unconditionally.
    """
    try:
        params = inspect.signature(runtime.start_ephemeral).parameters
    except (TypeError, ValueError):
        return False
    if "backend" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _inject_orchestration_secrets(secrets: dict[str, str], orchestration: Any) -> None:
    """Forward resolved upstream credentials under the names the proxy reads."""
    if not isinstance(orchestration, dict):
        return
    for leg in ("tool", "thinking", "classifier"):
        ref = orchestration.get(leg)
        if not isinstance(ref, dict):
            continue
        auth_env = ref.get("auth_env")
        if isinstance(auth_env, str) and auth_env:
            token = os.environ.get(auth_env, "")
            if token:
                secrets[auth_env] = token


def _str_dict(value: Any) -> dict[str, str]:
    """Coerce a possibly-None mapping into a plain ``dict[str, str]``.

    092: ``test_env_vars`` arrives via ``card_context`` (a ``model_dump`` of
    ``BootstrapJobPayload`` or a synthesized dispatch dict). Tolerate ``None`` /
    absent and stringify keys+values so the result is safe to feed into the
    ``secrets`` channel regardless of source.
    """
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _open_log_stream(
    iter_logs: Any,
    handle: str,
    io: Any,
) -> AsyncGenerator[bytes, None]:
    """132: the runtime's log stream; runtimes that take the run-log pool are given it."""
    if "io" in inspect.signature(iter_logs).parameters:
        return cast("AsyncGenerator[bytes, None]", iter_logs(handle, io=io))
    return cast("AsyncGenerator[bytes, None]", iter_logs(handle))


def _record_commit(collector: RunLogCollector, run_id: str, future: asyncio.Future[Any] | Any) -> None:
    """132: a background finish that committed its file marks the run committed."""
    if not future.cancelled() and future.exception() is None and future.result() is True:
        collector.mark_committed(run_id)


async def _stop_docker_session_confirmed(runtime: Any, session_id: str) -> bool:
    """A best-effort stop authorizes cleanup only after Docker proves absence."""
    if not isinstance(runtime, DockerRuntime):
        return False
    executor = DockerExecutor()
    labels = {"coordinare.session_id": session_id}
    containers = await executor.list_containers_by_label(labels, timeout=5.0)
    if len(containers) > 1:
        return False
    if containers:
        await runtime.stop(containers[0].container_id)
    return not await executor.list_containers_by_label(labels, timeout=5.0)


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
    # 132: the coordinare session id, which is also the run id of the transcript and
    # of the full log. None for a job that never got that far.
    run_id: str | None = None
    # 132: set once the full log has been collected, so one job collects once.
    log_collected: bool = False
    # 132: the job's secret values, known from the payload. Held only for this job's life:
    # they are dropped in cleanup, so a refused dispatch keeps no copy.
    secrets: tuple[str, ...] = ()


def _inject_driver_secrets(
    secrets: dict[str, str],
    role_auth_token_env: Any,
    role_api_key_env: Any,
) -> None:
    """Inject provider secrets for the driver backend (521/C2).

    argv is world-readable, so driver takes provider config by flags and API
    keys by env only (ANTHROPIC_API_KEY / OPENAI_API_KEY) — inject whichever
    keys the daemon has, exactly like the opencode/junie dual-provider family;
    nothing else. 080 catalog resolution: a self-hosted mode resolves to
    auth_token_env (proxy bearer) and a native mode to api_key_env; either
    rides OPENAI_API_KEY — driver speaks Chat Completions with provider openai
    (§3.5).
    """
    import os

    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(key, "")
        if val:
            secrets[key] = val
    endpoint_key_env = role_auth_token_env or role_api_key_env
    if endpoint_key_env:
        token = os.environ.get(str(endpoint_key_env), "")
        if token:
            secrets["OPENAI_API_KEY"] = token


class HTTPPerformerService:
    """AgentService-compatible adapter for ephemeral / persistent performers."""

    # 107: a containerized performer clones the repository for itself (the
    # dispatch payload carries only repo_url and branch), so the daemon has no
    # reason to clone on its behalf.
    clones_own_workspace = True

    def __init__(
        self,
        config: PerformerEndpointConfig,
        *,
        client: PerformerHTTPClient | None = None,
        runtime: PerformerRuntime | None = None,
        transcripts: TranscriptCollector | None = None,
        run_logs: RunLogCollector | None = None,
    ) -> None:
        self._config = config
        self._injected_client = client
        # 129: where the per-run transcript records go. None (the default) keeps
        # the wire and the disk exactly as before.
        self._transcripts = transcripts
        # 132: where the complete log of each run is written at teardown. None keeps
        # the teardown exactly as before.
        self._run_logs = run_logs
        self._transcript_targets: dict[str, tuple[str, str, str]] = {}
        self._transcript_unsupported_runs: set[str] = set()
        self._transcript_secrets: dict[str, list[str]] = {}
        self._transcript_failed_runs: set[str] = set()
        # Runs whose extra mounts overlap the transcripts root: never transcribed.
        self._transcript_refused: set[str] = set()
        # Runs already finished. Their writers are never recreated (item 7).
        self._transcript_finalised: dict[str, None] = {}
        # Runs whose transcript was drained on the terminal path. Their teardown does not
        # drain again, so each run is drained exactly once (review round 4, item 5).
        self._transcript_drained: set[str] = set()
        # Shielded teardown steps still running; aclose waits for them (review 6, F2).
        self._teardown_tasks: set[asyncio.Task[Any]] = set()
        # Spec 146: which runtime starts performers. Defaults to Docker so every
        # existing deployment and test behaves exactly as before; the Kubernetes
        # runtime is opted into via agent_transport config.
        self._runtime: PerformerRuntime = runtime or DockerRuntime()
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
        # 132: background log finishes for collections cut off at the time bound. Held so
        # they are not garbage collected; they run on worker threads and are never awaited
        # by a teardown.
        self._log_finishers: set[asyncio.Future[Any]] = set()

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
                raise TransportError(f"performer {self._config.id} has no endpoint resolved yet")
            self._persistent_client = PerformerHTTPClient(
                self._persistent_endpoint,
                auth_token=self._auth_token(),
            )
        return self._persistent_client

    async def _run_teardown[T](self, awaitable: Awaitable[T]) -> T:
        """Run a teardown step so that a cancellation of the caller cannot skip it.

        The step runs as its own task, recorded so that ``aclose`` can wait for it. If
        the caller is cancelled while waiting, the cancellation is raised only after the
        step has finished, so the container is stopped exactly once and the cancellation
        still propagates (review 5, blocker 2; review 6, F2).
        """
        task = asyncio.ensure_future(awaitable)
        self._teardown_tasks.add(task)
        task.add_done_callback(self._teardown_tasks.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.wait({task})
            if not task.cancelled():
                # Retrieved here, so a failure of the step is not reported as unhandled.
                task.exception()
            raise

    async def aclose(self) -> None:
        # Teardown steps still running are awaited, bounded, so a shutdown does not
        # abandon a container stop half way (review 6, F2).
        running = set(self._teardown_tasks)
        if running:
            await asyncio.wait(running, timeout=_CLOSE_TEARDOWN_SECONDS)
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
            # Shutdown stops every container promptly: no log collection here.
            await self._run_teardown(self._cleanup_ephemeral_job(job, collect=False))

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
            logger.warning(
                "http_performer.auth_failed", performer_id=self._config.id, error=str(exc),
            )
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
        return {
            "status": "unknown",
            "availability": availability,
            "capabilities": caps,
        }  # pragma: no cover — PerformerStatus.availability is Literal["starting","idle","busy","draining"], all handled above

    async def dispatch_card(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None = None,
        extra_volumes: list[VolumeMount] | None = None,
        run_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._dispatch_lock:
            return await self._dispatch_card_locked(
                card_context, workspace_info, extra_volumes, run_inputs,
            )

    async def _dispatch_card_locked(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None = None,
        extra_volumes: list[VolumeMount] | None = None,
        run_inputs: dict[str, Any] | None = None,
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
                update={"volumes": list(self._config.volumes) + list(extra_volumes)},
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
                # Issue #486: the backend resolved for this dispatch rides the
                # start call, so the runtime can put BACKEND in the container
                # env — the image's entrypoint installs the backend CLI from it
                # at container start, before any job payload can arrive.
                # @runtime_checkable only verifies method presence, so probe
                # the signature: an injected runtime predating the backend
                # kwarg would otherwise fail here with TypeError.
                backend = str(card_context.get("backend") or "") or None
                start_kwargs: dict[str, Any] = {"extra_labels": extra_labels}
                if _runtime_accepts_backend(self._runtime):
                    start_kwargs["backend"] = backend
                started = await self._runtime.start_ephemeral(
                    effective_config, **start_kwargs,
                )
                SYSTEM_HEALTH.record_performer_start(type(self._runtime).__name__, ok=True)
            except performer_lifecycle.LifecycleError as exc:
                # 151: the class name only; the message can carry runtime detail.
                SYSTEM_HEALTH.record_performer_start(
                    type(self._runtime).__name__, ok=False, error=type(exc).__name__,
                )
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
                # StartedPerformer.handle is the opaque runtime handle: a
                # container id under Docker, a Pod name under Kubernetes. The
                # internal field keeps its name for now so Phase 1 changes
                # nothing observable — line ~422 surfaces "container_id" in a
                # status payload. Renaming it belongs with the Kubernetes
                # runtime, where it stops being a container id in fact.
                container_id=started.handle,
                endpoint=started.endpoint,
                client=ep_client,
                run_id=session_id,
            )
            try:
                await performer_lifecycle.wait_ready(
                    started.endpoint,
                    self._auth_token(),
                    timeout=float(self._config.readiness_timeout_s),
                    performer_id=self._config.id,
                )
            except performer_lifecycle.ReadinessTimeoutError as exc:
                await self._refuse_job(ephemeral_job)
                return {"status": "error", "reason": f"readiness timeout: {exc}"}

        # Build the job-init payload.
        try:
            payload = self._build_job_payload(card_context, workspace_info)
        except ValueError as exc:
            if ephemeral_job is not None:
                await self._refuse_job(ephemeral_job)
            return {"status": "error", "reason": str(exc)}
        # 129: the coordinare session id is the run identity. Using it as the job
        # id means the performer's transcript run id and the daemon's transcript
        # file name agree without a second id. Persistent mode already set
        # session_id = job_id, so this is the same value there.
        payload = self._transcript_identity(session_id, payload, extra_volumes)
        if ephemeral_job is not None:
            # 132: a refused run's container still gets its full log collected at teardown,
            # so the job carries its secrets. They are not kept in the per-run table until
            # the job is accepted.
            ephemeral_job.secrets = tuple(secret.get_secret_value() for secret in payload.secrets.values())

        try:
            client = ephemeral_job.client if ephemeral_job is not None else self._ensure_client()
            response = await client.post_job(payload)
        except (PerformerAuthError, PerformerUnreachableError, TransportError) as exc:
            # The run was never accepted, so its refusal marker is dropped (review 6, F8).
            self._transcript_refused.discard(session_id)
            if ephemeral_job is not None:
                await self._refuse_job(ephemeral_job)
            logger.warning(
                "http_performer.dispatch_failed",
                performer_id=self._config.id,
                error=str(exc),
            )
            return {"status": "error", "reason": str(exc)}
        except Exception:
            self._transcript_refused.discard(session_id)
            if ephemeral_job is not None:
                await self._refuse_job(ephemeral_job)
            raise

        # 409 busy → surface as error so the dispatch loop retries next cycle.
        if not getattr(response, "accepted", False):
            reason = getattr(response, "reason", "busy")
            detail = getattr(response, "detail", None)
            self._transcript_refused.discard(session_id)
            if ephemeral_job is not None:
                await self._refuse_job(ephemeral_job)
            return {
                "status": "error",
                "reason": f"performer busy ({reason})" + (f": {detail}" if detail else ""),
            }

        # post_job's union collapses to JobAcceptResponse once the accepted=False
        # guard above has returned the only other shape.
        job_id = cast("JobAcceptResponse", response).job_id
        # Decided under the pre-dispatch id, the key the refusal was recorded under.
        transcribed = self._transcribes(session_id)
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
        # 129 (review 5, blocker 3; review 6, blocker 2): the job's secrets are kept only
        # once it is accepted, and under the run id the polls, the writer and the release
        # use. In persistent mode that is the job id, never the pre-dispatch id.
        # 132: kept for every accepted run, not only transcribed ones: the live tail and
        # the full log are redacted by the same exact values. Persistent runs are keyed
        # by the job id only here, because their session id changes on accept.
        self._remember_transcript_secrets(session_id, payload)
        if transcribed:
            endpoint = ephemeral_job.endpoint if ephemeral_job is not None else self._config.endpoint
            await self._write_run_header(
                session_id, card_context, run_inputs, str(endpoint) if endpoint else None,
            )

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
            "performer_id": self._config.id,
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
        ephemeral_job = (
            self._active_jobs.get(session_id) if self._config.mode == "ephemeral" else None
        )
        url_job_id = (
            ephemeral_job.job_id if (ephemeral_job and ephemeral_job.job_id) else session_id
        )
        # Forward refreshed secrets (e.g. github_token from monitor_performer)
        # to the running job before polling status. Best-effort: a PATCH
        # failure is logged but must not block the status poll, because the
        # running job may still succeed without the refreshed token if it
        # completes before the next expiry boundary.
        if payload:
            refreshed_token = payload.get("github_token")
            if refreshed_token:
                # 132: a refreshed token is a secret too: the live tail and the full log
                # must redact it as well.
                self._remember_refreshed_secret(session_id, refreshed_token)
                # 088 US6 (FR-012): retry once immediately on failure; a
                # second failure marks the session degraded so
                # monitor_performer can attribute later auth failures to
                # stale credentials instead of a generic system error.
                for attempt in (1, 2):
                    try:
                        await client.update_job_secrets(
                            url_job_id, {"github_token": refreshed_token},
                        )
                        self._secret_refresh_failed.pop(session_id, None)
                        break
                    except Exception as exc:
                        # A 404 means the job already reached a terminal state
                        # (succeeded/failed/cancelled) before this refresh poll
                        # landed — the performer's refresh_secrets rejects a
                        # non-active job. That is an expected race on short jobs,
                        # NOT a delivery failure: the token was never needed. Do
                        # not retry, and do not mark the session degraded (which
                        # 088 US6 uses to attribute *later* auth failures to stale
                        # creds — a false signal here).
                        if (
                            isinstance(exc, httpx.HTTPStatusError)
                            and exc.response.status_code == 404
                        ):
                            self._secret_refresh_failed.pop(session_id, None)
                            logger.debug(
                                "http_performer.secret_refresh_skipped_job_terminal",
                                performer_id=self._config.id,
                                session_id=session_id,
                            )
                            break
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
        writer = self._transcript_writer(session_id, payload)
        try:
            status = await self._get_job_status(client, url_job_id, session_id, writer)
        except TransportTimeoutError:
            # A bare status-poll timeout is NOT evidence the container is dead:
            # during a CPU-heavy build (e.g. a long Ruby native-gem compile) the
            # job runner can momentarily exceed the poll deadline while still
            # making progress. Reaping the container here would abort an
            # in-flight bootstrap. Re-raise WITHOUT cleanup so the caller's
            # retry path (daemon bootstrap poll loop / monitor_performer
            # transport-retry budget) can poll again. Genuinely-dead containers
            # surface as PerformerUnreachableError below, not as timeouts.
            raise
        except (PerformerAuthError, PerformerUnreachableError, TransportError):
            # Auth failure, unreachability, or other transport error — clean up
            # the stale _active_jobs entry so subsequent check_health() calls
            # aren't blocked by the dead container.
            # The ephemeral teardown drains, then stops, in a finally (review 5, blocker
            # 2). A persistent performer keeps its run: its writer stays live.
            if self._config.mode == "ephemeral":
                await self._cleanup_ephemeral_job_by_id(session_id)
            else:
                await self._teardown_drain(session_id)
            raise
        is_terminal = status.state in _TERMINAL_JOB_STATES
        if is_terminal:
            error_reason: str | None = None
            if (
                status.state in ("failed", "error")
                and status.result is not None
                and status.result.summary
            ):
                try:
                    parsed_summary = json.loads(status.result.summary)
                    if isinstance(parsed_summary, dict):
                        # The performer serialises its PerformerResponse with
                        # model_dump_json(exclude_none=True), so a None `reason`
                        # vanishes from the summary entirely. Fall back through the
                        # other diagnostic fields before degrading to the status
                        # token (and finally the raw summary) so a terminal failure
                        # never logs error_reason=None and discards every signal.
                        error_reason = parsed_summary.get("reason") or parsed_summary.get(
                            "inference_skipped_reason",
                        )
                        if not error_reason:
                            status_token = parsed_summary.get("status")
                            error_reason = (
                                f"status={status_token}" if status_token else status.result.summary
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
            # The remaining records are taken before the terminal state is reported,
            # because the performer keeps them only until the job is replaced or reset.
            # The teardown and the release sit in the finally, so a cancellation during
            # the drain still tears the run down (review 5, blocker 2).
            try:
                if writer is not None:
                    # A malformed batch on the terminal poll is not a basis for the
                    # drain: pull afresh instead.
                    basis: JobStatus | None = status
                    if status.transcript_last_seq is None and status.transcript_error:
                        basis = None
                    await self._drain_transcript(client, url_job_id, writer, basis)
            finally:
                if writer is not None:
                    self._transcript_drained.add(session_id)
                await self._finish_terminal(session_id)

        # Translate JobStatus → PerformerResponse-style dict that monitor_performer.py
        # expects (it reads `status.get("status", "working")`).
        if is_terminal and status.result is not None and status.result.summary:
            # _perform_job serialises the full PerformerResponse as JSON in summary.
            try:
                parsed: dict[str, Any] = json.loads(status.result.summary)
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
            result: dict[str, Any] = {
                "status": "working",
                "job_id": session_id,
                "job_state": status.state,
            }
            if status.events:
                result["events"] = status.events
            if status.metrics is not None:
                result["metrics"] = status.metrics
            return result

        # Terminal but no result (cancelled or internal failure).
        return {
            "status": "error",
            "reason": f"job ended in state '{status.state}' with no result",
            "job_id": session_id,
            "state": status.state,
        }

    def _transcript_identity(
        self,
        session_id: str,
        payload: JobInitPayload,
        extra_volumes: list[VolumeMount] | None,
    ) -> JobInitPayload:
        """129: the job payload for one dispatch.

        With transcripts disabled the payload is returned untouched, exactly as it was
        before spec 129 (item 8). Enabled, the coordinare session id becomes the job id,
        so the performer's run id and the transcript file name agree, and the byte cap
        is sent.

        A run whose extra mounts (per-symphony env caches) overlap the transcripts root
        is not transcribed. The performer gets no cap, and no writer is created for it.
        """
        if self._transcripts is None or not self._transcripts.enabled:
            return payload
        mounts = [Path(volume.host_path) for volume in extra_volumes or []]
        conflicts = (
            transcript_root_conflicts(self._transcripts.root, RootGuard(performer_mounts=tuple(mounts)))
            if mounts
            else []
        )
        if conflicts:
            self._transcript_refused.add(session_id)
            logger.warning(
                "run_transcript.run_mount_conflict",
                performer_id=self._config.id,
                session_id=session_id,
                conflicts=[str(path) for path in conflicts],
            )
            return payload
        return payload.model_copy(
            update={
                "job_id": session_id,
                "transcript_max_bytes": self._transcripts.max_bytes_per_run,
            },
        )

    def _transcribes(self, session_id: str) -> bool:
        """True when this run's records are captured: transcripts are on and the run is not refused."""
        if self._transcripts is None or not self._transcripts.enabled:
            return False
        return session_id not in self._transcript_refused

    def _remember_transcript_secrets(self, session_id: str, payload: JobInitPayload) -> None:
        """129: keep the job's secret values so the run's records are redacted by exact value."""
        self._transcript_secrets[session_id] = [
            secret.get_secret_value() for secret in payload.secrets.values()
        ]

    async def _write_run_header(
        self,
        run_id: str,
        card_context: dict[str, Any],
        run_inputs: dict[str, Any] | None,
        endpoint: str | None,
    ) -> None:
        """130: stage the run's header and write it, best effort. Never raises.

        The header goes to the run file the first poll also writes, so it is the first
        line of that file. A write that outlasts ``RUN_HEADER_WAIT_SECONDS`` is left to
        finish in its thread: the writer keeps the header first either way, and dispatch
        returns on time.
        """
        try:
            target = run_inputs.get("transcript_target") if run_inputs else None
            if not isinstance(target, dict):
                # Without the folder the run's file is named after, a writer created here
                # would pin the file to the wrong place before the first poll knows it.
                logger.info("run_transcript.header_skipped", run_id=run_id, reason="no transcript target")
                return
            self._transcript_targets[run_id] = (
                str(target.get("symphony") or ""),
                str(target.get("card_id") or ""),
                str(target.get("stage") or ""),
            )
            writer = self._transcript_writer(run_id, None)
            if writer is None:
                return
            writer.stage_run_header(
                build_run_header(
                    run_id=run_id,
                    card_context=card_context,
                    run_inputs=run_inputs,
                    performer={"id": self._config.id, "transport": self._config.mode, "endpoint": endpoint},
                ),
            )
            await asyncio.wait_for(
                asyncio.to_thread(writer.flush_run_header),
                timeout=RUN_HEADER_WAIT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "run_transcript.header_deferred",
                run_id=run_id,
                wait_seconds=RUN_HEADER_WAIT_SECONDS,
            )
        except Exception as exc:  # best effort: a header must never fail a dispatch
            logger.warning(
                "run_transcript.header_failed",
                run_id=run_id,
                exc_type=type(exc).__name__,
            )
    def _remember_refreshed_secret(self, session_id: str, value: str) -> None:
        """132: a refreshed secret joins the run's exact values, once."""
        values = self._transcript_secrets.get(session_id)
        if values is not None and value not in values:
            values.append(value)

    def _redact_tail(self, lines: list[str], session_id: str) -> list[str]:
        """132: the live tail is the same text as the full log, so it is redacted the same way."""
        secrets = [
            *self._transcript_secrets.get(session_id, []),
            *(self._run_logs.ambient_secrets if self._run_logs is not None else ()),
        ]
        return [redact_log_text(line, secrets) for line in lines]

    def _transcript_failed(
        self,
        session_id: str,
        stage: str,
        exc: BaseException,
        writer: RunTranscriptWriter | None = None,
    ) -> None:
        """129 HIGH 3: transcript code failed. Mark the run, log once, carry on.

        Never raises. The caller's status poll or teardown continues regardless.
        """
        if writer is not None:
            writer.mark_failed(exc)
        if session_id in self._transcript_failed_runs:
            return
        self._transcript_failed_runs.add(session_id)
        logger.warning(
            "run_transcript.capture_failed",
            performer_id=self._config.id,
            session_id=session_id,
            stage=stage,
            exc_type=type(exc).__name__,
            error=str(exc),
        )

    def _transcript_writer(
        self,
        session_id: str,
        payload: dict[str, Any] | None,
    ) -> RunTranscriptWriter | None:
        """129: the run's writer, or None when transcripts are off, refused, or finished.

        The monitor passes ``transcript_target`` (symphony and card id) on every
        poll. A target seen once is remembered for the run, so polls that do not
        carry one (observer probes, bootstrap polls) still write to the same file.
        A finished run never gets a writer again (item 7).
        """
        if self._transcripts is None or not self._transcripts.enabled:
            return None
        if session_id in self._transcript_finalised or session_id in self._transcript_refused:
            return None
        try:
            target = payload.get("transcript_target") if payload else None
            if isinstance(target, dict):
                self._transcript_targets[session_id] = (
                    str(target.get("symphony") or ""),
                    str(target.get("card_id") or ""),
                    str(target.get("stage") or ""),
                )
            symphony, card_id, stage = self._transcript_targets.get(session_id, (UNATTRIBUTED, "", ""))
            writer = self._transcripts.writer_for(
                run_id=session_id, symphony=symphony, card_id=card_id,
            )
            # 134: the stage goes on usage records, so the ledger survives a restart with it.
            writer.set_stage(stage)
            writer.add_secret_values(self._transcript_secrets.get(session_id, []))
            refreshed = payload.get("github_token") if payload else None
            if isinstance(refreshed, str) and refreshed:
                writer.add_secret_values([refreshed])
        except Exception as exc:  # best effort: a broken run must not break polling
            self._transcript_failed(session_id, "writer", exc)
            return None
        return writer

    def _release_transcript_run(self, session_id: str) -> None:
        """129 item 6: drop every per-run daemon record once the run has finished.

        Secrets, targets, failure and support markers are kept only while the run is
        live. The run id is remembered, bounded, so a late poll or teardown cannot
        recreate its writer (item 7). Safe to call repeatedly.
        """
        self._transcript_targets.pop(session_id, None)
        self._transcript_secrets.pop(session_id, None)
        self._transcript_failed_runs.discard(session_id)
        self._transcript_unsupported_runs.discard(session_id)
        self._transcript_refused.discard(session_id)
        self._transcript_drained.discard(session_id)
        if self._transcripts is not None:
            self._transcripts.release(session_id)
        self._transcript_finalised[session_id] = None
        while len(self._transcript_finalised) > _TRANSCRIPT_FINALISED_MEMORY:
            del self._transcript_finalised[next(iter(self._transcript_finalised))]

    async def _get_job_status(
        self,
        client: PerformerHTTPClient,
        job_id: str,
        session_id: str,
        writer: RunTranscriptWriter | None,
    ) -> JobStatus:
        """Poll the job. With a transcript writer, the same request carries the cursor."""
        if writer is None:
            return await client.get_job(job_id)
        if not writer.loaded and not await self._load_writer(writer, session_id):
            # The cursor is not known yet. A plain poll keeps completion moving; the
            # transcript catches up on a later poll.
            return await client.get_job(job_id)
        status = await client.get_job(
            job_id, transcript_after=writer.cursor, transcript_limit=_TRANSCRIPT_BATCH_LIMIT,
        )
        await self._absorb_transcript(writer, status, session_id)
        return status

    def _count_usage(self, writer: RunTranscriptWriter) -> None:
        """134: hand the usage records this batch accepted to the ledger. Totals rise here."""
        if self._transcripts is None:
            return
        taken = writer.drain_usage()
        if not taken:
            return
        symphony, card_id = writer.parts
        self._transcripts.usage.observe(
            writer.run_id, symphony=symphony, card=card_id, stage=writer.stage, records=taken,
        )

    async def _absorb_transcript(
        self,
        writer: RunTranscriptWriter,
        status: JobStatus,
        session_id: str,
    ) -> None:
        """Write one batch on a worker thread (129 item 2). The file and the redaction
        never run on the event loop."""
        try:
            if status.transcript_last_seq is None:
                if status.transcript_error:
                    # A malformed transcript field (review 5, blocker 1): errored for this
                    # poll only; the next poll is read afresh.
                    logger.warning(
                        "run_transcript.performer_batch_error",
                        session_id=session_id,
                        exc_type=status.transcript_error,
                    )
                    return
                # Transcripts are on but the performer returned no transcript fields:
                # an image that predates spec 129. Say so once per run, write nothing.
                if session_id not in self._transcript_unsupported_runs:
                    self._transcript_unsupported_runs.add(session_id)
                    logger.warning(
                        "run_transcript.performer_unsupported",
                        performer_id=self._config.id,
                        session_id=session_id,
                    )
                return
            writer.observe_last_seq(status.transcript_last_seq)
            if writer.cursor > status.transcript_last_seq:
                logger.warning(
                    "run_transcript.cursor_ahead_of_performer",
                    session_id=session_id,
                    cursor=writer.cursor,
                    performer_last_seq=status.transcript_last_seq,
                )
            if status.transcript_error:
                logger.warning(
                    "run_transcript.performer_batch_error",
                    session_id=session_id,
                    exc_type=status.transcript_error,
                )
            if status.transcript:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        writer.append_batch,
                        status.transcript,
                        last_seq=status.transcript_last_seq,
                    ),
                    timeout=_TRANSCRIPT_POLL_SECONDS,
                )
        except TimeoutError:
            # Slow, not broken: the batch is retried on the next poll.
            logger.warning(
                "run_transcript.poll_timeout",
                session_id=session_id,
                timeout_s=_TRANSCRIPT_POLL_SECONDS,
                written_through_seq=writer.cursor,
            )
        except Exception as exc:  # best effort: see _transcript_failed
            self._transcript_failed(session_id, "absorb", exc, writer)
        finally:
            # 134 review: count what the writer accepted on every path, including a
            # timed-out append, whose worker thread may have written before the timeout.
            self._count_usage(writer)

    async def _load_writer(self, writer: RunTranscriptWriter, session_id: str) -> bool:
        """Read the writer's cursor on a worker thread, bounded. False if it is not ready."""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(writer.ensure_loaded),
                timeout=_TRANSCRIPT_POLL_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "run_transcript.load_timeout",
                session_id=session_id,
                timeout_s=_TRANSCRIPT_POLL_SECONDS,
            )
            return False
        return writer.loaded

    async def _mark_tail_lost(
        self,
        writer: RunTranscriptWriter,
        reason: str,
        *,
        timeout: float = _TRANSCRIPT_MARKER_SECONDS,
    ) -> None:
        """Record that records after the cursor were not retrieved (review round 4, item 4).

        Bounded by ``timeout`` and contained: a failure here fails the run's transcript,
        never the poll.
        """
        try:
            await asyncio.wait_for(
                asyncio.to_thread(writer.record_tail_lost, reason),
                timeout=timeout,
            )
        except Exception as exc:  # best effort: see _transcript_failed
            self._transcript_failed(writer.run_id, "tail_marker", exc, writer)

    async def _fetch_drain_batch(
        self,
        client: PerformerHTTPClient,
        job_id: str,
        writer: RunTranscriptWriter,
    ) -> JobStatus | None:
        """One pull of the tail, retried once (review 5, item 5).

        A transport failure or a malformed batch is retried once within the drain's
        bound. Returns None when both attempts fail. A 404 means the job is gone and is
        raised for the caller.
        """
        failure = "unknown"
        for attempt in (1, 2):
            try:
                status = await client.get_job(
                    job_id,
                    transcript_after=writer.cursor,
                    transcript_limit=_TRANSCRIPT_DRAIN_LIMIT,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == httpx.codes.NOT_FOUND:
                    raise
                failure = f"http {exc.response.status_code}"
            except Exception as exc:  # any failure of one pull is retried (review 6, F6)
                failure = type(exc).__name__
            else:
                if status.transcript_last_seq is not None or not status.transcript_error:
                    return status
                failure = status.transcript_error
            logger.warning(
                "run_transcript.drain_fetch_failed",
                session_id=writer.run_id,
                attempt=attempt,
                failure=failure,
            )
        return None

    async def _drain_rounds(
        self,
        client: PerformerHTTPClient,
        job_id: str,
        writer: RunTranscriptWriter,
        status: JobStatus | None,
    ) -> None:
        """Fetch until the performer's last seq is on disk. ``status`` is already absorbed, or None."""
        session_id = writer.run_id
        if not writer.loaded and not await self._load_writer(writer, session_id):
            return
        for _ in range(_TRANSCRIPT_DRAIN_MAX_ROUNDS):
            if writer.failed:
                return
            if status is not None and (
                status.transcript_last_seq is None or writer.cursor >= status.transcript_last_seq
            ):
                return
            cursor_before = writer.cursor
            fetched = await self._fetch_drain_batch(client, job_id, writer)
            if fetched is None:
                # Both attempts failed: the tail after the cursor is lost. The marker says
                # so, and only when records were actually missed (record_tail_lost).
                logger.warning(
                    "run_transcript.drain_tail_lost",
                    session_id=session_id,
                    written_through_seq=writer.cursor,
                )
                await self._mark_tail_lost(writer, "drain fetch failed")
                return
            status = fetched
            await self._absorb_transcript(writer, status, session_id)
            if status.transcript_last_seq is None or writer.cursor >= status.transcript_last_seq:
                return
            if writer.cursor == cursor_before:
                # Nothing was accepted (empty or entirely rejected batch). Asking
                # again would return the same records, so stop and say so.
                logger.warning(
                    "run_transcript.drain_stalled",
                    session_id=session_id,
                    written_through_seq=writer.cursor,
                    last_seq=status.transcript_last_seq,
                )
                await self._mark_tail_lost(writer, "drain stalled")
                return
        logger.warning(
            "run_transcript.drain_round_limit",
            session_id=session_id,
            written_through_seq=writer.cursor,
        )

    async def _drain_transcript(
        self,
        client: PerformerHTTPClient,
        job_id: str,
        writer: RunTranscriptWriter,
        status: JobStatus | None,
        *,
        timeout: float = _TRANSCRIPT_DRAIN_SECONDS,
    ) -> None:
        """Fetch the remaining records within ``timeout`` in total, marker included.

        The truncation marker is written inside the same bound: the drain gets the
        bound less a share kept for the marker (review 5, item 4). Never raises except
        for cancellation, which still records the tail as lost before it propagates.
        """
        session_id = writer.run_id
        if status is not None:
            writer.observe_last_seq(status.transcript_last_seq)
        marker = min(_TRANSCRIPT_MARKER_SECONDS, timeout / 2)
        try:
            async with asyncio.timeout(timeout - marker):
                await self._drain_rounds(client, job_id, writer, status)
        except TimeoutError:
            logger.warning(
                "run_transcript.drain_timeout",
                session_id=session_id,
                timeout_s=timeout,
                written_through_seq=writer.cursor,
            )
            await self._mark_tail_lost(writer, "drain timed out", timeout=marker)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.NOT_FOUND:
                # The performer no longer holds the job: whatever is not yet on disk is lost.
                logger.debug("run_transcript.drain_job_gone", session_id=session_id)
                await self._mark_tail_lost(writer, "job gone", timeout=marker)
                return
            self._transcript_failed(session_id, "drain", exc, writer)
        except asyncio.CancelledError:
            # Cancelled mid-drain (the owner is shutting down or stopping the run). The
            # tail is recorded as lost, then the cancellation propagates.
            await self._mark_tail_lost(writer, "drain cancelled", timeout=marker)
            raise
        except Exception as exc:  # best effort: a drain failure must not block teardown
            self._transcript_failed(session_id, "drain", exc, writer)

    async def _finish_terminal(self, session_id: str) -> None:
        """Tear down a finished run's container (or reset a persistent performer), then
        drop its per-run daemon state. The release is in the finally, so it happens
        even when the teardown is cancelled (review 5, blocker 2)."""
        try:
            if self._config.mode == "ephemeral":
                await self._cleanup_ephemeral_job_by_id(session_id)
            elif self._config.mode == "persistent":
                await self.call_reset()
        finally:
            self._release_transcript_run(session_id)

    async def _teardown_drain(self, session_id: str) -> None:
        """129 MEDIUM 6: best-effort, time-bounded pull before a teardown that is not terminal.

        A cancelled, reaped or stopped run would otherwise lose everything since
        its last poll. The bound means the teardown is delayed by at most
        ``_TRANSCRIPT_TEARDOWN_SECONDS`` and never prevented. It does not release the
        run: the caller releases it once the container's absence is known (review 5,
        item 6).
        """
        if self._transcripts is None or not self._transcripts.enabled:
            return
        if session_id in self._transcript_drained:
            return
        try:
            if self._config.mode == "ephemeral":
                job = self._active_jobs.get(session_id)
                if job is None or not job.job_id:
                    return
                client, job_id = job.client, job.job_id
            else:
                client, job_id = self._ensure_client(), session_id
            writer = self._transcript_writer(session_id, None)
            if writer is None:
                return
            await self._drain_transcript(
                client, job_id, writer, None,
                timeout=_TRANSCRIPT_TEARDOWN_SECONDS,
            )
        except Exception as exc:  # best effort: never block the teardown
            self._transcript_failed(session_id, "teardown_drain", exc)

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
        """Background task: poll the runtime's logs every 5 seconds until the job ends.

        Goes through ``PerformerRuntime.tail_logs`` rather than ``docker logs``
        directly. The handle is a container id under Docker and a Pod name under
        Kubernetes, so the direct call failed on every poll under Kubernetes while
        the buffer simply stayed empty — a silent hole in the dashboard's live log
        view rather than anything that announced itself.
        """
        while job_id in self._active_jobs:
            try:
                lines = await self._runtime.tail_logs(container_id, lines=200)
                if lines:
                    # 132: redacted before it is stored, so the snapshot never holds the raw text.
                    self._log_buffer = self._redact_tail(lines, job_id)
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
            lines = await self._runtime.tail_logs(container_id, lines=200)
            if lines:
                self._log_buffer = self._redact_tail(lines, job_id)
        except Exception:  # pragma: no cover
            pass

    async def stop_session_confirmed(self, session_id: str) -> bool:
        """Stop an owned writer; retain clients and identity until absence is proved.

        The run's transcript is finalised only when the stop is confirmed (review 5,
        item 6). The confirmation runs in a finally, shielded, so a cancelled drain still
        stops the container once.
        """
        confirmed = False
        try:
            await self._teardown_drain(session_id)
        finally:
            confirmed = await self._run_teardown(self._confirm_stopped(session_id))
            if confirmed:
                self._release_transcript_run(session_id)
        return confirmed

    async def _confirm_stopped(self, session_id: str) -> bool:
        """The stop itself. True only once the container's absence is proved."""
        if self._config.mode != "ephemeral":
            client = self._ensure_client()
            try:
                await client.cancel_job(session_id)
                status = await client.get_job(session_id)
            except httpx.HTTPStatusError as exc:
                if _known_missing_job(exc):
                    return True
                raise
            return status.state in _TERMINAL_JOB_STATES
        job = self._active_jobs.get(session_id)
        find = getattr(self._runtime, "find_session", None)
        handle = job.container_id if job is not None else None
        if callable(find):
            if handle is None:
                started = await find(session_id)
                if started is None:
                    return True
                handle = started.handle
            try:
                # 132: the full log is read before the stop removes it.
                if job is not None:
                    await self._capture_run_log(job)
            finally:
                await self._runtime.stop(handle)
            if await find(session_id) is not None:
                return False
        elif not await self._stop_by_label_collecting(job, session_id):
            return False
        task = self._log_poll_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
        self._active_jobs.pop(session_id, None)
        if job is not None and self._injected_client is None:
            await job.client.aclose()
        return True

    async def _stop_by_label_collecting(self, job: _EphemeralJob | None, session_id: str) -> bool:
        """132: a stop confirmed by container label (Docker without ``find_session``) reads the
        job's full log first, then stops, exactly as the handle-based path does."""
        try:
            if job is not None:
                await self._capture_run_log(job)
        finally:
            confirmed = await _stop_docker_session_confirmed(self._runtime, session_id)
        return confirmed

    async def release_session(self, session_id: str) -> None:
        """Issue #489: release-time teardown for a coordinare session.

        The daemon calls this when it drops a session (card completed or
        evicted), whether or not the job ever reported terminal — the 076
        ``check_status`` cleanup only fires on a terminal poll, so a job
        wedged mid-run would otherwise leave its container alive with no
        session behind it. Keyed on the coordinare-allocated ``session_id``
        (the ``_active_jobs`` key / ``coordinare.session_id`` label).
        Best-effort and idempotent, mirroring the terminal-poll cleanup.
        """
        if not session_id:
            return
        try:
            await self._cleanup_ephemeral_job_by_id(session_id)
        finally:
            self._release_transcript_run(session_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _cleanup_ephemeral_job(self, job: _EphemeralJob, *, collect: bool = True) -> None:
        """Stop and release a specific ephemeral container + its client.

        The stop is in a finally: nothing that happens while the log is read (a
        cancellation, a failure, a slow disk) keeps the container alive. ``collect=False``
        skips the log, which shutdown uses so that every stop happens promptly.
        """
        try:
            await self._capture_run_log(job, collect=collect)
        finally:
            await self._stop_job_container(job)
            if self._injected_client is None:
                with contextlib.suppress(Exception):
                    await job.client.aclose()
            job.secrets = ()
            if job.run_id is not None:
                self._transcript_secrets.pop(job.run_id, None)

    async def _refuse_job(self, job: _EphemeralJob) -> None:
        """Tear down a job whose dispatch was refused. Shielded, like every other teardown:
        a caller cancelled during the stop does not skip it (review round 3)."""
        await self._run_teardown(self._cleanup_ephemeral_job(job))

    async def _stop_job_container(self, job: _EphemeralJob) -> None:
        try:
            await self._runtime.stop(job.container_id)
        except Exception as exc:
            logger.warning(
                "http_performer.stop_failed",
                performer_id=self._config.id,
                container_id=job.container_id,
                error=str(exc),
            )

    async def _capture_run_log(self, job: _EphemeralJob, *, collect: bool = True) -> None:
        """132: write the run's complete log before its container or Pod is removed.

        Once per job, and bounded by ``_RUN_LOG_TEARDOWN_SECONDS`` (with the drain, at most
        20 s before a stop). Run-log I/O runs on the collector's own pool. A runtime that
        hangs or fails leaves no file. A collection cut short by the bound keeps what it
        wrote, marked as cut off, and is finished on the pool without the stop waiting for
        it. A saturated pool skips the collection, logged once. A cancellation aborts the
        partial without waiting for any thread, then propagates.
        """
        collector = self._run_logs
        if (
            not collect
            or collector is None
            or not collector.enabled
            or job.run_id is None
            or job.log_collected
        ):
            return
        iter_logs = getattr(self._runtime, "iter_logs", None)
        if not callable(iter_logs):
            return
        job.log_collected = True
        run_id = job.run_id
        writer = collector.writer_for(
            run_id=run_id,
            secrets=(*job.secrets, *self._transcript_secrets.get(run_id, [])),
        )
        completed = False
        try:
            chunks = _open_log_stream(iter_logs, job.container_id, collector.io)
            await asyncio.wait_for(
                self._copy_run_log(chunks, writer, collector),
                timeout=_RUN_LOG_TEARDOWN_SECONDS,
            )
            completed = True
        except TimeoutError:
            logger.warning(
                "run_log.collect_timeout",
                run_id=run_id,
                performer_id=self._config.id,
                timeout_s=_RUN_LOG_TEARDOWN_SECONDS,
            )
            writer.request_cut_off()
            self._schedule_log_finish(writer, collector)
        except RunLogBusyError:
            logger.info("run_log.collect_skipped", run_id=run_id, reason="run-log workers busy")
        except Exception as exc:  # best effort: the stop follows regardless
            logger.warning(
                "run_log.collect_failed",
                run_id=run_id,
                performer_id=self._config.id,
                exc_type=type(exc).__name__,
                error=str(exc),
            )
        finally:
            if not completed and not writer.cut_off:
                writer.abort()

    def _schedule_log_finish(self, writer: RunLogWriter, collector: RunLogCollector) -> None:
        """Finish a cut-off log on the run-log pool, without anyone awaiting it."""
        try:
            future = collector.io.submit(writer.finish)
        except RunLogBusyError:
            writer.abort()
            return
        future.add_done_callback(functools.partial(_record_commit, collector, writer.run_id))
        # Held so the finish is not lost; no teardown awaits it.
        tracked = asyncio.wrap_future(future)
        self._log_finishers.add(tracked)
        tracked.add_done_callback(self._log_finishers.discard)

    async def _copy_run_log(
        self,
        chunks: AsyncGenerator[bytes, None],
        writer: RunLogWriter,
        collector: RunLogCollector,
    ) -> bool:
        """Stream the runtime's output into the writer, one chunk on the run-log pool at a time."""
        try:
            async for chunk in chunks:
                await asyncio.wrap_future(collector.io.submit(writer.feed, chunk))
            committed = await asyncio.wrap_future(collector.io.submit(writer.finish))
            if committed:
                collector.mark_committed(writer.run_id)
            return committed
        finally:
            await chunks.aclose()

    async def _cleanup_ephemeral_job_by_id(self, job_id: str) -> None:
        """Look up and clean up an ephemeral job by its job_id."""
        # Every path that tears a container down ends here (terminal, system error,
        # stop, release). The transcript is drained first, once. The stop sits in a
        # finally and is shielded, so a cancelled drain still removes the container
        # exactly once (review 5, blocker 2).
        try:
            await self._teardown_drain(job_id)
        finally:
            try:
                await self._run_teardown(self._stop_ephemeral_job(job_id))
            finally:
                self._release_transcript_run(job_id)

    async def _stop_ephemeral_job(self, job_id: str) -> None:
        """Forget the job and stop its container. Runs once per call, shielded by the caller."""
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
        # 092: symphony test-environment vars are injected FIRST so a test file can
        # never clobber a coordinare-owned operational credential (GITHUB_TOKEN, API
        # keys) appended below — operational secrets always win on a name collision.
        # These reach code-running performers (e.g. the implementer) and the env-cache
        # QA runtime where services-start.sh runs. Secret-like: redacted from metadata
        # below; the dispatch payload carries values but logs emit names + source only.
        _test_env_vars = _str_dict(card_context.get("test_env_vars"))
        secrets.update(_test_env_vars)
        if _test_env_vars:
            # 092 US3: log only the var NAMES + source label — never values. The
            # values are routed exclusively through the redacted `secrets` channel
            # above; this confirms the redacted hand-off at the dispatch boundary.
            logger.info(
                "http_performer.test_env_injected",
                card_id=str(card_context.get("id", "")),
                context="performer_payload",
                source=str(card_context.get("test_env_source") or "card_context"),
                keys=sorted(_test_env_vars.keys()),
            )
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
                secrets, role_base_url, role_api_key_env, role_auth_token_env,
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
            # 123: the OPENAI_API_KEY injected above is the daemon's REAL OpenAI key
            # (from .env) — correct for a NATIVE opencode/junie, but it 401s and
            # poisons the 078 health probe when the backend is routed through the
            # self-hosted shim to LiteLLM (the probe reads OPENAI_API_KEY). The
            # coordinare can't see the container-mounted routing table, so it cannot
            # tell here whether this dispatch is shim-routed. Instead, ALSO expose the
            # LiteLLM master key under a DEDICATED, non-colliding env var; a shim
            # routing entry sets `upstream_auth_env: COORDINARE_PROXY_AUTH` so the
            # probe + the shim's Authorization override use the clean proxy bearer,
            # while a native (unrouted) opencode/junie ignores it and keeps the raw
            # provider keys above. Secret rides the redacted `secrets` channel.
            _proxy_bearer = os.environ.get("LITELLM_MASTER_KEY", "")
            if _proxy_bearer:
                secrets["COORDINARE_PROXY_AUTH"] = _proxy_bearer
        elif backend == "driver":
            # 521/C2: argv is world-readable, so driver takes provider config by
            # flags and API keys by env only. See _inject_driver_secrets.
            _inject_driver_secrets(
                secrets, role_auth_token_env, role_api_key_env,
            )

        _inject_orchestration_secrets(secrets, card_context.get("orchestration"))

        # Stash card_context as metadata so the performer has full access to
        # role/persona/relay_feedback/etc. without us forking the schema here.
        # 092: test_env_vars are secret-like — they ride the redacted `secrets`
        # channel above and MUST NOT be echoed into metadata (which is unredacted).
        meta_src = {k: v for k, v in card_context.items() if k != "test_env_vars"}
        metadata = json.loads(json.dumps(meta_src, default=str))

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
                "env_cache_path": env_cache_path or None,
            },
        )

    @staticmethod
    def _render_service_install_block(declared_services: list[Any], cache_mount_path: str) -> str:
        """Render the SYSTEM SERVICES install block for declared stateful services (091).

        A declared service with a coordinare-known kind (postgres, redis) contributes
        its server/client deb package(s) — derived once in
        ``env_manifest.derive_service_install_items`` (the single source of truth) —
        and instructs the agent to fetch them into ``<cache>/debs/`` via the existing
        system-package path. Returns "" when nothing stateful is declared so the
        persona is byte-for-byte unchanged (FR-006, FR-007, C-14).
        """
        if not declared_services:
            return ""
        from coordinare_service_inference.schema import ServiceEntry

        from coordinare.services.env_manifest import derive_service_install_items

        entries: list[ServiceEntry] = []
        for raw in declared_services:
            if isinstance(raw, ServiceEntry):
                entries.append(raw)
                continue
            try:
                entries.append(ServiceEntry.model_validate(raw))
            except Exception:
                # A malformed declaration must not abort the whole bootstrap; skip it.
                continue
        items = derive_service_install_items(entries)
        if not items:
            return ""
        pkgs = " ".join(sorted({i.name for i in items}))
        return (
            "SYSTEM SERVICES (stateful) — CRITICAL: this project declares stateful "
            "services that coordinare hosts in-container, but the image ships NO service "
            "binary. Fetch the following package(s) AND their full dependency closure as "
            f".deb files into {cache_mount_path}/debs/ during THIS bootstrap. Use apt's "
            "OWN dependency resolver so a meta-package (e.g. `postgresql`) expands to the "
            "concrete versioned server it depends on (the package that actually ships "
            "initdb/pg_ctl/postgres) — fetching only the named meta-package leaves the "
            "cache with NO server binaries. Resolve the download against the PRISTINE "
            "base-image package state (`-o Dir::State::status=<base-snapshot>`), NOT the "
            "live container, so the service's full runtime-library closure (e.g. ICU) is "
            "downloaded even if an earlier step already installed those libs into THIS "
            "ephemeral container (a skipped lib never reaches the persistent cache and the "
            "server then can't load it at QA time). Run: `cd "
            f'"{cache_mount_path}/debs" && apt-get update && '
            '_stat=/opt/coordinare-base-dpkg-status; [ -f "$_stat" ] || '
            "_stat=/var/lib/dpkg/status; apt-get install -y --download-only "
            '-o Dir::State::status="$_stat" '
            f'-o Dir::Cache::archives="{cache_mount_path}/debs/" {pkgs}` '
            "(--download-only resolves + downloads the full install set without installing "
            "into the live system; resolving against the base snapshot keeps it targeted — "
            "base OS libs already shipped in the image are not re-fetched), then extract "
            "every fetched .deb into a "
            f'concrete cache subdir (`for d in "{cache_mount_path}/debs/"*.deb; do dpkg-deb '
            f'-x "$d" "{cache_mount_path}/services-extract"; done` — dpkg-deb -x recreates '
            f"each deb's absolute layout, so the server binaries land in "
            f"{cache_mount_path}/services-extract/usr/bin) so the daemon and its tooling "
            "land on PATH via the auto-discovering activate.sh (it adds every */usr/bin "
            "under the cache). Do NOT pin a distro version — let apt resolve "
            "whatever server the current base image provides. Coordinare owns the service "
            "start/init/health/stop recipe (rendered into services-{start,health,stop}.sh) "
            "— you only need the binaries present in the cache. Packages: "
            f"{pkgs}.\n\n"
        )

    def _build_env_bootstrap_payload(self, card_context: dict[str, Any]) -> JobInitPayload:
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
            raise ValueError("env_bootstrap card_context missing symphony_org or symphony_repo")

        repo_url = f"https://github.com/{symphony_org}/{symphony_repo}.git"
        # Synthetic branch — bootstrap never pushes/PRs. clone_repository fetches
        # ``origin {branch}:{branch}`` and, if the ref doesn't exist remotely,
        # falls back to ``checkout -b`` from the shallow-cloned default branch.
        # Using a real branch (e.g. "main") triggers git's "refusing to fetch
        # into current branch" error, which surfaces as WorkspaceSetupError.
        branch = f"env-bootstrap-{uuid.uuid4().hex[:8]}"

        cache_mount_path = str(card_context.get("cache_mount_path") or "")
        env_spec_files = list(card_context.get("env_spec_files") or [])
        env_spec_contents: dict[str, str] = dict(card_context.get("env_spec_contents") or {})
        # 077: coordinare-derived authoritative manifest artifacts.
        dependency_checklist = str(card_context.get("dependency_checklist") or "").strip()
        verify_provided = bool(card_context.get("verify_provided"))
        activate_provided = bool(card_context.get("activate_provided"))

        secrets: dict[str, str] = {}
        # 092: THE load-bearing dry-run fix. The service-inference start-phase
        # validation runs INSIDE this bootstrap performer (the performer sets
        # os.environ from payload.secrets before _run_service_inference → validate),
        # so threading the symphony's test-env vars here is what lets the dry-run see
        # e.g. POSTGRESQL_PASSWORD and clear the unset-secret gate (exit 75). Injected
        # FIRST so the coordinare-owned operational secrets below always win on a name
        # collision; secret-like, so redacted from metadata (logs carry names only).
        _test_env_vars = _str_dict(card_context.get("test_env_vars"))
        secrets.update(_test_env_vars)
        if _test_env_vars:
            # 092 US3: keys + source label only — never values (the load-bearing
            # dry-run vars ride the redacted `secrets` channel into the bootstrap
            # performer, where the start-phase validation reads them from os.environ).
            logger.info(
                "http_performer.test_env_injected",
                card_id=str(card_context.get("id", "")),
                context="env_bootstrap_payload",
                source=str(card_context.get("test_env_source") or "card_context"),
                keys=sorted(_test_env_vars.keys()),
            )
        # Prefer the daemon-injected token (fresh App installation token or
        # configured PAT, sourced via WorkspaceManager.get_fresh_github_token);
        # fall back to the coordinare process env only when not provided.
        gh_token = str(card_context.get("_github_token") or "") or os.environ.get(
            "GITHUB_TOKEN", "",
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
                secrets, role_base_url, role_api_key_env, role_auth_token_env,
            )
            openai_key = os.environ.get("OPENAI_API_KEY", "")
            if openai_key:
                secrets["OPENAI_API_KEY"] = openai_key
        else:
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                val = os.environ.get(key, "")
                if val:
                    secrets[key] = val

        _inject_orchestration_secrets(secrets, card_context.get("orchestration"))

        spec_block = "\n\n".join(
            f"=== {path} ===\n{content}" for path, content in env_spec_contents.items()
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
        # 091: a durably-declared stateful service (postgres/redis) makes the
        # bootstrap fetch its binary into the cache via the SAME deb-into-<cache>/debs/
        # path as any other system package — the base image gains nothing. Empty
        # declared_services ⇒ no extra block (behavior unchanged).
        service_install_block = HTTPPerformerService._render_service_install_block(
            card_context.get("declared_services") or [], cache_mount_path,
        )
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
        # 092 toolchain fix: a forgetful model reads the detailed prose below, does
        # the CHEAP setup (git-clone rbenv/ruby-build, extract debs) and then DECLARES
        # DONE — never running the expensive `rbenv install` / `nvm install` /
        # `bundle install` that actually build the toolchain. The result is a cache
        # with .rbenv/ but no .rbenv/versions/<ver>, no ruby binary, no gems, and
        # verify.sh fails. A front-loaded, numbered, imperative sequence that names
        # this EXACT anti-pattern is what forgetful models attend to — the prose wall
        # alone did not stop it. Keep this FIRST (after any retry feedback).
        required_sequence = (
            "REQUIRED EXECUTION SEQUENCE — DO NOT SKIP A STEP, DO NOT STOP EARLY.\n"
            "The #1 way this job FAILS: you git-clone a version manager (rbenv, "
            "ruby-build, nvm, asdf, pyenv) and extract some .deb files, then declare "
            "done. CLONING A VERSION MANAGER IS NOT INSTALLING THE RUNTIME. A cache "
            "with .rbenv/ but no .rbenv/versions/<version>/bin/ruby is BROKEN. You "
            "MUST actually RUN the install command and WAIT for it to finish "
            "(`rbenv install <version>` COMPILES Ruby and can take many minutes — let "
            "it run to completion; do not abort it). FIRST, before any step below, "
            f"`cd {cache_mount_path}` and run `pwd`: it MUST print exactly "
            f"{cache_mount_path} (with its leading '/') — see THE INSTALL TARGET IS AN "
            "ABSOLUTE PATH rule below; every step's install prefix must be that "
            "absolute path or a subdirectory of it. Then execute, in order:\n"
            "  1. Install the EXACT pinned language runtime(s) — set the version "
            "manager's install prefix/root to a subdirectory of "
            f"{cache_mount_path} (NOT its default under $HOME; see the INSTALL-IN-PLACE "
            "rule below), run its install (e.g. `rbenv install <ver>`, "
            "`nvm install <ver>`) and WAIT for it to finish. Then confirm the binary "
            "exists (`ruby -v`, `node -v`) BEFORE moving on.\n"
            "  2. Install the dependency manager, then the project's declared "
            "dependencies via the lockfile (`gem install bundler` + "
            "`cd /repo && bundle install`; `npm ci`; `pip install -r requirements.txt`). "
            "Confirm with `bundle -v` / installed-gem checks.\n"
            "  3. Fetch + extract any declared stateful-service and system-package "
            f"debs into {cache_mount_path}/debs (see the service-install and "
            "apt-package sections below).\n"
            f"  4. RUN `bash {cache_mount_path}/verify.sh` yourself and read its "
            "output. If it exits non-zero, FIX the specific dependency it names and "
            "re-run. DO NOT end your turn, and DO NOT report success, until verify.sh "
            "exits 0. Every detail of HOW to do each step is in the sections below — "
            "but these four steps, actually executed and verified, are mandatory.\n\n"
        )
        # 092 cache-relocation fix (toolchain-agnostic). Root cause is a PATH mismatch,
        # not a writability one: whatever is installed here is CONSUMED as-is by later
        # performers, which mount this exact directory at this exact container path and
        # run the toolchain — they do NOT rebuild it. Many toolchains bake their absolute
        # build-time install path into compiled binaries (ELF RUNPATH) and generated
        # wrapper scripts (shebang lines). Installing at a default/$HOME location and
        # relocating into the cache leaves those baked paths pointing at the original
        # (now-absent) location, so the cache looks complete but its binaries/wrappers
        # fail to resolve in the consumer (verify.sh fails) — and that holds whether the
        # consume mount is read-only or read-write, because the build-time path simply
        # does not exist in the consumer container. Coordinare names NO specific tool
        # here: it states the constraint and the WHY; the agent (which already infers the
        # toolchain) owns the per-tool mechanism (install prefix / root env var).
        # 092 absolute-path fix (toolchain-agnostic). Observed failure: a forgetful
        # model treated cache_mount_path (an ABSOLUTE path like /devenv/<name>) as if
        # it were relative to its shell cwd, so the leading '/' was effectively dropped
        # and the toolchain was built under <cwd>/devenv/<name> (e.g.
        # /tmp/performer-XXXX/devenv/<name>) — ~1GB of a complete, self-consistent
        # install in the WRONG place. The real mounted volume stayed empty, the agent's
        # own verify.sh (written into the wrong dir) passed locally, and coordinare's
        # convergence check (run against the empty real mount) rejected it. This is
        # distinct from build-then-relocate (relocation_block): the path is never even
        # resolved to the right absolute location. Coordinare names no tool here; it
        # states the invariant (use the path verbatim, confirm pwd) and the WHY.
        absolute_path_block = (
            "THE INSTALL TARGET IS AN ABSOLUTE PATH — USE IT VERBATIM (a top cause of a "
            "broken cache). The install directory you are given, "
            f"{cache_mount_path}, is an ABSOLUTE path: it begins with a leading '/' and "
            "resolves from the filesystem root, NOT from your shell's current working "
            "directory. It is a real, already-mounted volume that exists at exactly that "
            "path inside this container right now. A frequent and FATAL mistake is to "
            "treat it as relative — running an install from some working directory so "
            "the leading '/' is effectively dropped and the path gets joined onto the "
            f"cwd, silently creating a DIFFERENT directory (e.g. <cwd>{cache_mount_path} "
            f"instead of {cache_mount_path}). When that happens you build a complete, "
            "self-consistent toolchain in the WRONG place: the real mounted volume stays "
            "empty, nothing you installed persists for downstream consumers, and the "
            "bootstrap is REJECTED even though your own local checks passed. To prevent "
            f"this, before installing anything: `cd {cache_mount_path}` and run `pwd` — "
            f"it MUST print exactly {cache_mount_path}, leading '/' included. Always "
            "reference the cache by this absolute path verbatim (with the leading '/'); "
            "never strip it, never join it onto another directory, and never assume your "
            "shell cwd is the cache. Every install prefix / root you set must be this "
            f"absolute path or a subdirectory of it (e.g. {cache_mount_path}/<subdir>), "
            "written out in full.\n\n"
        )
        relocation_block = (
            "INSTALL IN PLACE — NEVER BUILD-THEN-RELOCATE (the #1 cause of a "
            "complete-looking but BROKEN cache). Whatever you install here is CONSUMED "
            "as-is by later performers: they mount this exact directory at this exact "
            f"path ({cache_mount_path}) and run your toolchain — they do NOT rebuild it. "
            "Many toolchains bake their absolute build-time install path into compiled "
            "binaries (the ELF RUNPATH) and into generated wrapper scripts (shebang "
            "lines on dependency-manager executables). If you install a toolchain "
            f"ANYWHERE other than under {cache_mount_path} — for example at its default "
            "location under $HOME — and then move or copy it into the cache, those "
            "baked-in absolute paths still point at the ORIGINAL build location, which "
            "does not exist in the consumer container. The result is a cache that passes "
            "your own eyeball check but whose binaries and wrappers fail to resolve for "
            "every downstream consumer (and the consume mount may be read-only there, so "
            "the paths cannot even be patched after the fact). Therefore: set each "
            "toolchain's install prefix / root to a subdirectory of "
            f"{cache_mount_path} BEFORE you install, so everything compiles, links, and "
            "writes its wrapper shebangs against the FINAL consume-time path. Never "
            "install to a default or $HOME location and relocate.\n\n"
        )
        persona = (
            retry_block
            + checklist_block
            + required_sequence
            + absolute_path_block
            + relocation_block
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
            "PROJECT DEPENDENCIES — CRITICAL: installing the pinned runtime is only "
            "step one. You MUST then install the project's DECLARED dependencies with "
            "its native dependency manager, or every gem/module the checklist lists "
            "will be missing and verify.sh will FAIL. Specifically: if the repo has a "
            "Gemfile/Gemfile.lock, first `gem install bundler` (match the version in "
            "Gemfile.lock's `BUNDLED WITH` if one is pinned), then run `bundle install` "
            "from the repo root (`cd /repo && bundle install`) so bundler AND every gem "
            "in the Gemfile are installed under the pinned Ruby — that Ruby lives in "
            f"{cache_mount_path}, so its gem dir persists in the cache automatically. "
            "Likewise run `npm ci` (or `npm install`) for a package.json, and "
            "`pip install -r requirements.txt` / `pip install .` for a Python project "
            f"— all resolving INTO {cache_mount_path}. Do NOT hand-install gems "
            "one-by-one with `gem install <name>`; use the lockfile-driven "
            "`bundle install` so versions resolve correctly and transitive deps come "
            "along. The dependency checklist above enumerates the individual gems ONLY "
            "so verify.sh can assert each is present — the way to SATISFY that list is "
            "`bundle install`, not piecemeal installs.\n\n"
            + service_install_block
            + activate_block
            + verify_block
            + f"Spec files ({', '.join(env_spec_files) or 'none'}):\n\n"
            f"{spec_block}\n"
        )

        # 092: also drop test_env_vars — secret-like values ride the redacted
        # `secrets` channel, never the unredacted metadata.
        meta_src = {
            k: v for k, v in card_context.items() if k not in ("_github_token", "test_env_vars")
        }
        # Score (performer-side dispatch payload model) requires a `title`
        # field; BootstrapJobPayload doesn't carry one, so synthesize it from
        # the symphony name. Also seed `description` from the spec block so the
        # backend agent has user-facing context if it inspects the Score.
        symphony_name = str(card_context.get("symphony_name") or "")
        meta_src.setdefault(
            "title", f"env_bootstrap: {symphony_name}" if symphony_name else "env_bootstrap",
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
            },
        )


__all__ = ["HTTPPerformerService"]
