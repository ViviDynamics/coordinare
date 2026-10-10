"""A reader timeout must reach the daemon before any role success post-processing."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends.base import BackendStatus
from performer.backends.claude_code import ClaudeCodeBackend
from performer.main import handle_status
from performer.models import BackendEvent, BackendEventType, Performance, Score, Stand
from performer.protocol import PerformerMessage, PerformerResponse


def performance(backend, role):
    perf = Performance(
        session_id="synthetic", stand=Stand(path=Path("/tmp/synthetic"), branch="synthetic"),
        score=Score(title="Synthetic timeout", repo_url="https://github.com/example/sample",
                    branch="synthetic", github_token="synthetic"), backend=backend,
    )
    perf.role = role
    perf.state = "working"
    return perf


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["implementing", "assessing", "architecting", "reviewing",
                                   "qa", "closing_review", "documenting", "security", "diagnostic"])
async def test_timeout_does_not_run_any_role_success_path(role):
    backend = MagicMock()
    backend.get_status.return_value = BackendStatus(state="done", stop_reason="idle_timeout",
                                                    output="I will add the requested tests.")
    event = BackendEvent(type=BackendEventType.progress, text="Started")
    backend.drain_events.return_value = [event]
    perf = performance(backend, role)
    with patch("performer.main._role_dispatch_response", new=AsyncMock(return_value=None)) as dispatch, \
         patch("performer.main._pre_push_lint_response", new=AsyncMock(return_value=None)) as lint, \
         patch("performer.main._push_and_open_pr", new=AsyncMock(return_value=PerformerResponse(status="pr_opened"))) as push, \
         patch("performer.qa_postprocess.finalize_qa", new=AsyncMock(return_value=PerformerResponse(status="qa_passed"))) as qa:
        response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf)
        repeated = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf)
    assert response.status == "error"
    assert "idle timeout" in (response.reason or "").lower()
    assert response.events == [event.model_dump()]
    assert repeated.status == "error" and repeated.reason == response.reason
    assert perf.state == "error"
    for collaborator in (dispatch, lint, push, qa):
        collaborator.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_reader_timeout_reaches_status_as_timeout():
    backend = ClaudeCodeBackend()
    proc = MagicMock()
    proc.stdout.read = AsyncMock(side_effect=asyncio.TimeoutError())
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    backend._proc = proc
    backend._output_accumulator = ["Partial assistant narration"]
    await backend._event_reader_loop()
    assert backend.get_status().stop_reason == "idle_timeout"
    perf = performance(backend, "implementing")
    with patch("performer.main._role_dispatch_response", new=AsyncMock(return_value=None)), \
         patch("performer.main._pre_push_lint_response", new=AsyncMock(return_value=None)), \
         patch("performer.main._push_and_open_pr", new=AsyncMock(return_value=PerformerResponse(status="pr_opened"))) as push:
        response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf)
    assert response.status == "error"
    assert "idle timeout" in (response.reason or "").lower()
    push.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state,output", [("done", None), ("error", "partial"),
                                          ("done", "ghp_" + "A" * 36)])
async def test_timeout_reason_does_not_expose_partial_output(state, output):
    backend = MagicMock()
    backend.get_status.return_value = BackendStatus(state=state, stop_reason="idle_timeout", output=output)
    backend.drain_events.return_value = []
    perf = performance(backend, "implementing")
    response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf)
    assert response.status == "error" and "idle timeout" in response.reason.lower()
    assert "ghp_" not in response.reason and "partial" not in response.reason
