"""Unit tests for performer.main — handles dispatch, status, health, relay_feedback, isolation."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends import UnsupportedBackendError
from performer.backends.base import BackendStatus
from performer.config import Settings, get_settings
from performer.github import GitHubAPIError
from performer.main import (
    _doc_folder,
    collect_metrics,
    handle_dispatch,
    handle_health,
    handle_relay_feedback,
    handle_status,
    run_loop,
)
from performer.models import Performance, Score, Stand
from performer.protocol import PerformerMessage, PerformerResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(action: str, session_id: str = "", **payload) -> PerformerMessage:  # type: ignore[type-arg]
    return PerformerMessage(action=action, session_id=session_id, payload=payload)  # type: ignore[arg-type]


def _dispatch_msg() -> PerformerMessage:
    return _msg(
        "dispatch",
        title="T",
        repo_url="https://github.com/org/repo",
        branch="feat/x",
        github_token="tok",
    )


def _make_perf(session_id: str = "sid", state: str = "working") -> Performance:
    stand = Stand(path=Path("/tmp/x"), branch="feat/x")
    score = Score(
        title="T",
        repo_url="https://github.com/org/repo",
        branch="feat/x",
        github_token="tok",
    )
    backend = MagicMock()
    backend.get_status.return_value = BackendStatus(state="working")
    backend.relay_feedback = AsyncMock()
    perf = Performance(session_id=session_id, stand=stand, score=score, backend=backend)
    perf.state = state  # type: ignore[assignment]
    return perf


def _settings(backend: str = "opencode") -> Settings:
    get_settings.cache_clear()
    return Settings(AGENT_BACKEND=backend, AGENT_TIMEOUT=5)


# ---------------------------------------------------------------------------
# handle_health
# ---------------------------------------------------------------------------

class TestHandleHealth:
    def test_valid_backend_returns_healthy(self) -> None:
        with patch("performer.main.get_backend"):
            resp = handle_health(_settings("opencode"))
        assert resp.status == "healthy"

    def test_unsupported_backend_returns_unhealthy(self) -> None:
        with patch("performer.main.get_backend", side_effect=UnsupportedBackendError("foobar")):
            resp = handle_health(_settings("foobar"))
        assert resp.status == "unhealthy"
        assert "foobar" in (resp.reason or "")

    def test_response_time_under_2s(self) -> None:
        with patch("performer.main.get_backend"):
            t0 = time.monotonic()
            handle_health(_settings())
            elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"health check took {elapsed:.3f}s — exceeds SC-002 budget"


# ---------------------------------------------------------------------------
# handle_dispatch
# ---------------------------------------------------------------------------

class TestHandleDispatch:
    async def test_returns_accepted_with_session_id(self) -> None:
        mock_backend = MagicMock()
        mock_backend.start = AsyncMock()
        with (
            patch("performer.main.clone_repository", new=AsyncMock(return_value=MagicMock())),
            patch("performer.main.get_backend", return_value=mock_backend),
        ):
            t0 = time.monotonic()
            resp, perf = await handle_dispatch(_dispatch_msg(), _settings())
            elapsed = time.monotonic() - t0
        assert resp.status == "accepted"
        assert resp.session_id  # non-empty UUID
        # SC-003: accepted within 5s
        assert elapsed < 5.0, f"dispatch acceptance took {elapsed:.3f}s — exceeds SC-003 budget"

    async def test_invalid_payload_raises(self) -> None:
        bad_msg = _msg("dispatch", title="T")  # missing repo_url, branch, github_token
        with pytest.raises(Exception):
            await handle_dispatch(bad_msg, _settings())


# ---------------------------------------------------------------------------
# handle_status
# ---------------------------------------------------------------------------

class TestHandleStatus:
    async def test_unknown_session_returns_session_expired(self) -> None:
        resp = await handle_status(_msg("status", session_id="unknown"), None)
        assert resp.status == "session_expired"

    async def test_working_returns_working_with_metrics(self) -> None:
        perf = _make_perf(session_id="sid")
        resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "working"
        assert resp.metrics is not None
        assert resp.metrics.pid is not None

    async def test_blocked_returns_blocked_with_questions(self) -> None:
        perf = _make_perf(session_id="sid")
        perf.backend.get_status.return_value = BackendStatus(
            state="blocked", questions=["Which CI?"]
        )
        resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "blocked"
        assert resp.metrics is None
        assert "Which CI?" in resp.questions

    async def test_error_returns_error(self) -> None:
        perf = _make_perf(session_id="sid")
        perf.backend.get_status.return_value = BackendStatus(state="error", error_reason="boom")
        resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "error"
        assert "boom" in (resp.reason or "")
        assert resp.metrics is None

    async def test_done_pushes_and_transitions_to_waiting_for_checks(self) -> None:
        perf = _make_perf(session_id="sid")
        perf.backend.get_status.return_value = BackendStatus(state="done")
        with (
            patch("performer.main.push_branch", new=AsyncMock()),
            patch(
                "performer.main.create_pull_request",
                new=AsyncMock(return_value=("https://github.com/org/repo/pull/1", "PR_n1")),
            ),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "working"
        assert perf.state == "waiting_for_checks"
        assert perf.pr_url == "https://github.com/org/repo/pull/1"
        assert perf.pr_head_sha == "abc123"

    async def test_session_timeout_returns_error(self) -> None:
        """FR-015: if AGENT_TIMEOUT is exceeded, handle_status returns error and stops backend."""
        perf = _make_perf(session_id="sid")
        perf.backend.stop = AsyncMock()
        # Make started_at appear to be 1 hour ago
        perf.started_at = datetime.now(UTC) - timedelta(hours=1)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=5)
        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp.status == "error"
        assert "timed out" in (resp.reason or "")
        perf.backend.stop.assert_called_once()

    async def test_no_timeout_when_settings_none(self) -> None:
        """When settings is None, no deadline is enforced even for old sessions."""
        perf = _make_perf(session_id="sid")
        perf.started_at = datetime.now(UTC) - timedelta(hours=1)
        resp = await handle_status(_msg("status", session_id="sid"), perf, None)
        assert resp.status == "working"  # not timed out

    async def test_session_timeout_backend_stop_exception_swallowed(self) -> None:
        """If backend.stop() raises during session timeout, the error is swallowed."""
        perf = _make_perf(session_id="sid")
        perf.backend.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        perf.started_at = datetime.now(UTC) - timedelta(hours=1)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=5)
        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)
        # Despite stop() raising, we still get an error response
        assert resp.status == "error"
        assert "timed out" in (resp.reason or "")


# ---------------------------------------------------------------------------
# handle_relay_feedback
# ---------------------------------------------------------------------------

class TestHandleRelayFeedback:
    async def test_no_session_returns_session_expired(self) -> None:
        msg = _msg("relay_feedback", session_id="x", feedback="go ahead")
        resp = await handle_relay_feedback(msg, None)
        assert resp.status == "session_expired"

    async def test_calls_backend_relay_and_returns_acknowledged(self) -> None:
        perf = _make_perf(session_id="sid")
        msg = _msg("relay_feedback", session_id="sid", feedback="use GitHub Actions")
        resp = await handle_relay_feedback(msg, perf)
        assert resp.status == "acknowledged"
        perf.backend.relay_feedback.assert_called_once_with("use GitHub Actions")

    async def test_structured_coordinare_payload_is_normalised(self) -> None:
        """Coordinare sends {"pr_url": ..., "comments": [{"body": ...}]} — must not be dropped."""
        perf = _make_perf(session_id="sid")
        msg = _msg(
            "relay_feedback",
            session_id="sid",
            pr_url="https://github.com/org/repo/pull/1",
            comments=[{"body": "Please add tests."}, {"body": "Fix lint."}],
        )
        resp = await handle_relay_feedback(msg, perf)
        assert resp.status == "acknowledged"
        forwarded: str = perf.backend.relay_feedback.call_args[0][0]
        assert "PR: https://github.com/org/repo/pull/1" in forwarded
        assert "Please add tests." in forwarded
        assert "Fix lint." in forwarded

    async def test_empty_comments_list_sends_empty_string(self) -> None:
        perf = _make_perf(session_id="sid")
        msg = _msg("relay_feedback", session_id="sid", pr_url="https://github.com/org/repo/pull/1", comments=[])
        await handle_relay_feedback(msg, perf)
        forwarded: str = perf.backend.relay_feedback.call_args[0][0]
        assert "PR: https://github.com/org/repo/pull/1" in forwarded

    async def test_string_items_in_comments_are_included(self) -> None:
        """Comments list items that are plain strings are forwarded as-is."""
        perf = _make_perf(session_id="sid")
        msg = _msg(
            "relay_feedback",
            session_id="sid",
            comments=["First string comment", "Second string comment"],
        )
        await handle_relay_feedback(msg, perf)
        forwarded: str = perf.backend.relay_feedback.call_args[0][0]
        assert "First string comment" in forwarded
        assert "Second string comment" in forwarded


# ---------------------------------------------------------------------------
# Isolation (US6)
# ---------------------------------------------------------------------------

class TestIsolation:
    async def test_cleanup_called_after_pr_opened(self) -> None:
        perf = _make_perf(session_id="sid")
        perf.backend.get_status.return_value = BackendStatus(state="done")
        with (
            patch("performer.main.push_branch", new=AsyncMock()),
            patch("performer.main.create_pull_request", new=AsyncMock(return_value=("u", "n"))),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="sha1")),
            patch("performer.main.cleanup_stand") as mock_cleanup,
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
            assert resp.status == "working"  # now transitions through waiting_for_checks
            # Simulate finally cleanup
            t0 = time.monotonic()
            from performer.main import cleanup_stand as _cs
            _cs(perf.stand)
            elapsed = time.monotonic() - t0
        mock_cleanup.assert_called_once()
        assert elapsed < 30.0, f"cleanup took {elapsed:.3f}s — exceeds SC-004 budget"

    async def test_second_dispatch_gets_fresh_stand(self) -> None:
        backend = MagicMock()
        backend.start = AsyncMock()
        stand1 = MagicMock()
        stand2 = MagicMock()
        with (
            patch(
                "performer.main.clone_repository",
                new=AsyncMock(side_effect=[stand1, stand2]),
            ),
            patch("performer.main.get_backend", return_value=backend),
        ):
            _, perf1 = await handle_dispatch(_dispatch_msg(), _settings())
            _, perf2 = await handle_dispatch(_dispatch_msg(), _settings())
        assert perf1.stand is stand1
        assert perf2.stand is stand2
        assert perf1.stand is not perf2.stand


# ---------------------------------------------------------------------------
# collect_metrics
# ---------------------------------------------------------------------------

class TestCollectMetrics:
    def test_returns_metrics_with_pid(self) -> None:
        backend = MagicMock()
        backend.get_status.return_value = BackendStatus(state="working")
        import performer.main as m
        m._self_process = None  # reset
        metrics = collect_metrics(backend)
        assert metrics.pid is not None
        assert metrics.pid > 0

    def test_handles_no_such_process_gracefully(self) -> None:
        import psutil
        backend = MagicMock()
        backend.get_status.return_value = BackendStatus(state="working")
        with patch("performer.main.psutil.Process", side_effect=psutil.NoSuchProcess(0)):
            import performer.main as m
            m._self_process = None
            metrics = collect_metrics(backend)
        # Should not raise; pid still set from os.getpid()
        assert metrics.pid is not None


# ---------------------------------------------------------------------------
# Backend factory (US4)
# ---------------------------------------------------------------------------

class TestBackendFactory:
    def test_opencode_returns_adapter(self) -> None:
        from performer.backends import get_backend
        from performer.backends.opencode import OpenCodeAdapter
        adapter = get_backend("opencode")
        assert isinstance(adapter, OpenCodeAdapter)

    def test_unknown_raises_unsupported(self) -> None:
        from performer.backends import UnsupportedBackendError, get_backend
        with pytest.raises(UnsupportedBackendError, match="foobar"):
            get_backend("foobar")

    def test_health_unhealthy_for_bad_backend(self) -> None:
        resp = handle_health(_settings("foobar"))
        assert resp.status == "unhealthy"
        assert "foobar" in (resp.reason or "")


# ---------------------------------------------------------------------------
# handle_dispatch — stand cleanup on backend failure
# ---------------------------------------------------------------------------

class TestHandleDispatchCleanup:
    async def test_stand_cleaned_up_when_backend_start_fails(self, tmp_path: Path) -> None:
        """If backend.start raises, the cloned stand is cleaned up before re-raising."""
        stand = MagicMock()
        stand.path = tmp_path / "stand"
        stand.path.mkdir()

        mock_backend = MagicMock()
        mock_backend.start = AsyncMock(side_effect=RuntimeError("opencode not found"))

        with (
            patch("performer.main.clone_repository", new=AsyncMock(return_value=stand)),
            patch("performer.main.get_backend", return_value=mock_backend),
            patch("performer.main.cleanup_stand") as mock_cleanup,
        ):
            with pytest.raises(RuntimeError, match="opencode not found"):
                await handle_dispatch(_dispatch_msg(), _settings())

        mock_cleanup.assert_called_once_with(stand)


# ---------------------------------------------------------------------------
# run_loop — stdin→stdout wire protocol
# ---------------------------------------------------------------------------


def _make_line(**kwargs: object) -> bytes:  # type: ignore[type-arg]
    return (json.dumps(kwargs) + "\n").encode()


async def _run_loop_with(
    lines: list[bytes],
    settings: Settings | None = None,
) -> list[PerformerResponse]:
    """Feed lines into run_loop and return the PerformerResponse objects written."""
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line)
    reader.feed_eof()

    captured: list[PerformerResponse] = []
    loop = asyncio.get_running_loop()
    s = settings or _settings()

    with (
        patch("performer.main.asyncio.StreamReader", return_value=reader),
        patch("performer.main.asyncio.StreamReaderProtocol"),
        patch("performer.main.get_settings", return_value=s),
        patch.object(loop, "connect_read_pipe", AsyncMock()),
        patch("performer.main._write_response", side_effect=captured.append),
    ):
        await run_loop()

    return captured


class TestRunLoop:
    async def test_invalid_json_returns_error_and_continues(self) -> None:
        """Invalid JSON line produces an error response; loop exits cleanly on EOF."""
        responses = await _run_loop_with([b"not-valid-json\n"])
        assert len(responses) == 1
        assert responses[0].status == "error"
        assert "invalid message" in (responses[0].reason or "")

    async def test_empty_lines_are_skipped(self) -> None:
        """Blank lines produce no output."""
        responses = await _run_loop_with([b"\n", b"   \n"])
        assert responses == []

    async def test_health_returns_healthy(self) -> None:
        with patch("performer.main.get_backend"):
            responses = await _run_loop_with([_make_line(action="health")])
        assert responses[0].status == "healthy"

    async def test_unknown_action_fails_validation_and_returns_error(self) -> None:
        """Actions outside the Literal constraint fail pydantic validation → error response."""
        responses = await _run_loop_with([_make_line(action="unknown_thing")])
        assert responses[0].status == "error"
        assert "invalid message" in (responses[0].reason or "")

    async def test_dispatch_failure_returns_error_and_exits(self) -> None:
        """WorkspaceSetupError from handle_dispatch produces an error response and exits."""
        from performer.workspace import WorkspaceSetupError

        with patch(
            "performer.main.handle_dispatch",
            new=AsyncMock(side_effect=WorkspaceSetupError("disk full")),
        ):
            responses = await _run_loop_with([
                _make_line(
                    action="dispatch",
                    title="T",
                    repo_url="https://github.com/org/repo",
                    branch="feat/x",
                    github_token="tok",
                )
            ])
        assert responses[0].status == "error"
        assert "disk full" in (responses[0].reason or "")

    async def test_cleanup_runs_after_terminal_error(self) -> None:
        """Backend stop and cleanup_stand are called after the loop exits on error."""
        mock_backend = MagicMock()
        mock_backend.stop = AsyncMock()

        stand = MagicMock()
        stand.path = Path("/tmp/test-stand")

        perf = MagicMock()
        perf.session_id = "sid"
        perf.backend = mock_backend
        perf.stand = stand
        perf.started_at = datetime.now(UTC)  # fresh — watchdog won't fire

        with (
            patch("performer.main.handle_dispatch", new=AsyncMock(return_value=(
                PerformerResponse(status="accepted", session_id="sid"),
                perf,
            ))),
            patch("performer.main.handle_status", new=AsyncMock(
                return_value=PerformerResponse(status="error", session_id="sid")
            )),
            patch("performer.main.cleanup_stand") as mock_cleanup,
        ):
            await _run_loop_with([
                _make_line(action="dispatch", title="T", repo_url="https://github.com/org/repo",
                           branch="feat/x", github_token="tok"),
                _make_line(action="status", session_id="sid"),
            ])

        mock_backend.stop.assert_called_once()
        mock_cleanup.assert_called_once_with(stand)

    async def test_session_expired_with_no_perf_exits_loop(self) -> None:
        """session_expired when no active session should exit the loop immediately."""
        responses = await _run_loop_with([
            _make_line(action="status", session_id="no-such-session"),
            # This second line should never be processed
            _make_line(action="health"),
        ])
        assert responses[0].status == "session_expired"
        # Only one response — loop exited after session_expired
        assert len(responses) == 1

    async def test_dispatch_validation_error_reports_field_names_only(self) -> None:
        """ValidationError from Score(**payload) returns field names without values."""
        responses = await _run_loop_with([
            _make_line(
                action="dispatch",
                title="T",
                repo_url="not-a-github-url",  # fails Score._validate_repo_url
                branch="feat/x",
                github_token="tok",
            )
        ])
        assert responses[0].status == "error"
        reason = responses[0].reason or ""
        assert "bad fields" in reason
        # The bad field name should appear, not the bad value
        assert "repo_url" in reason
        assert "not-a-github-url" not in reason

    async def test_dispatch_generic_exception_hides_details(self) -> None:
        """Generic exceptions in dispatch return only the type name over the wire."""
        with patch(
            "performer.main.handle_dispatch",
            new=AsyncMock(side_effect=RuntimeError("secret internal detail")),
        ):
            responses = await _run_loop_with([
                _make_line(
                    action="dispatch",
                    title="T",
                    repo_url="https://github.com/org/repo",
                    branch="feat/x",
                    github_token="tok",
                )
            ])
        assert responses[0].status == "error"
        reason = responses[0].reason or ""
        assert "RuntimeError" in reason
        assert "secret internal detail" not in reason

    async def test_status_raises_branch_conflict_returns_error(self) -> None:
        """BranchConflictError raised from handle_status is caught and returned as error."""
        from performer.workspace import BranchConflictError

        fresh_perf = MagicMock(session_id="sid", stand=MagicMock(), backend=MagicMock())
        fresh_perf.started_at = datetime.now(UTC)
        with patch(
            "performer.main.handle_dispatch",
            new=AsyncMock(return_value=(
                PerformerResponse(status="accepted", session_id="sid"),
                fresh_perf,
            )),
        ), patch(
            "performer.main.handle_status",
            new=AsyncMock(side_effect=BranchConflictError("conflict")),
        ):
            responses = await _run_loop_with([
                _make_line(action="dispatch", title="T", repo_url="https://github.com/org/repo",
                           branch="feat/x", github_token="tok"),
                _make_line(action="status", session_id="sid"),
            ])
        # Second response should be the error from BranchConflictError
        assert any(r.status == "error" for r in responses)

    async def test_relay_feedback_in_loop_returns_acknowledged(self) -> None:
        """relay_feedback action is routed correctly in run_loop."""
        mock_backend = MagicMock()
        mock_backend.stop = AsyncMock()
        mock_backend.relay_feedback = AsyncMock()

        perf = MagicMock()
        perf.session_id = "sid"
        perf.backend = mock_backend
        perf.stand = MagicMock()
        perf.started_at = datetime.now(UTC)  # fresh session — watchdog won't fire

        with (
            patch("performer.main.handle_dispatch", new=AsyncMock(return_value=(
                PerformerResponse(status="accepted", session_id="sid"),
                perf,
            ))),
            patch("performer.main.cleanup_stand"),
        ):
            responses = await _run_loop_with([
                _make_line(action="dispatch", title="T", repo_url="https://github.com/org/repo",
                           branch="feat/x", github_token="tok"),
                _make_line(action="relay_feedback", session_id="sid", feedback="looks good"),
                # Send an error to terminate the loop
                _make_line(action="status", session_id="wrong-sid"),
            ])
        feedback_resp = next((r for r in responses if r.status == "acknowledged"), None)
        assert feedback_resp is not None

    async def test_watchdog_fires_when_session_exceeds_timeout(self) -> None:
        """If the coordinare stops polling after dispatch, the watchdog terminates the loop."""
        mock_backend = MagicMock()
        mock_backend.stop = AsyncMock()

        perf = MagicMock()
        perf.session_id = "sid"
        perf.backend = mock_backend
        perf.stand = MagicMock()
        perf.state = "working"
        # Make started_at appear to be in the past so remaining budget is 0
        perf.started_at = datetime.now(UTC) - timedelta(hours=1)

        # Feed only the dispatch line — no status lines follow (coordinare stopped)
        reader = asyncio.StreamReader()
        reader.feed_data(_make_line(
            action="dispatch", title="T",
            repo_url="https://github.com/org/repo",
            branch="feat/x", github_token="tok",
        ))
        # No EOF — simulates coordinare going silent; watchdog should fire

        loop = asyncio.get_running_loop()
        s = _settings()

        with (
            patch("performer.main.asyncio.StreamReader", return_value=reader),
            patch("performer.main.asyncio.StreamReaderProtocol"),
            patch("performer.main.get_settings", return_value=s),
            patch.object(loop, "connect_read_pipe", AsyncMock()),
            patch("performer.main._write_response"),
            patch("performer.main.handle_dispatch", new=AsyncMock(return_value=(
                PerformerResponse(status="accepted", session_id="sid"),
                perf,
            ))),
            patch("performer.main.cleanup_stand"),
        ):
            await run_loop()

        # Watchdog should have called backend.stop() (cleanup also calls it, so ≥1)
        assert mock_backend.stop.called


# ---------------------------------------------------------------------------
# US5: CI check polling (T034-T039)
# ---------------------------------------------------------------------------


def _make_perf_waiting(session_id: str = "sid") -> Performance:
    """Return a Performance already in waiting_for_checks state with PR info set."""
    perf = _make_perf(session_id=session_id, state="waiting_for_checks")  # type: ignore[arg-type]
    perf.pr_url = "https://github.com/org/repo/pull/1"
    perf.pr_node_id = "PR_n1"
    perf.pr_head_sha = "abc123"
    perf.check_attempt = 0
    return perf


class TestCheckPolling:
    async def test_backend_done_transitions_to_waiting_for_checks(self) -> None:
        """When backend is done, performer pushes, opens PR, and returns working (not pr_opened)."""
        perf = _make_perf(session_id="sid")
        perf.backend.get_status.return_value = BackendStatus(state="done")
        with (
            patch("performer.main.push_branch", new=AsyncMock()),
            patch("performer.main.create_pull_request",
                  new=AsyncMock(return_value=("https://github.com/org/repo/pull/1", "PR_n1"))),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "working"
        assert perf.state == "waiting_for_checks"
        assert perf.pr_head_sha == "abc123"

    async def test_waiting_checks_all_pass_returns_pr_opened(self) -> None:
        perf = _make_perf_waiting()
        passing_runs = [{"name": "build", "status": "completed", "conclusion": "success"}]
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=passing_runs)):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "pr_opened"
        assert resp.pr_url == "https://github.com/org/repo/pull/1"
        assert resp.pr_node_id == "PR_n1"
        assert perf.state == "pr_opened"

    async def test_waiting_checks_pending_returns_working(self) -> None:
        perf = _make_perf_waiting()
        pending_runs = [{"name": "build", "status": "in_progress", "conclusion": None}]
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=pending_runs)):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "working"
        assert perf.state == "waiting_for_checks"

    async def test_waiting_checks_no_checks_returns_pr_opened(self) -> None:
        """Empty check run list (no CI configured) immediately reports pr_opened."""
        perf = _make_perf_waiting()
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=[])):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "pr_opened"

    async def test_waiting_checks_failure_relays_to_backend_and_increments_attempt(self) -> None:
        perf = _make_perf_waiting()
        failed_run = {
            "name": "tests",
            "status": "completed",
            "conclusion": "failure",
            "output": {"title": "3 failures", "summary": "Tests failed", "text": ""},
        }
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, CHECK_MAX_ATTEMPTS=3)
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=[failed_run])):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp.status == "working"
        assert perf.state == "working"
        assert perf.check_attempt == 1
        perf.backend.relay_feedback.assert_called_once()
        call_arg = perf.backend.relay_feedback.call_args[0][0]
        assert "tests" in call_arg
        assert "CI checks failed" in call_arg

    async def test_waiting_checks_failure_at_max_attempts_returns_blocked(self) -> None:
        perf = _make_perf_waiting()
        perf.check_attempt = 3
        failed_run = {
            "name": "lint",
            "status": "completed",
            "conclusion": "failure",
            "output": {},
        }
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, CHECK_MAX_ATTEMPTS=3)
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=[failed_run])):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp.status == "blocked"
        assert perf.state == "blocked"
        assert any("lint" in q for q in resp.questions)
        assert any("3" in q for q in resp.questions)

    async def test_waiting_checks_5xx_api_error_returns_working_retries(self) -> None:
        """Transient 5xx errors are retried — performer stays in waiting_for_checks."""
        perf = _make_perf_waiting()
        perf.check_attempt = 1
        with patch("performer.main.get_check_runs",
                   new=AsyncMock(side_effect=GitHubAPIError(503, "Service unavailable"))):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "working"
        assert perf.check_attempt == 1  # unchanged
        assert perf.state == "waiting_for_checks"

    async def test_waiting_checks_401_returns_error_immediately(self) -> None:
        """Deterministic 4xx errors (bad/empty token) surface as error state, not looping."""
        perf = _make_perf_waiting()
        with patch("performer.main.get_check_runs",
                   new=AsyncMock(side_effect=GitHubAPIError(401, "Bad credentials"))):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "error"
        assert "401" in (resp.reason or "") or "Bad credentials" in (resp.reason or "")
        assert perf.state == "error"

    async def test_waiting_checks_429_rate_limit_returns_working_retries(self) -> None:
        """429 rate-limit is transient — performer stays in waiting_for_checks and retries."""
        perf = _make_perf_waiting()
        with patch("performer.main.get_check_runs",
                   new=AsyncMock(side_effect=GitHubAPIError(429, "Rate limit exceeded"))):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "working"
        assert perf.state == "waiting_for_checks"

    async def test_waiting_checks_missing_pr_head_sha_returns_error(self) -> None:
        """If pr_head_sha is not set, poll_check_runs returns error immediately."""
        perf = _make_perf(session_id="sid", state="waiting_for_checks")  # type: ignore[arg-type]
        perf.pr_url = "https://github.com/org/repo/pull/1"
        perf.pr_node_id = "PR_n1"
        perf.pr_head_sha = None  # not set
        resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "error"
        assert perf.state == "error"

    async def test_blocked_state_stable_on_repeated_poll(self) -> None:
        """Repeated status polls after max-attempts blocked must NOT re-run push/PR-open.

        After _poll_check_runs sets perf.state = 'blocked', the next coordinare
        poll must return a stable 'blocked' response — not fall through to
        backend.get_status() → done → push/create PR again.
        """
        perf = _make_perf_waiting()
        perf.state = "blocked"  # type: ignore[assignment]
        perf.open_questions = ["CI checks failed after 3 fix attempt(s): lint"]
        mock_backend = MagicMock()
        perf.backend = mock_backend

        resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "blocked"
        assert resp.questions == ["CI checks failed after 3 fix attempt(s): lint"]
        # backend.get_status() must NOT have been called — no push/PR-open attempted
        mock_backend.get_status.assert_not_called()

    async def test_blocked_state_questions_persisted_on_perf(self) -> None:
        """Max-attempts blocked path stores questions on perf.open_questions."""
        perf = _make_perf_waiting()
        perf.check_attempt = 3
        failed_run = {
            "name": "test-suite",
            "status": "completed",
            "conclusion": "failure",
            "output": {},
        }
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, CHECK_MAX_ATTEMPTS=3)
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=[failed_run])):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp.status == "blocked"
        assert perf.open_questions == resp.questions
        assert any("test-suite" in q for q in perf.open_questions)


# ---------------------------------------------------------------------------
# 020 — Architect performer tests
# ---------------------------------------------------------------------------


class TestArchitectPerformer:
    """Tests for the architect role in handle_status and handle_dispatch (020)."""

    def _make_perf(self, role: str = "architecting") -> Performance:
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(
            title="Test card",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
        )
        return Performance(
            session_id="sid",
            stand=stand,
            score=score,
            backend=MagicMock(),
            role=role,
        )

    @pytest.mark.asyncio
    async def test_architect_produces_plan_committed(self) -> None:
        """Architect role returns plan_committed when backend is done."""
        perf = self._make_perf(role="architecting")
        perf.backend.get_status.return_value = BackendStatus(state="done", output="# Plan\n## Overview\n...")
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, PLAN_FILE_PATH="docs/coordinare-architecture.md")

        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "plan_committed"
        assert resp.plan_path == "docs/cards/test-card/plan.md"
        assert perf.state == "plan_committed"
        assert perf.plan_path == "docs/cards/test-card/plan.md"

    @pytest.mark.asyncio
    async def test_architect_commits_plan_to_correct_path(self) -> None:
        """commit_file is called with the issue-specific plan path."""
        perf = self._make_perf(role="architecting")
        perf.score.issue_number = 42
        perf.backend.get_status.return_value = BackendStatus(state="done", output="plan content")
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        mock_commit = AsyncMock()
        with patch("performer.main.commit_file", new=mock_commit):
            await handle_status(_msg("status", session_id="sid"), perf, settings)

        mock_commit.assert_called_once()
        call_args = mock_commit.call_args
        assert call_args[0][1] == "docs/cards/42-test-card/plan.md"
        assert call_args[0][2] == "plan content"

    @pytest.mark.asyncio
    async def test_implementer_still_produces_pr_opened(self) -> None:
        """Non-architect role follows the normal PR path (no regression)."""
        perf = self._make_perf(role="implementing")
        perf.backend.get_status.return_value = BackendStatus(state="done")

        with patch("performer.main.push_branch", new=AsyncMock()), \
             patch("performer.main.create_pull_request", new=AsyncMock(return_value=("http://pr", "PR_1"))), \
             patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "working"  # waiting_for_checks
        assert perf.state == "waiting_for_checks"

    @pytest.mark.asyncio
    async def test_plan_committed_is_terminal(self) -> None:
        """After plan_committed, subsequent status checks return the same state."""
        perf = self._make_perf(role="architecting")
        perf.state = "plan_committed"
        perf.plan_path = "docs/plan.md"

        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "plan_committed"
        assert resp.plan_path == "docs/plan.md"

    @pytest.mark.asyncio
    async def test_dispatch_reads_role_from_payload(self) -> None:
        """handle_dispatch sets perf.role from the dispatch payload."""
        msg = _msg(
            "dispatch",
            title="Test",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
            role="architecting",
        )

        mock_backend = MagicMock()
        mock_backend.start = AsyncMock()

        with patch("performer.main.clone_repository", new=AsyncMock(
            return_value=Stand(path=Path("/tmp/test"), branch="feat/test")
        )), patch("performer.main.get_backend", return_value=mock_backend):
            resp, perf = await handle_dispatch(msg, Settings(AGENT_BACKEND="opencode"))

        assert perf.role == "architecting"
        assert resp.status == "accepted"

    @pytest.mark.asyncio
    async def test_architect_empty_output_returns_error(self) -> None:
        """Architect backend done with empty output returns error status."""
        perf = self._make_perf(role="architecting")
        perf.backend.get_status.return_value = BackendStatus(state="done", output="")
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "error"
        assert "empty" in (resp.reason or "").lower()
        assert perf.state == "error"

    @pytest.mark.asyncio
    async def test_architect_none_output_returns_error(self) -> None:
        """Architect backend done with None output returns error status."""
        perf = self._make_perf(role="architecting")
        perf.backend.get_status.return_value = BackendStatus(state="done", output=None)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "error"
        assert "empty" in (resp.reason or "").lower()
        assert perf.state == "error"

    @pytest.mark.asyncio
    async def test_architect_commit_file_raises_propagates(self) -> None:
        """WorkspaceSetupError from commit_file propagates out of handle_status."""
        from performer.workspace import WorkspaceSetupError

        perf = self._make_perf(role="architecting")
        perf.backend.get_status.return_value = BackendStatus(state="done", output="# Real plan content")
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        with patch("performer.main.commit_file", new=AsyncMock(
            side_effect=WorkspaceSetupError("git push failed (exit 1): error")
        )):
            with pytest.raises(WorkspaceSetupError, match="git push failed"):
                await handle_status(_msg("status", session_id="sid"), perf, settings)

    @pytest.mark.asyncio
    async def test_dispatch_role_defaults_to_implementing(self) -> None:
        """Dispatch with no role in payload defaults perf.role to 'implementing'."""
        msg = _msg(
            "dispatch",
            title="Test",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
            # no role key
        )

        mock_backend = MagicMock()
        mock_backend.start = AsyncMock()

        with patch("performer.main.clone_repository", new=AsyncMock(
            return_value=Stand(path=Path("/tmp/test"), branch="feat/test")
        )), patch("performer.main.get_backend", return_value=mock_backend):
            resp, perf = await handle_dispatch(msg, Settings(AGENT_BACKEND="opencode"))

        assert perf.role == "implementing"
        assert resp.status == "accepted"

    @pytest.mark.asyncio
    async def test_dispatch_wires_github_api_url_to_settings(self) -> None:
        """handle_dispatch applies github_api_url from payload to settings (036)."""
        msg = _msg(
            "dispatch",
            title="Test",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
            github_api_url="https://ghes.acme.corp/api/v3",
        )

        mock_backend = MagicMock()
        mock_backend.start = AsyncMock()
        settings = Settings(AGENT_BACKEND="opencode")

        with patch("performer.main.clone_repository", new=AsyncMock(
            return_value=Stand(path=Path("/tmp/test"), branch="feat/test")
        )), patch("performer.main.get_backend", return_value=mock_backend):
            resp, _perf = await handle_dispatch(msg, settings)

        assert resp.status == "accepted"
        assert settings.GITHUB_API_URL == "https://ghes.acme.corp/api/v3"

    @pytest.mark.asyncio
    async def test_dispatch_strips_trailing_slash_from_github_api_url(self) -> None:
        """Trailing slash on github_api_url is stripped (036)."""
        msg = _msg(
            "dispatch",
            title="Test",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
            github_api_url="https://ghes.acme.corp/api/v3/",
        )

        mock_backend = MagicMock()
        mock_backend.start = AsyncMock()
        settings = Settings(AGENT_BACKEND="opencode")

        with patch("performer.main.clone_repository", new=AsyncMock(
            return_value=Stand(path=Path("/tmp/test"), branch="feat/test")
        )), patch("performer.main.get_backend", return_value=mock_backend):
            await handle_dispatch(msg, settings)

        assert settings.GITHUB_API_URL == "https://ghes.acme.corp/api/v3"

    @pytest.mark.asyncio
    async def test_dispatch_ignores_empty_github_api_url(self) -> None:
        """Empty github_api_url in payload does not override settings default (036)."""
        msg = _msg(
            "dispatch",
            title="Test",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
            github_api_url="",
        )

        mock_backend = MagicMock()
        mock_backend.start = AsyncMock()
        settings = Settings(AGENT_BACKEND="opencode")

        with patch("performer.main.clone_repository", new=AsyncMock(
            return_value=Stand(path=Path("/tmp/test"), branch="feat/test")
        )), patch("performer.main.get_backend", return_value=mock_backend):
            await handle_dispatch(msg, settings)

        assert settings.GITHUB_API_URL == "https://api.github.com"


# ---------------------------------------------------------------------------
# 021 — Reviewer performer tests
# ---------------------------------------------------------------------------


class TestReviewerPerformer:
    """Tests for the reviewer role in handle_status (021)."""

    def _make_perf(self, role: str = "reviewing", pr_url: str = "https://github.com/acme/repo/pull/42") -> Performance:
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(
            title="Test card",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
        )
        perf = Performance(
            session_id="sid",
            stand=stand,
            score=score,
            backend=MagicMock(),
            role=role,
        )
        perf.pr_url = pr_url
        return perf

    @pytest.mark.asyncio
    async def test_reviewer_approved_returns_approved_status(self) -> None:
        """Reviewer with approved output returns approved status."""
        import json
        perf = self._make_perf()
        output = json.dumps({"approved": True, "comments": [], "suggestions": ["Consider adding docstring"], "body": "LGTM"})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        with patch("performer.main.post_pull_request_review", new=AsyncMock(return_value={})):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "approved"
        assert resp.suggestions == ["Consider adding docstring"]
        assert perf.state == "approved"

    @pytest.mark.asyncio
    async def test_reviewer_posts_comment_review_to_github(self) -> None:
        """Reviewer always posts as COMMENT (human handles formal approval)."""
        import json
        perf = self._make_perf()
        output = json.dumps({"approved": True, "body": "Looks good"})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        mock_post = AsyncMock(return_value={})
        with patch("performer.main.post_pull_request_review", new=mock_post):
            await handle_status(_msg("status", session_id="sid"), perf, settings)

        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs.get("event") == "COMMENT"

    @pytest.mark.asyncio
    async def test_reviewer_approved_body_has_no_suggestions(self) -> None:
        """When approved with suggestions, the posted review body must NOT contain suggestions."""
        import json
        perf = self._make_perf()
        output = json.dumps({"approved": True, "body": "Looks good", "suggestions": ["do X"]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        mock_post = AsyncMock(return_value={})
        with patch("performer.main.post_pull_request_review", new=mock_post), \
             patch("performer.main.resolve_pr_review_threads", new=AsyncMock(return_value=0)):
            await handle_status(_msg("status", session_id="sid"), perf, settings)

        mock_post.assert_called_once()
        posted_body = mock_post.call_args.kwargs.get("body", "")
        assert "Suggestions" not in posted_body
        assert "non-blocking" not in posted_body
        assert posted_body == "**Bot Review: APPROVED**\n\nLooks good"

    @pytest.mark.asyncio
    async def test_reviewer_changes_requested_returns_comments(self) -> None:
        """Reviewer with changes returns changes_requested with comments."""
        import json
        perf = self._make_perf()
        comments = [{"file": "src/main.py", "line": 10, "body": "Missing null check"}]
        output = json.dumps({"approved": False, "comments": comments, "body": "Issues found"})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, REVIEWER_MAX_CYCLES=3)

        with patch("performer.main.post_pull_request_review", new=AsyncMock(return_value={})):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "changes_requested"
        assert len(resp.comments) == 1
        assert perf.state == "changes_requested"
        assert perf.review_cycle == 1

    @pytest.mark.asyncio
    async def test_reviewer_max_cycles_returns_blocked(self) -> None:
        """Reviewer returns blocked when max review cycles reached."""
        import json
        perf = self._make_perf()
        perf.review_cycle = 2  # already at limit - 1
        output = json.dumps({"approved": False, "comments": [{"file": "x.py", "line": 1, "body": "still broken"}]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, REVIEWER_MAX_CYCLES=3)

        with patch("performer.main.post_pull_request_review", new=AsyncMock(return_value={})):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "blocked"
        assert perf.state == "blocked"
        assert any("cycle limit" in q.lower() for q in resp.questions)

    @pytest.mark.asyncio
    async def test_reviewer_posts_comment_for_changes_requested(self) -> None:
        """Reviewer always posts as COMMENT even when requesting changes."""
        import json
        perf = self._make_perf()
        output = json.dumps({"approved": False, "comments": [{"file": "a.py", "line": 5, "body": "bug"}], "body": "Fix needed"})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, REVIEWER_MAX_CYCLES=3)

        mock_post = AsyncMock(return_value={})
        with patch("performer.main.post_pull_request_review", new=mock_post):
            await handle_status(_msg("status", session_id="sid"), perf, settings)

        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs.get("event") == "COMMENT"

    @pytest.mark.asyncio
    async def test_approved_is_terminal(self) -> None:
        """After approved, subsequent status polls return cached approved."""
        perf = self._make_perf()
        perf.state = "approved"

        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "approved"

    @pytest.mark.asyncio
    async def test_closing_review_role_uses_reviewer_path(self) -> None:
        """042: The 'closing_review' role flows through the same code as
        'reviewing' — same JSON parsing, same review post, same thread
        resolution on approval. Only the persona instructions differ."""
        import json
        perf = self._make_perf(role="closing_review")
        output = json.dumps({
            "approved": True,
            "body": "All prior threads addressed by subsequent commits.",
            "comments": [],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        mock_post = AsyncMock(return_value={})
        mock_resolve = AsyncMock(return_value=2)
        with patch("performer.main.post_pull_request_review", new=mock_post), \
             patch("performer.main.resolve_pr_review_threads", new=mock_resolve):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "approved"
        mock_post.assert_called_once()
        # Closer must resolve threads on approval — that's its core purpose.
        mock_resolve.assert_called_once()
        assert perf.state == "approved"

    @pytest.mark.asyncio
    async def test_closing_review_changes_requested_does_not_resolve_threads(self) -> None:
        """042: When the closer determines prior feedback is unresolved,
        it must NOT resolve threads — that would mask the unaddressed work."""
        import json
        perf = self._make_perf(role="closing_review")
        output = json.dumps({
            "approved": False,
            "comments": [{"file": "a.py", "line": 5, "body": "Copilot's concern about sizes still unaddressed"}],
            "body": "One prior thread remains unfixed.",
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, REVIEWER_MAX_CYCLES=3)

        mock_post = AsyncMock(return_value={})
        mock_resolve = AsyncMock(return_value=0)
        with patch("performer.main.post_pull_request_review", new=mock_post), \
             patch("performer.main.resolve_pr_review_threads", new=mock_resolve):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "changes_requested"
        mock_post.assert_called_once()
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_changes_requested_is_terminal(self) -> None:
        """After changes_requested, subsequent polls return cached result."""
        perf = self._make_perf()
        perf.state = "changes_requested"
        perf.review_comments = [{"file": "x.py", "line": 1, "body": "fix"}]

        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "changes_requested"
        assert len(resp.comments) == 1

    @pytest.mark.asyncio
    async def test_implementer_unaffected_by_reviewer(self) -> None:
        """Implementer role still follows the PR path (no regression)."""
        perf = self._make_perf(role="implementing")
        perf.backend.get_status.return_value = BackendStatus(state="done")

        with patch("performer.main.push_branch", new=AsyncMock()), \
             patch("performer.main.create_pull_request", new=AsyncMock(return_value=("http://pr", "PR_1"))), \
             patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "working"  # waiting_for_checks  (reviewer test)
        assert perf.state == "waiting_for_checks"


# ---------------------------------------------------------------------------
# 022 — Security performer tests
# ---------------------------------------------------------------------------


class TestSecurityPerformer:
    """Tests for the security role in handle_status (022)."""

    def _make_perf(self, pr_url: str = "https://github.com/acme/repo/pull/42") -> Performance:
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        perf = Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="security")
        perf.pr_url = pr_url
        return perf

    @pytest.mark.asyncio
    async def test_security_passed_no_blocking_findings(self) -> None:
        import json
        perf = self._make_perf()
        output = json.dumps({"findings": [{"severity": "low", "category": "style", "description": "minor", "routing": "implementer"}]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        with patch("performer.main.post_pr_comment", new=AsyncMock(return_value={})):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "security_passed"
        assert perf.state == "security_passed"

    @pytest.mark.asyncio
    async def test_security_failed_with_blocking_findings(self) -> None:
        import json
        perf = self._make_perf()
        findings = [{"severity": "critical", "category": "secret_leakage", "description": "hardcoded key", "file": "config.py", "line": 10, "routing": "implementer"}]
        output = json.dumps({"findings": findings})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, SECURITY_MAX_CYCLES=3)

        with patch("performer.main.post_pr_comment", new=AsyncMock(return_value={})):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "security_failed"
        assert len(resp.findings) == 1
        assert perf.security_cycle == 1

    @pytest.mark.asyncio
    async def test_security_max_cycles_returns_blocked(self) -> None:
        import json
        perf = self._make_perf()
        perf.security_cycle = 2
        findings = [{"severity": "high", "category": "injection", "description": "SQL injection", "routing": "implementer"}]
        output = json.dumps({"findings": findings})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, SECURITY_MAX_CYCLES=3)

        with patch("performer.main.post_pr_comment", new=AsyncMock(return_value={})):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "blocked"
        assert perf.state == "blocked"

    @pytest.mark.asyncio
    async def test_advisory_comments_posted(self) -> None:
        import json
        perf = self._make_perf()
        findings = [{"severity": "medium", "category": "insecure_default", "description": "debug mode", "routing": "implementer"}]
        output = json.dumps({"findings": findings})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        mock_comment = AsyncMock(return_value={})
        with patch("performer.main.post_pr_comment", new=mock_comment):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "security_passed"
        mock_comment.assert_called_once()

    @pytest.mark.asyncio
    async def test_security_passed_is_terminal(self) -> None:
        perf = self._make_perf()
        perf.state = "security_passed"
        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "security_passed"

    @pytest.mark.asyncio
    async def test_security_failed_is_terminal(self) -> None:
        perf = self._make_perf()
        perf.state = "security_failed"
        perf.security_findings = [{"severity": "high", "category": "xss"}]
        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "security_failed"
        assert len(resp.findings) == 1  # security terminal test


# ---------------------------------------------------------------------------
# 023 — QA performer tests
# ---------------------------------------------------------------------------


class TestQAPerformer:
    def _make_perf(self) -> Performance:
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="qa")

    @pytest.mark.asyncio
    async def test_qa_passed_no_failures(self) -> None:
        import json
        perf = self._make_perf()
        output = json.dumps({"failures": [], "criteria_checked": 5, "criteria_passed": 5})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"
        assert resp.report["criteria_checked"] == 5

    @pytest.mark.asyncio
    async def test_qa_failed_with_failures(self) -> None:
        import json
        perf = self._make_perf()
        failures = [{"criterion": "Login", "expected": "200", "actual": "500", "test": "test_login"}]
        output = json.dumps({"failures": failures})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode", QA_MAX_CYCLES=3))
        assert resp.status == "qa_failed"
        assert len(resp.failures) == 1
        assert perf.qa_cycle == 1

    @pytest.mark.asyncio
    async def test_qa_max_cycles_blocked(self) -> None:
        import json
        perf = self._make_perf()
        perf.qa_cycle = 2
        output = json.dumps({"failures": [{"criterion": "X", "expected": "Y", "actual": "Z", "test": "t"}]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode", QA_MAX_CYCLES=3))
        assert resp.status == "blocked"

    @pytest.mark.asyncio
    async def test_qa_commits_new_tests(self) -> None:
        import json
        perf = self._make_perf()
        output = json.dumps({"failures": [], "criteria_checked": 1, "criteria_passed": 1,
                             "new_test_files": [{"path": "tests/test_new.py", "content": "pass"}]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_commit = AsyncMock()
        with patch("performer.main.commit_file", new=mock_commit):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"
        # commit_file called twice: once for the test file, once for the QA report
        assert mock_commit.call_count == 2
        assert mock_commit.call_args_list[0][0][1] == "tests/test_new.py"
        assert "qa.md" in mock_commit.call_args_list[1][0][1]
        assert resp.report["new_tests_added"] == 1

    @pytest.mark.asyncio
    async def test_qa_environment_error_returns_blocked(self) -> None:
        """Environment failure returns blocked, not qa_failed."""
        import json
        perf = self._make_perf()
        output = json.dumps({"environment_error": "Missing runtime: node", "failures": []})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "blocked"
        assert "node" in resp.questions[0].lower()
        assert perf.state == "blocked"

    @pytest.mark.asyncio
    async def test_qa_commits_empty_content_test_file(self) -> None:
        """Test file with empty content string is still committed."""
        import json
        perf = self._make_perf()
        output = json.dumps({"failures": [], "criteria_checked": 1, "criteria_passed": 1,
                             "new_test_files": [{"path": "tests/test_empty.py", "content": ""}]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_commit = AsyncMock()
        with patch("performer.main.commit_file", new=mock_commit):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"
        # commit_file called twice: once for the test file, once for the QA report
        assert mock_commit.call_count == 2
        assert perf.qa_new_tests == ["tests/test_empty.py"]

    @pytest.mark.asyncio
    async def test_qa_passed_terminal_preserves_report(self) -> None:
        """Subsequent polls return the same report."""
        perf = self._make_perf()
        perf.state = "qa_passed"
        perf.qa_report = {"criteria_checked": 3, "criteria_passed": 3, "new_tests_added": 1}
        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"
        assert resp.report == {"criteria_checked": 3, "criteria_passed": 3, "new_tests_added": 1}

    @pytest.mark.asyncio
    async def test_qa_failed_is_terminal(self) -> None:
        perf = self._make_perf()
        perf.state = "qa_failed"
        perf.qa_failures = [{"criterion": "X"}]
        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_failed"


# ---------------------------------------------------------------------------
# 024 — Tech writer performer tests
# ---------------------------------------------------------------------------


class TestTechWriterPerformer:
    def _make_perf(self) -> Performance:
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="documenting")

    @pytest.mark.asyncio
    async def test_docs_committed_with_files(self) -> None:
        import json
        perf = self._make_perf()
        output = json.dumps({"files": [
            {"path": "CHANGELOG.md", "content": "## 1.0.0\n- New feature"},
            {"path": "README.md", "content": "# Updated README"},
        ]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_commit = AsyncMock()
        with patch("performer.main.commit_file", new=mock_commit):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "docs_committed"
        assert resp.files_modified == ["CHANGELOG.md", "README.md"]
        assert mock_commit.call_count == 2

    @pytest.mark.asyncio
    async def test_docs_committed_empty_diff(self) -> None:
        """Empty output → docs_committed with empty files_modified (FR-010)."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="")
        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "docs_committed"
        assert resp.files_modified == []

    @pytest.mark.asyncio
    async def test_docs_committed_is_terminal(self) -> None:
        perf = self._make_perf()
        perf.state = "docs_committed"
        perf.docs_files_modified = ["CHANGELOG.md"]
        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "docs_committed"
        assert resp.files_modified == ["CHANGELOG.md"]

    @pytest.mark.asyncio
    async def test_docs_commit_failure_returns_error(self) -> None:
        import json
        perf = self._make_perf()
        output = json.dumps({"files": [{"path": "README.md", "content": "new content"}]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_commit = AsyncMock(side_effect=Exception("git push failed"))
        with patch("performer.main.commit_file", new=mock_commit):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "error"
        assert "README.md" in (resp.reason or "")

    @pytest.mark.asyncio
    async def test_docs_malformed_files_skipped(self) -> None:
        """Malformed file entries are skipped; valid ones still committed."""
        import json
        perf = self._make_perf()
        output = json.dumps({"files": [
            "not a dict",
            {"path": "CHANGELOG.md", "content": "# Log"},
            {"path": "", "content": "no path"},
            {"no_path_key": True},
        ]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_commit = AsyncMock()
        with patch("performer.main.commit_file", new=mock_commit):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "docs_committed"
        assert resp.files_modified == ["CHANGELOG.md"]
        mock_commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_docs_idempotent_commit_still_reports_file(self) -> None:
        """When commit_file is a no-op (identical content), file is still reported as processed."""
        import json
        perf = self._make_perf()
        output = json.dumps({"files": [{"path": "README.md", "content": "same content"}]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        # commit_file succeeds but doesn't actually commit (no-op)
        mock_commit = AsyncMock()
        with patch("performer.main.commit_file", new=mock_commit):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "docs_committed"
        assert resp.files_modified == ["README.md"]  # still reported as processed


# ---------------------------------------------------------------------------
# Assessor role — handle_status for role="assessing"
# ---------------------------------------------------------------------------


class TestAssessorRole:
    """Tests for the assessor performer role in handle_status."""

    def _make_perf(self, *, state: str = "working") -> Performance:
        perf = _make_perf(session_id="sid", state=state)
        perf.role = "assessing"
        return perf

    @pytest.mark.asyncio
    async def test_sufficient_returns_assessment_complete(self) -> None:
        """When backend says sufficient=True, report assessment_complete."""
        import json
        perf = self._make_perf()
        output = json.dumps({"sufficient": True, "questions": []})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "assessment_complete"
        assert perf.state == "assessment_complete"

    @pytest.mark.asyncio
    async def test_insufficient_with_questions_returns_blocked(self) -> None:
        """When backend says sufficient=False with questions, report blocked."""
        import json
        perf = self._make_perf()
        output = json.dumps({"sufficient": False, "questions": ["Q1?", "Q2?"]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "blocked"
        assert resp.questions == ["Q1?", "Q2?"]
        assert perf.state == "blocked"

    @pytest.mark.asyncio
    async def test_insufficient_no_questions_treated_as_sufficient(self) -> None:
        """When backend says insufficient but no questions, treat as sufficient."""
        import json
        perf = self._make_perf()
        output = json.dumps({"sufficient": False, "questions": []})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "assessment_complete"
        assert perf.state == "assessment_complete"

    @pytest.mark.asyncio
    async def test_empty_output_returns_error(self) -> None:
        """Empty backend output returns error."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="")

        resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "error"
        assert "empty" in (resp.reason or "").lower()
        assert perf.state == "error"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self) -> None:
        """Invalid JSON backend output returns error."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="not json")

        resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "error"
        assert "invalid json" in (resp.reason or "").lower()
        assert perf.state == "error"

    @pytest.mark.asyncio
    async def test_non_object_json_returns_error(self) -> None:
        """JSON that's not an object returns error."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="[1,2,3]")

        resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "error"
        assert "not a json object" in (resp.reason or "").lower()
        assert perf.state == "error"

    @pytest.mark.asyncio
    async def test_stable_response_for_assessment_complete_state(self) -> None:
        """Re-polling after assessment_complete returns the same status."""
        perf = self._make_perf(state="assessment_complete")

        resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "assessment_complete"
        assert perf.state == "assessment_complete"

    @pytest.mark.asyncio
    async def test_default_sufficient_when_missing(self) -> None:
        """When 'sufficient' key is missing, defaults to True."""
        import json
        perf = self._make_perf()
        output = json.dumps({"questions": []})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "assessment_complete"


# ---------------------------------------------------------------------------
# _doc_folder unit tests
# ---------------------------------------------------------------------------


class TestDocFolder:
    def test_doc_folder_with_issue_number(self) -> None:
        score = Score(
            title="My Feature",
            issue_number=42,
            repo_url="https://github.com/org/repo",
            branch="main",
        )
        assert _doc_folder(score) == "docs/cards/42-my-feature"

    def test_doc_folder_empty_slug_fallback(self) -> None:
        score = Score(
            title="!!!",
            issue_number=0,
            repo_url="https://github.com/org/repo",
            branch="main",
        )
        assert _doc_folder(score) == "docs/cards/untitled"

    def test_doc_folder_long_title_truncated(self) -> None:
        score = Score(
            title="A very long title that exceeds twenty characters",
            issue_number=5,
            repo_url="https://github.com/org/repo",
            branch="main",
        )
        result = _doc_folder(score)
        # Folder format: docs/cards/{issue_number}-{slug}
        slug = result.split("/")[-1].split("-", 1)[1]  # remove issue number prefix
        assert len(slug) <= 20


# ---------------------------------------------------------------------------
# Architect — plan/tasks split
# ---------------------------------------------------------------------------


class TestArchitectSplitPlanAndTasks:
    @pytest.mark.asyncio
    async def test_architect_splits_plan_and_tasks(self) -> None:
        """When backend output contains ---TASKS--- separator, commit_file is called twice."""
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(
            title="Test card",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
            issue_number=10,
        )
        perf = Performance(
            session_id="sid",
            stand=stand,
            score=score,
            backend=MagicMock(),
            role="architecting",
        )
        backend_output = "# Plan\nThis is the plan content.\n---TASKS---\n# Tasks\n- [ ] Do thing"
        perf.backend.get_status.return_value = BackendStatus(state="done", output=backend_output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        mock_commit = AsyncMock()
        with patch("performer.main.commit_file", new=mock_commit):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "plan_committed"
        assert mock_commit.call_count == 2

        # First call: plan.md
        plan_path = mock_commit.call_args_list[0][0][1]
        plan_content = mock_commit.call_args_list[0][0][2]
        assert plan_path.endswith("plan.md")
        assert "---TASKS---" not in plan_content
        assert "# Plan" in plan_content

        # Second call: tasks.md
        tasks_path = mock_commit.call_args_list[1][0][1]
        tasks_content = mock_commit.call_args_list[1][0][2]
        assert tasks_path.endswith("tasks.md")
        assert "# Tasks" in tasks_content


# ---------------------------------------------------------------------------
# Assessor — commits assessment.md
# ---------------------------------------------------------------------------


class TestAssessorCommitsAssessment:
    @pytest.mark.asyncio
    async def test_assessor_commits_assessment(self) -> None:
        """When assessment is sufficient, commit_file is called with assessment.md."""
        perf = _make_perf(session_id="sid")
        perf.role = "assessing"
        output = json.dumps({"sufficient": True, "questions": []})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        mock_commit = AsyncMock()
        with patch("performer.main.commit_file", new=mock_commit):
            resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "assessment_complete"
        assert mock_commit.call_count == 1
        committed_path = mock_commit.call_args[0][1]
        assert "assessment.md" in committed_path


# ---------------------------------------------------------------------------
# Tech writer — extract JSON from prose with code fence
# ---------------------------------------------------------------------------


class TestTechWriterExtractJson:
    @pytest.mark.asyncio
    async def test_tech_writer_extract_json(self) -> None:
        """Backend output with prose and embedded JSON code block is parsed correctly."""
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(
            title="Test card",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
        )
        perf = Performance(
            session_id="sid",
            stand=stand,
            score=score,
            backend=MagicMock(),
            role="documenting",
        )
        backend_output = 'Here is the doc output:\n```json\n{"files": []}\n```'
        perf.backend.get_status.return_value = BackendStatus(state="done", output=backend_output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800)

        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "docs_committed"
