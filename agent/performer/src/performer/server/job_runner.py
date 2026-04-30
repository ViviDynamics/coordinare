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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

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

            started = _utcnow()
            self._status = JobStatus(
                job_id=payload.job_id,
                state="accepted",
                started_at=started,
            )
            self._update_event = asyncio.Event()
            self._task = asyncio.create_task(self._run(payload))
            return JobAcceptResponse(job_id=payload.job_id, started_at=started)

    async def _run(self, payload: JobInitPayload) -> None:
        if self._status is None:
            raise RuntimeError("_run called without active status — submit() must set _status first")
        self._status = self._status.model_copy(update={"state": "running"})
        self._signal_update()
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
                        summary=f"executor raised: {type(exc).__name__}",
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


__all__ = ["JobExecutor", "JobNotFoundError", "JobRunner"]
