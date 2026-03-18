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
