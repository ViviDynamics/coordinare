"""A fatal executor error must reach the caller and the benchmark artifact.

#306: the performer logged ``job_executor_failed`` after
``push_branch.rebase_conflict`` reported unstaged changes, coordinare kept
polling the same implementing session until the 600s deadline, and the retained
``run.json`` recorded a *cancelled* dispatch with ``terminal_marker: null``. A
real failure was indistinguishable from "the budget cut it off".

Two boundaries are pinned here:

1. The reachable path. A real ``JobRunner`` executor exception, served over the
   real ``GET /jobs/{id}`` shape, must reach ``HTTPPerformerService.check_status``
   as a terminal non-null failure classification, observed once, not as another
   ``working`` poll.
2. The lossy path. When the terminal state can only be observed as a raised
   poll (an unreachable container, a reaped job), the bench recorder must record
   that a poll failed instead of dropping it, so the artifact does not report a
   clean cancellation it never witnessed.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from coordinare.bench.recording_performer import RecordingPerformer
from coordinare.bench.runner import _records_to_dispatch_log
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.transport.http_transport import PerformerHTTPClient


def _service(handler) -> HTTPPerformerService:
    config = PerformerEndpointConfig.model_validate({
        "id": "perf-p1", "mode": "persistent", "roles": ["implementing"],
        "image": "performer:base", "endpoint": "http://127.0.0.1:8080",
    })
    client = PerformerHTTPClient(
        "http://127.0.0.1:8080",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return HTTPPerformerService(config, client=client)


async def _failed_job_status_json(exc: Exception) -> str:
    """Run a REAL JobRunner whose executor raises, and return the JobStatus the
    performer's ``GET /jobs/{id}`` would serve. Not a hand-built dict: the whole
    point of #306 is that the terminal state was lost somewhere in this chain."""
    from performer.server.job_runner import JobRunner
    from performer.server.models import JobInitPayload, JobResult

    async def boom(_: JobInitPayload) -> JobResult:
        raise exc

    payload = JobInitPayload(
        job_id="job-306", card_id="PVTI_1", role="implementer",
        backend="claude_code", persona="default",
        repo_url="https://example.com/repo.git", branch="main",
    )
    runner = JobRunner(boom, required_secrets=())
    await runner.submit(payload)
    assert runner._task is not None
    await runner._task

    status = runner.get("job-306")
    assert status.state == "failed"                      # the runner did classify it
    assert status.result is not None
    assert status.result.error_code == "executor_error"
    return status.model_dump_json()


@pytest.mark.asyncio
async def test_executor_exception_reaches_the_caller_as_a_terminal_failure() -> None:
    """The workspace error that actually happened, end to end."""
    from performer.workspace import WorkspaceSetupError

    body = await _failed_job_status_json(
        WorkspaceSetupError("push_branch.rebase_conflict: cannot rebase; unstaged changes"),
    )
    status_polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_polls
        if request.method == "GET" and request.url.path == "/jobs/job-306":
            status_polls += 1
            return httpx.Response(
                200, content=body, headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json={})  # the persistent-mode reset that follows

    result = await _service(handler).check_status("job-306")

    assert result["status"] == "error"          # not "working": polling stops here
    assert result["state"] == "failed"          # non-null failure classification
    assert result["reason"]                     # the diagnostic survives, not None
    assert "unstaged changes" in result["reason"]
    assert status_polls == 1                    # observed once, no polling on


@pytest.mark.asyncio
async def test_executor_exception_is_not_reported_as_still_working() -> None:
    """The regression's negative half: a failed job must never look in-flight."""
    body = await _failed_job_status_json(RuntimeError("executor died"))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = await _service(handler).check_status("job-306")
    assert result["status"] != "working"
    assert result.get("job_state") is None      # the non-terminal shape is not used


class _PollRaises:
    """A performer whose status poll always raises, the shape an ephemeral
    container that died after its executor failed presents to the bench."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.polls = 0

    async def dispatch_card(self, _ctx: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return {"status": "ok", "session_id": "s1", "job_id": "j1"}

    async def check_status(self, _sid: str, **_kw: Any) -> dict[str, Any]:
        self.polls += 1
        raise self._exc


@pytest.mark.asyncio
async def test_failed_polls_are_recorded_not_silently_dropped() -> None:
    """The exact artifact signature #306 reports.

    Before the fix this produced ``status: cancelled, terminal_marker: null,
    seconds: null``, byte-for-byte the retained run.json row, and therefore
    indistinguishable from a dispatch the wall-clock budget cut off cleanly.
    """
    from coordinare.transport.http_transport import TransportError

    svc = RecordingPerformer(_PollRaises(TransportError("performer unreachable")))
    await svc.dispatch_card({"stage": "implementing", "role": "implementer", "item_id": "PVTI_1"})
    for _ in range(40):
        with pytest.raises(TransportError):
            await svc.check_status("s1")

    row = _records_to_dispatch_log({"implementing": svc})[0]
    assert row["status"] == "error"             # NOT "cancelled"
    assert row["poll_error"]                    # what actually happened is on the row
    assert "unreachable" in row["poll_error"]
    assert row["finished_at"] is not None       # the row is not silently timeless
    assert row["seconds"] is not None


def test_the_dispatch_model_still_carries_poll_error() -> None:
    """The dict row above is pinned; the run.json model field it feeds is not.
    Pydantic v2 silently ignores an unexpected kwarg, so deleting the field
    would drop it from the artifact with every test above still passing."""
    from coordinare.bench.artifact import PersonaDispatch

    dump = PersonaDispatch(
        stage="implementing", status="error", poll_error="boom",
    ).model_dump()
    assert dump["poll_error"] == "boom"


@pytest.mark.asyncio
async def test_a_raised_poll_never_outranks_a_real_terminal_status() -> None:
    """A transient poll failure followed by a real terminal status must not
    overwrite the verdict. check_status raising is not always terminal: a bare
    poll timeout is retried by monitor_performer while the job still runs."""
    from coordinare.transport.http_transport import TransportTimeoutError

    class _FlakyThenTerminal:
        def __init__(self) -> None:
            self.polls = 0

        async def dispatch_card(self, _ctx: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            return {"status": "ok", "session_id": "s1", "job_id": "j1"}

        async def check_status(self, _sid: str, **_kw: Any) -> dict[str, Any]:
            self.polls += 1
            if self.polls == 1:
                msg = "poll deadline exceeded during a long build"
                raise TransportTimeoutError(msg)
            return {"status": "pr_opened", "metrics": {"tokens_processed": 1234}}

    svc = RecordingPerformer(_FlakyThenTerminal())
    await svc.dispatch_card({"stage": "implementing", "role": "implementer", "item_id": "PVTI_1"})
    with pytest.raises(TransportTimeoutError):
        await svc.check_status("s1")
    await svc.check_status("s1")

    row = _records_to_dispatch_log({"implementing": svc})[0]
    assert row["terminal_marker"] == "pr_opened"
    assert row["status"] == "succeeded"
    assert row["tokens_processed"] == 1234
    assert row["poll_error"] is None            # the real verdict wins


@pytest.mark.asyncio
async def test_a_dispatch_the_budget_cut_off_still_reads_as_cancelled() -> None:
    """The other side of the contract: no polls failed and none were terminal,
    so this genuinely is a budget cutoff and must keep saying so."""
    class _NeverTerminal:
        async def dispatch_card(self, _ctx: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            return {"status": "ok", "session_id": "s9"}

        async def check_status(self, _sid: str, **_kw: Any) -> dict[str, Any]:
            return {"status": "working"}

    svc = RecordingPerformer(_NeverTerminal())
    await svc.dispatch_card({"stage": "implementing", "item_id": "PVTI_1"})
    await svc.check_status("s9")

    row = _records_to_dispatch_log({"implementing": svc})[0]
    assert row["status"] == "cancelled"
    assert row["terminal_marker"] is None
    assert row["poll_error"] is None
