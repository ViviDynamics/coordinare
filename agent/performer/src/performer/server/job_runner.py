"""Single-slot async job runner for the performer HTTP server (spec 056, T022).

The runner enforces the contract that a performer accepts at most one job at
a time. It does not know how to *execute* a job — that is delegated to a
``Callable[[JobInitPayload], Awaitable[JobResult]]`` injected at construction
time. The runner only owns the lifecycle: accept, run, stream status, cancel,
clean up.

Secret resolution (T058): When a job is submitted, the runner checks that any
required secrets are available before accepting the job. Missing required
secrets return JobBusyResponse with reason="secret_missing".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import _redact_secrets
from performer.server.models import (
    CancelResponse,
    JobAcceptResponse,
    JobBusyResponse,
    JobInitPayload,
    JobResult,
    JobStatus,
)
from performer.server.secrets import SecretMissingError

if TYPE_CHECKING:
    from performer.server.secrets import SecretResolver

log = structlog.get_logger(__name__)

JobExecutor = Callable[[JobInitPayload], Awaitable[JobResult]]

# Contextvar that holds the active runner's push_progress callback during job execution.
# Set by JobRunner._run() so _perform_job can push intermediate events without a
# direct reference to the runner.
_progress_cb_var: ContextVar[Callable[[list[Any], dict[str, Any] | None], None] | None] = (
    ContextVar("_progress_cb", default=None)
)

# US5 / Spec 073 Phase 9: shared mutable holder for secrets refreshed mid-job
# (e.g. github_token rotated to dodge the GitHub App 1h expiry boundary).
# The PATCH /jobs/{id}/secrets endpoint mutates the dict via
# JobRunner.refresh_secrets(); the running _perform_job loop reads it each tick
# and re-injects into perf.score/perf.stand. The dict reference is published
# into a ContextVar at _run() entry so the in-job loop can find it without a
# back-reference to the runner. Contents must never be logged.
_refreshed_secrets_var: ContextVar[dict[str, str] | None] = ContextVar(
    "_refreshed_secrets", default=None
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobNotFoundError(LookupError):
    """Raised when a job_id is not the active job."""


class JobRunner:
    def __init__(
        self,
        executor: JobExecutor,
        resolver: SecretResolver | None = None,
        required_secrets: tuple[str, ...] = ("GITHUB_TOKEN",),
    ) -> None:
        self._executor = executor
        self._resolver = resolver
        self._required_secrets = required_secrets
        self._lock = asyncio.Lock()
        self._status: JobStatus | None = None
        self._task: asyncio.Task[None] | None = None
        self._update_event = asyncio.Event()
        self._live_events: list[dict[str, Any]] = []
        self._live_metrics: dict[str, Any] | None = None
        # Mutable, shared with the running job task via _refreshed_secrets_var.
        # PATCH /jobs/{id}/secrets writes here; the _perform_job loop reads.
        self._refreshed_secrets: dict[str, str] = {}

    @property
    def current_job_id(self) -> str | None:
        if self._status is None:
            return None
        if self._status.state in {"succeeded", "failed", "cancelled"}:
            return None
        return self._status.job_id

    async def submit(
        self, payload: JobInitPayload
    ) -> JobAcceptResponse | JobBusyResponse:
        async with self._lock:
            if self.current_job_id is not None:
                return JobBusyResponse(reason="busy", retry_after_s=10)

            # T058: Pre-flight secret existence check.
            # Populate init_secrets from the job payload so the init_payload source
            # is checked with this job's secrets (not the empty startup defaults).
            # resolve() is called for its side-effect (raises SecretMissingError if absent);
            # the return value is intentionally discarded. The executor reads secrets
            # directly from payload.secrets or the container environment at run time.
            if self._resolver is not None:
                self._resolver.init_secrets = {
                    k: v.get_secret_value() for k, v in payload.secrets.items()
                }
                for secret_name in self._required_secrets:
                    try:
                        self._resolver.resolve(secret_name)
                    except SecretMissingError as exc:
                        return JobBusyResponse(
                            reason="secret_missing",
                            detail=exc.name,
                            retry_after_s=None,
                        )
                # Backend-specific pre-flight: ensure the API key for the selected
                # backend is present so the job fails fast rather than mid-run.
                # Use resolve() so env/creds_file fallback sources are honoured.
                _backend = payload.backend.replace("-", "_")
                # claude_code accepts either ANTHROPIC_API_KEY (direct Anthropic auth,
                # x-api-key) or ANTHROPIC_AUTH_TOKEN (Bearer, used when routed through
                # a LiteLLM-style proxy that owns its own upstream credential). Codex
                # still strictly requires OPENAI_API_KEY.
                _backend_key_groups: dict[str, tuple[str, ...]] = {
                    "codex": ("OPENAI_API_KEY",),
                    "claude_code": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                }
                _required_keys = _backend_key_groups.get(_backend)
                if _required_keys:
                    _last_missing: SecretMissingError | None = None
                    for _candidate in _required_keys:
                        try:
                            self._resolver.resolve(_candidate)
                            _last_missing = None
                            break
                        except SecretMissingError as exc:
                            _last_missing = exc
                    if _last_missing is not None:
                        return JobBusyResponse(
                            reason="secret_missing",
                            detail=" or ".join(_required_keys),
                            retry_after_s=None,
                        )

            started = _utcnow()
            self._status = JobStatus(
                job_id=payload.job_id,
                state="accepted",
                started_at=started,
            )
            self._live_events = []
            self._live_metrics = None
            self._update_event = asyncio.Event()
            self._task = asyncio.create_task(self._run(payload))
            return JobAcceptResponse(job_id=payload.job_id, started_at=started)

    async def refresh_secrets(
        self, job_id: str, secrets: dict[str, str]
    ) -> None:
        """Merge PATCH-delivered secrets into the running job's holder.

        Raises ``JobNotFoundError`` if ``job_id`` is not the currently active
        job — there is no point caching secrets for a job that has already
        finished or never existed. Secret values must not be logged here.
        """
        if self._status is None or self._status.job_id != job_id:
            raise JobNotFoundError(job_id)
        if self._status.state in {"succeeded", "failed", "cancelled"}:
            raise JobNotFoundError(job_id)
        # Mutate in place — the in-job loop holds the same dict reference via
        # _refreshed_secrets_var, so an .update() is sufficient.
        self._refreshed_secrets.update(secrets)
        log.debug(
            "job_runner.secrets_refreshed",
            job_id=job_id,
            keys=sorted(secrets.keys()),
        )

    def push_progress(
        self,
        events: list[dict[str, Any]],
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Called by the executor to stream intermediate events and metrics."""
        if events:
            self._live_events = (self._live_events + events)[-200:]
        if metrics is not None:
            self._live_metrics = metrics
        if self._status is not None and (events or metrics is not None):
            self._signal_update()

    async def _run(self, payload: JobInitPayload) -> None:
        if self._status is None:
            raise RuntimeError("_run called without active status — submit() must set _status first")
        self._status = self._status.model_copy(update={"state": "running"})
        self._signal_update()
        _progress_cb_var.set(self.push_progress)
        # Publish the runner's mutable refreshed-secrets dict so the in-job
        # loop can read fresh values without a back-reference to the runner.
        _refreshed_secrets_var.set(self._refreshed_secrets)
        try:
            result = await self._executor(payload)
            self._status = self._status.model_copy(
                update={
                    "state": "succeeded" if result.success else "failed",
                    "finished_at": _utcnow(),
                    "result": result,
                    "progress_pct": 100,
                }
            )
        except asyncio.CancelledError:
            self._status = self._status.model_copy(
                update={
                    "state": "cancelled",
                    "finished_at": _utcnow(),
                    "result": JobResult(
                        success=False,
                        summary="job cancelled",
                        error_code="cancelled",
                    ),
                }
            )
            self._signal_update()
            raise
        except Exception as exc:  # noqa: BLE001 — surface arbitrary executor failure
            log.exception("job_executor_failed", job_id=payload.job_id)
            self._status = self._status.model_copy(
                update={
                    "state": "failed",
                    "finished_at": _utcnow(),
                    "result": JobResult(
                        success=False,
                        summary=_redact_secrets(f"{type(exc).__name__}: {str(exc)[:500]}"),
                        error_code="executor_error",
                    ),
                }
            )
        finally:
            self._signal_update()

    def _signal_update(self) -> None:
        self._update_event.set()
        self._update_event = asyncio.Event()

    def get(self, job_id: str) -> JobStatus:
        if self._status is None or self._status.job_id != job_id:
            raise JobNotFoundError(job_id)
        if self._live_events or self._live_metrics is not None:
            return self._status.model_copy(
                update={"events": list(self._live_events), "metrics": self._live_metrics}
            )
        return self._status

    async def stream(self, job_id: str) -> AsyncIterator[JobStatus]:
        if self._status is None or self._status.job_id != job_id:
            raise JobNotFoundError(job_id)
        last_state: str | None = None
        while True:
            # Capture the current event *before* reading status so that any
            # _signal_update() call that fires between here and the await below
            # is guaranteed to have already set this event object.
            event = self._update_event
            status = self._status
            if status is None or status.job_id != job_id:
                return
            if status.state != last_state:
                yield status
                last_state = status.state
            if status.state in {"succeeded", "failed", "cancelled"}:
                return
            await event.wait()

    async def cancel(self, job_id: str) -> CancelResponse:
        if self._status is None or self._status.job_id != job_id:
            raise JobNotFoundError(job_id)
        if self._status.state in {"succeeded", "failed", "cancelled"}:
            return CancelResponse(honored=False, detail="job already terminal")
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return CancelResponse(honored=True)


__all__ = [
    "JobExecutor",
    "JobNotFoundError",
    "JobRunner",
    "_progress_cb_var",
    "_refreshed_secrets_var",
]
