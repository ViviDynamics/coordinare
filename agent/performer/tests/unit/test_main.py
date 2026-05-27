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
    _extract_pr_number,
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
# Helper parsing
# ---------------------------------------------------------------------------

class TestExtractPRNumber:
    def test_extracts_plain_pull_url(self) -> None:
        assert _extract_pr_number("https://github.com/acme/repo/pull/42") == 42

    def test_extracts_pull_number_from_suffix_path(self) -> None:
        assert _extract_pr_number("https://github.com/acme/repo/pull/42/files") == 42

    def test_extracts_pull_number_with_query_fragment(self) -> None:
        assert _extract_pr_number("https://github.com/acme/repo/pull/42?foo=bar#diff") == 42

    def test_returns_zero_when_missing_pull_segment(self) -> None:
        assert _extract_pr_number("https://github.com/acme/repo/issues/42") == 0


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
        assert resp.backend == "opencode"
        assert resp.model is None
        # SC-003: accepted within 5s
        assert elapsed < 5.0, f"dispatch acceptance took {elapsed:.3f}s — exceeds SC-003 budget"

    async def test_dispatch_returns_backend_and_model_overrides(self) -> None:
        msg = _msg(
            "dispatch",
            title="T",
            repo_url="https://github.com/org/repo",
            branch="feat/x",
            github_token="tok",
            backend="junie",
            model="junie-pro-1",
        )
        mock_backend = MagicMock()
        mock_backend.start = AsyncMock()
        with (
            patch("performer.main.clone_repository", new=AsyncMock(return_value=MagicMock())),
            patch("performer.main.get_backend", return_value=mock_backend),
        ):
            resp, _ = await handle_dispatch(msg, _settings("opencode"))

        assert resp.status == "accepted"
        assert resp.backend == "junie"
        assert resp.model == "junie-pro-1"

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


class TestEnvBootstrapInferenceTimeout:
    """Fix 2: _run_service_inference must be wrapped in asyncio.wait_for so a
    wedged LiteLLM proxy can't hang the env_bootstrap job indefinitely."""

    async def test_inference_timeout_returns_error_response(self) -> None:
        perf = _make_perf(session_id="sid")
        perf.role = "env_bootstrap"
        perf.score.env_cache_path = "/tmp/env-cache"
        perf.backend.get_status.return_value = BackendStatus(state="done")

        async def _hang(*_args: object, **_kwargs: object) -> dict:
            await asyncio.sleep(10)
            return {}

        settings = Settings(AGENT_BACKEND="opencode", SERVICE_INFERENCE_TIMEOUT=0)
        with (
            patch("performer.main._run_service_inference", new=AsyncMock(side_effect=_hang)),
            patch("performer.main.get_settings", return_value=settings),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "error"
        assert resp.inference_succeeded is False
        assert resp.inference_skipped_reason == "timeout"
        assert "service_inference_timeout" in (resp.reason or "")

    async def test_inference_success_when_under_timeout(self) -> None:
        perf = _make_perf(session_id="sid")
        perf.role = "env_bootstrap"
        perf.score.env_cache_path = "/tmp/env-cache"
        perf.backend.get_status.return_value = BackendStatus(state="done")

        inference_state = {"inference_succeeded": True}
        settings = Settings(AGENT_BACKEND="opencode", SERVICE_INFERENCE_TIMEOUT=60)
        with (
            patch(
                "performer.main._run_service_inference",
                new=AsyncMock(return_value=inference_state),
            ),
            patch("performer.main.get_settings", return_value=settings),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "env_bootstrap_complete"
        assert perf.state == "env_bootstrap_complete"


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

    def test_junie_returns_adapter(self) -> None:
        from performer.backends import get_backend
        from performer.backends.junie import JunieBackend
        adapter = get_backend("junie")
        assert isinstance(adapter, JunieBackend)

    def test_cursor_returns_adapter(self) -> None:
        from performer.backends import get_backend
        from performer.backends.cursor import CursorBackend
        adapter = get_backend("cursor")
        assert isinstance(adapter, CursorBackend)

    def test_unknown_raises_unsupported(self) -> None:
        from performer.backends import UnsupportedBackendError, get_backend
        with pytest.raises(UnsupportedBackendError, match="foobar"):
            get_backend("foobar")

    def test_missing_backend_class_raises_unsupported(self) -> None:
        from performer.backends import UnsupportedBackendError, get_backend
        with patch("performer.backends.import_module", return_value=object()):
            with pytest.raises(UnsupportedBackendError, match="misconfigured"):
                get_backend("opencode")

    def test_backend_import_error_raises_unsupported(self) -> None:
        from performer.backends import UnsupportedBackendError, get_backend
        with patch("performer.backends.import_module", side_effect=ImportError("boom")):
            with pytest.raises(UnsupportedBackendError, match="supported"):
                get_backend("opencode")

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

    async def test_status_message_refreshes_stand_git_env(self) -> None:
        """A status poll carrying github_token updates both score.github_token and stand.git_env.

        This covers the tech-writer (and architect/security/QA) 401 regression: those roles use
        stand.git_env for git credential injection.  The fix must propagate the rotated token into
        stand.git_env so that long-running sessions don't hit the old baked-in credentials.
        """
        import base64
        from performer.workspace import _git_credential_vars

        stand = Stand(path=Path("/tmp/x"), branch="feat/x")
        stand.git_env = _git_credential_vars("old-token")

        score = Score(
            title="T",
            repo_url="https://github.com/org/repo",
            branch="feat/x",
            github_token="old-token",
        )
        backend = MagicMock()
        backend.stop = AsyncMock()

        perf = Performance(session_id="sid", stand=stand, score=score, backend=backend)
        perf.state = "working"  # type: ignore[assignment]
        perf.started_at = datetime.now(UTC)

        with (
            patch("performer.main.handle_dispatch", new=AsyncMock(return_value=(
                PerformerResponse(status="accepted", session_id="sid"),
                perf,
            ))),
            patch("performer.main.handle_status", new=AsyncMock(
                return_value=PerformerResponse(status="error", session_id="sid"),
            )),
            patch("performer.main.cleanup_stand"),
        ):
            await _run_loop_with([
                _make_line(action="dispatch", title="T", repo_url="https://github.com/org/repo",
                           branch="feat/x", github_token="old-token"),
                _make_line(action="status", session_id="sid", payload={"github_token": "new-token"}),
            ])

        assert perf.score.github_token == "new-token"

        expected_header = "Authorization: Basic " + base64.b64encode(
            b"x-access-token:new-token"
        ).decode()
        assert perf.stand.git_env.get("GIT_CONFIG_VALUE_0") == expected_header

        old_header = "Authorization: Basic " + base64.b64encode(
            b"x-access-token:old-token"
        ).decode()
        assert perf.stand.git_env.get("GIT_CONFIG_VALUE_0") != old_header

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

    async def test_relay_message_includes_fetch_ci_log_hint(self) -> None:
        """065 Fix 15: relay points the model at performer-fetch-ci-log."""
        perf = _make_perf_waiting()
        failed_run = {
            "name": "Validate version",
            "status": "completed",
            "conclusion": "failure",
            "output": {"title": "version.json not bumped"},
        }
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, CHECK_MAX_ATTEMPTS=3)
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=[failed_run])):
            await handle_status(_msg("status", session_id="sid"), perf, settings)
        relay = perf.backend.relay_feedback.call_args[0][0]
        assert "performer-fetch-ci-log --check 'Validate version'" in relay
        assert "performer-fetch-ci-log --list" in relay

    async def test_relay_inlines_workflow_log_tail_when_output_text_empty(self) -> None:
        """071 T003: when output.text is empty, relay body includes fetched log tail."""
        perf = _make_perf_waiting()
        failed_run = {
            "id": 999111,
            "name": "lint",
            "status": "completed",
            "conclusion": "failure",
            "output": {"title": "ruff failed", "summary": "1 error", "text": ""},
        }
        settings = Settings(
            AGENT_BACKEND="opencode",
            AGENT_TIMEOUT=1800,
            CHECK_MAX_ATTEMPTS=3,
            CI_LOG_INLINE_MIN_OUTPUT_CHARS=200,
            CI_LOG_INLINE_MAX_CHARS=6000,
        )
        with (
            patch("performer.main.get_check_runs", new=AsyncMock(return_value=[failed_run])),
            patch(
                "performer.main.get_check_run_logs",
                new=AsyncMock(return_value="ERROR: src/x.py:1:1 E501 line too long"),
            ),
        ):
            await handle_status(_msg("status", session_id="sid"), perf, settings)
        relay = perf.backend.relay_feedback.call_args[0][0]
        assert "Log tail (job 999111" in relay
        assert "E501 line too long" in relay

    async def test_relay_falls_back_when_log_fetch_raises(self) -> None:
        """071 T004: get_check_run_logs raising → relay body falls back to base format."""
        perf = _make_perf_waiting()
        failed_run = {
            "id": 999222,
            "name": "tests",
            "status": "completed",
            "conclusion": "failure",
            "output": {"title": "3 failures", "summary": "Tests failed", "text": ""},
        }
        settings = Settings(
            AGENT_BACKEND="opencode",
            AGENT_TIMEOUT=1800,
            CHECK_MAX_ATTEMPTS=3,
            CI_LOG_INLINE_MIN_OUTPUT_CHARS=200,
            CI_LOG_INLINE_MAX_CHARS=6000,
        )
        with (
            patch("performer.main.get_check_runs", new=AsyncMock(return_value=[failed_run])),
            patch(
                "performer.main.get_check_run_logs",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp.status == "working"
        relay = perf.backend.relay_feedback.call_args[0][0]
        assert "Log tail" not in relay
        assert "tests" in relay
        assert "3 failures" in relay

    async def test_no_progress_streak_blocks_before_max_attempts(self) -> None:
        """065 Fix 14: same failure twice in a row blocks before CHECK_MAX_ATTEMPTS."""
        perf = _make_perf_waiting()
        failed_run = {
            "name": "lint",
            "status": "completed",
            "conclusion": "failure",
            "output": {"title": "1 ruff error"},
        }
        settings = Settings(
            AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800,
            CHECK_MAX_ATTEMPTS=8, CHECK_NO_PROGRESS_LIMIT=2,
        )
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=[failed_run])):
            # 1st identical failure — relays
            r1 = await handle_status(_msg("status", session_id="sid"), perf, settings)
            assert r1.status == "working"
            # poll-check sets state="working" after relay; flip back to waiting to re-enter
            perf.state = "waiting_for_checks"  # type: ignore[assignment]
            # 2nd identical — streak=1, still relays
            r2 = await handle_status(_msg("status", session_id="sid"), perf, settings)
            assert r2.status == "working"
            perf.state = "waiting_for_checks"  # type: ignore[assignment]
            # 3rd identical — streak=2 reaches limit, blocks
            r3 = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert r3.status == "blocked"
        assert any("no progress" in q for q in r3.questions)
        # bailed before max_attempts (check_attempt incremented only on relay turns)
        assert perf.check_attempt < settings.CHECK_MAX_ATTEMPTS

    async def test_no_progress_streak_resets_on_different_failure(self) -> None:
        """065 Fix 14: streak resets when the failure signature changes."""
        perf = _make_perf_waiting()
        settings = Settings(
            AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800,
            CHECK_MAX_ATTEMPTS=8, CHECK_NO_PROGRESS_LIMIT=2,
        )
        run_a = {"name": "lint", "status": "completed", "conclusion": "failure",
                 "output": {"title": "A"}}
        run_b = {"name": "lint", "status": "completed", "conclusion": "failure",
                 "output": {"title": "B"}}
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=[run_a])):
            await handle_status(_msg("status", session_id="sid"), perf, settings)
        perf.state = "waiting_for_checks"  # type: ignore[assignment]
        with patch("performer.main.get_check_runs", new=AsyncMock(return_value=[run_b])):
            await handle_status(_msg("status", session_id="sid"), perf, settings)
        # different signature → streak reset to 0
        assert perf.check_no_progress_streak == 0


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
    async def test_dispatch_normalizes_role_alias_to_stage(self) -> None:
        """Alias role names normalize to canonical stage names."""
        msg = _msg(
            "dispatch",
            title="Test",
            repo_url="https://github.com/acme/repo",
            branch="feat/test",
            role="reviewer",
        )

        mock_backend = MagicMock()
        mock_backend.start = AsyncMock()

        with patch("performer.main.clone_repository", new=AsyncMock(
            return_value=Stand(path=Path("/tmp/test"), branch="feat/test")
        )), patch("performer.main.get_backend", return_value=mock_backend):
            resp, perf = await handle_dispatch(msg, Settings(AGENT_BACKEND="opencode"))

        assert perf.role == "reviewing"
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
    async def test_architect_reads_workspace_plan_when_output_empty(self, tmp_path: Path) -> None:
        """Fix 13 (065): when backend output is empty but plan.md exists in
        the workspace (codex apply_patch path), read it back and succeed."""
        perf = self._make_perf(role="architecting")
        perf.stand.path = tmp_path
        perf.score.issue_number = 101
        # Pre-populate workspace as if the backend wrote via apply_patch
        folder = tmp_path / "docs" / "cards" / "101-test-card"
        folder.mkdir(parents=True)
        (folder / "plan.md").write_text("# Plan from workspace\n## Overview\n...")
        (folder / "tasks.md").write_text("- [ ] task one\n- [ ] task two")
        perf.backend.get_status.return_value = BackendStatus(state="done", output=None)
        settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

        mock_commit = AsyncMock()
        with patch("performer.main.commit_file", new=mock_commit):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "plan_committed"
        assert resp.plan_path == "docs/cards/101-test-card/plan.md"
        assert perf.state == "plan_committed"
        # Both plan.md and tasks.md should be committed
        assert mock_commit.call_count == 2
        plan_call, tasks_call = mock_commit.call_args_list
        assert plan_call[0][1] == "docs/cards/101-test-card/plan.md"
        assert plan_call[0][2] == "# Plan from workspace\n## Overview\n..."
        assert tasks_call[0][1] == "docs/cards/101-test-card/tasks.md"
        assert tasks_call[0][2] == "- [ ] task one\n- [ ] task two"

    @pytest.mark.asyncio
    async def test_architect_empty_output_and_no_workspace_plan_still_errors(self, tmp_path: Path) -> None:
        """Fix 13 (065) regression guard: empty output AND no workspace plan.md
        still returns the same error — workspace fallback must not mask the
        true-empty case."""
        perf = self._make_perf(role="architecting")
        perf.stand.path = tmp_path  # exists but contains no plan.md
        perf.backend.get_status.return_value = BackendStatus(state="done", output=None)
        settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

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
    async def test_reviewer_changes_requested_forwards_body(self) -> None:
        """065 Fix 4b: review_body is populated on PerformerResponse so the
        coordinare can relay closer/reviewer prose when there are no
        structured comments."""
        import json
        perf = self._make_perf()
        output = json.dumps({
            "approved": False,
            "comments": [],
            "body": "No actionable file-level issues, but PR scope is too broad.",
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, REVIEWER_MAX_CYCLES=3)

        with patch("performer.main.post_pull_request_review", new=AsyncMock(return_value={})):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "changes_requested"
        assert resp.body == "No actionable file-level issues, but PR scope is too broad."

    @pytest.mark.asyncio
    async def test_closing_review_uses_distinct_pr_review_header(self) -> None:
        """065 Fix 4d: closer rejection comment is headed 'Bot Closer Review'
        so PR readers can distinguish the merge-gating closer from the
        round-trip reviewer."""
        import json
        perf = self._make_perf(role="closing_review")
        output = json.dumps({
            "approved": False,
            "comments": [{"file": "a.py", "line": 5, "body": "still unresolved"}],
            "body": "Prior thread unaddressed.",
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, REVIEWER_MAX_CYCLES=3)

        mock_post = AsyncMock(return_value={})
        with patch("performer.main.post_pull_request_review", new=mock_post), \
             patch("performer.main.resolve_pr_review_threads", new=AsyncMock(return_value=0)):
            await handle_status(_msg("status", session_id="sid"), perf, settings)

        mock_post.assert_called_once()
        posted_body = mock_post.call_args.kwargs.get("body", "")
        assert posted_body.startswith("**Bot Closer Review:"), f"got: {posted_body!r}"

        # And confirm the reviewer role uses the original header
        perf_rev = self._make_perf(role="reviewing")
        perf_rev.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_post2 = AsyncMock(return_value={})
        with patch("performer.main.post_pull_request_review", new=mock_post2):
            await handle_status(_msg("status", session_id="sid"), perf_rev, settings)
        posted_body2 = mock_post2.call_args.kwargs.get("body", "")
        assert posted_body2.startswith("**Bot Review:"), f"got: {posted_body2!r}"

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
    async def test_advisory_comments_dedup_against_existing_pr_comments(self) -> None:
        # 065 Fix 3: when an [Advisory - Security] for the same OWASP class +
        # severity already exists on the PR, do not repost. Reproduces the
        # PR #133 oscillation (same A04/A01 advisory re-emitted every cycle).
        import json
        perf = self._make_perf()
        findings = [{
            "severity": "medium",
            "category": "OWASP A04: Insecure Design",
            "description": "lifecycle invariants slightly reworded each cycle",
            "routing": "implementer",
        }]
        output = json.dumps({"findings": findings})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        existing = [{"body": "[Advisory - Security] **OWASP A04 Insecure Design** (medium)\n\nprior cycle body"}]

        mock_post = AsyncMock(return_value={})
        mock_list = AsyncMock(return_value=existing)
        with patch("performer.main.post_pr_comment", new=mock_post), \
             patch("performer.main.list_pr_comments", new=mock_list):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "security_passed"
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_advisory_comments_posted_when_no_matching_existing(self) -> None:
        # Different OWASP class than any existing advisory → still posts.
        import json
        perf = self._make_perf()
        findings = [{
            "severity": "medium",
            "category": "OWASP A07: Identification and Authentication Failures",
            "description": "new finding",
            "routing": "implementer",
        }]
        output = json.dumps({"findings": findings})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        existing = [{"body": "[Advisory - Security] **OWASP A04 Insecure Design** (medium)\n\nprior"}]

        mock_post = AsyncMock(return_value={})
        mock_list = AsyncMock(return_value=existing)
        with patch("performer.main.post_pr_comment", new=mock_post), \
             patch("performer.main.list_pr_comments", new=mock_list):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "security_passed"
        mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_advisory_comments_intra_batch_dedup(self) -> None:
        # Two findings in the same batch with the same OWASP class + severity
        # → only one comment posted.
        import json
        perf = self._make_perf()
        findings = [
            {"severity": "medium", "category": "OWASP A04: Insecure Design", "description": "first wording", "routing": "implementer"},
            {"severity": "medium", "category": "OWASP A04 Insecure Design", "description": "second wording", "routing": "implementer"},
        ]
        output = json.dumps({"findings": findings})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        mock_post = AsyncMock(return_value={})
        mock_list = AsyncMock(return_value=[])
        with patch("performer.main.post_pr_comment", new=mock_post), \
             patch("performer.main.list_pr_comments", new=mock_list):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "security_passed"
        mock_post.assert_called_once()

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
    async def test_qa_posts_pr_comment_with_verification_and_evidence(self) -> None:
        import json
        perf = self._make_perf()
        perf.score.title = "Dashboard button visual polish"
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "visual_validation_required": True,
            "verification_steps": ["Open the blog post.", "Click copy and confirm clipboard text."],
            "visual_evidence": [
                {
                    "label": "After fix",
                    "kind": "screenshot",
                    "path_or_url": "https://example.com/after-fix.png",
                    "note": "Copy button visible with no extra padding.",
                },
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_commit = AsyncMock()
        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=mock_commit),
            patch("performer.main.post_pr_comment", new=mock_comment),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_passed"
        mock_comment.assert_called_once()
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "QA Evidence" in posted_body
        assert "Demo / Verification Steps" in posted_body
        assert "after-fix.png" in posted_body
        assert resp.report["verification_steps"] == [
            "Open the blog post.",
            "Click copy and confirm clipboard text.",
        ]

    @pytest.mark.asyncio
    async def test_qa_local_screenshot_uploaded_to_cdn_before_posting(self) -> None:
        """Bug 16.2: container-local screenshot paths must be uploaded and
        replaced with the CDN URL before the PR comment is posted, so the
        comment body never contains raw /tmp/... references."""
        import json
        perf = self._make_perf()
        perf.score.title = "Refactor token parsing"
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        perf.score.issue_number = 99
        output = json.dumps({
            "failures": [],
            "criteria_checked": 1,
            "criteria_passed": 1,
            "visual_validation_required": False,
            "verification_steps": ["Verify the screenshot."],
            "visual_evidence": [
                {
                    "label": "After fix",
                    "kind": "screenshot",
                    "path_or_url": "/tmp/screenshots/after.png",
                    "note": "Final state.",
                },
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        async def fake_resolve(evidence, **kwargs):
            return [
                {**ev, "path_or_url": "https://github.com/user-attachments/assets/xyz"}
                for ev in evidence
            ]

        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.post_pr_comment", new=mock_comment),
            patch("performer.main.post_issue_comment", new=AsyncMock(return_value={})),
            patch("performer.main.resolve_visual_evidence_urls", new=fake_resolve),
        ):
            await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert mock_comment.called
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "/tmp/screenshots/after.png" not in posted_body
        assert "https://github.com/user-attachments/assets/xyz" in posted_body
        # screenshot kind with http URL renders as embedded image markdown
        assert "![After fix](https://github.com/user-attachments/assets/xyz)" in posted_body

    @pytest.mark.asyncio
    async def test_qa_includes_visual_capture_setup_and_blockers_when_no_artifacts(self) -> None:
        import json
        perf = self._make_perf()
        perf.score.title = "Fix dashboard copy button visual behavior"
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "visual_validation_required": True,
            "verification_steps": ["Verify copy button behavior on blog post page."],
            "demo_setup_steps": [
                "Run bin/dev to start the Rails app.",
                "Seed sample post content with fenced code blocks.",
                "Open /blog/<slug> in a browser.",
            ],
            "visual_capture_commands": [
                "bundle exec playwright screenshot http://localhost:3000/blog/example tmp/qa/copy-button.png",
            ],
            "visual_capture_blockers": [
                "No headless browser screenshot tooling was configured in this runtime.",
            ],
            "visual_evidence": [],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_commit = AsyncMock()
        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=mock_commit),
            patch("performer.main.post_pr_comment", new=mock_comment),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_failed"
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "Visual Capture Setup Steps" in posted_body
        assert "Visual Capture Commands Attempted" in posted_body
        assert "Capture blockers" in posted_body
        assert "No headless browser screenshot tooling was configured" in posted_body
        assert "Remaining Failures" in posted_body
        qa_report_markdown = mock_commit.call_args_list[-1][0][2]
        assert "## Visual Capture Setup Steps" in qa_report_markdown
        assert "## Visual Capture Commands Attempted" in qa_report_markdown
        assert "### Capture blockers" in qa_report_markdown
        assert resp.failures[0]["criterion"] == "Visual evidence artifacts captured"

    @pytest.mark.asyncio
    async def test_qa_visual_required_fails_when_evidence_has_no_artifact_location(self) -> None:
        import json
        perf = self._make_perf()
        perf.score.title = "Dashboard UI polish for copy interactions"
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "visual_validation_required": True,
            "verification_steps": ["Open dashboard and validate copy button styling."],
            "visual_evidence": [
                {
                    "label": "Capture note",
                    "kind": "screenshot",
                    "note": "Tooling timed out before artifact upload completed.",
                },
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_failed"
        criteria = {failure["criterion"] for failure in resp.failures}
        assert "Visual evidence entries include artifact locations" in criteria
        assert "Visual evidence artifacts captured" in criteria

    @pytest.mark.asyncio
    async def test_qa_non_visual_ticket_can_pass_without_visual_artifacts(self) -> None:
        import json
        perf = self._make_perf()
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        perf.score.title = "Harden GitHub retry backoff"
        perf.score.description = "Improve retry handling for API outages."
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "visual_validation_required": False,
            "verification_steps": ["Run unit tests and verify backoff timings in logs."],
            "visual_evidence": [],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"

    @pytest.mark.asyncio
    async def test_qa_visual_keyword_matching_avoids_substring_false_positives(self) -> None:
        import json
        perf = self._make_perf()
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        perf.score.title = "Fix build pipeline bug"
        perf.score.description = "Harden build retries and logging for flaky CI runners."
        output = json.dumps({
            "failures": [],
            "criteria_checked": 1,
            "criteria_passed": 1,
            "verification_steps": ["Run CI and verify retries are bounded."],
            "visual_evidence": [],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"

    @pytest.mark.asyncio
    async def test_qa_posts_issue_comment_when_issue_number_present(self) -> None:
        import json
        perf = self._make_perf()
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        perf.score.issue_number = 89
        output = json.dumps({
            "failures": [],
            "criteria_checked": 1,
            "criteria_passed": 1,
            "verification_steps": ["Open page and verify copied text."],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        mock_commit = AsyncMock()
        mock_pr_comment = AsyncMock(return_value={})
        mock_issue_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=mock_commit),
            patch("performer.main.post_pr_comment", new=mock_pr_comment),
            patch("performer.main.post_issue_comment", new=mock_issue_comment),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_passed"
        mock_pr_comment.assert_called_once()
        mock_issue_comment.assert_called_once()
        assert mock_issue_comment.call_args.args[2] == 89
        issue_body = mock_issue_comment.call_args.kwargs["body"]
        assert "QA Evidence" in issue_body
        assert "Open page and verify copied text." in issue_body

    @pytest.mark.asyncio
    async def test_qa_bug_ticket_uses_fix_verification_label_and_default_steps(self) -> None:
        import json
        perf = self._make_perf()
        perf.score.title = "Fix API retry bug"
        perf.score.acceptance_criteria = ["Retries stop after configured max attempts."]
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        output = json.dumps({"failures": [], "criteria_checked": 1, "criteria_passed": 1})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        mock_commit = AsyncMock()
        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=mock_commit),
            patch("performer.main.post_pr_comment", new=mock_comment),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_passed"
        assert resp.report["verification_steps"][0].startswith("Verify acceptance criterion:")
        qa_report_markdown = mock_commit.call_args_list[-1][0][2]
        assert "## Verification Steps" in qa_report_markdown
        assert "Retries stop after configured max attempts." in qa_report_markdown
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "Fix Verification Steps" in posted_body

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

    # --- 054: QA freshness gate tests ---

    @pytest.mark.asyncio
    async def test_qa_freshness_up_to_date_passes(self) -> None:
        """054 T023: Branch includes latest main SHA → freshness check passes, qa_passed."""
        import json
        from unittest.mock import MagicMock

        perf = self._make_perf()
        perf.score.latest_main_sha = "abc1234567890000000000000000000000000000"
        output = json.dumps({"failures": [], "criteria_checked": 2, "criteria_passed": 2})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="head-sha")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_passed"
        assert resp.report is not None
        fc = resp.report.get("qa_freshness_check", {})
        assert fc.get("up_to_date") is True
        assert fc.get("latest_main_sha") == perf.score.latest_main_sha

    @pytest.mark.asyncio
    async def test_qa_freshness_behind_main_fails(self) -> None:
        """054 T023: Branch does not include latest main SHA → freshness fails, qa_failed."""
        import json
        from unittest.mock import MagicMock

        perf = self._make_perf()
        perf.score.latest_main_sha = "deadbeef0000000000000000000000000000000"
        output = json.dumps({"failures": [], "criteria_checked": 2, "criteria_passed": 2})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock(return_value=1)  # non-zero → not an ancestor

        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="head-sha")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_failed"
        assert resp.report is not None
        fc = resp.report.get("qa_freshness_check", {})
        assert fc.get("up_to_date") is False

    @pytest.mark.asyncio
    async def test_qa_freshness_indeterminate_fails(self) -> None:
        """054 T023: git subprocess raises → freshness is indeterminate, qa_failed."""
        import json

        perf = self._make_perf()
        perf.score.latest_main_sha = "cafebabe0000000000000000000000000000000"
        output = json.dumps({"failures": [], "criteria_checked": 2, "criteria_passed": 2})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="head-sha")),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("git not found"))),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_failed"
        assert resp.report is not None
        fc = resp.report.get("qa_freshness_check", {})
        assert fc.get("up_to_date") is None
        assert fc.get("detail") == "freshness_check_indeterminate"

    @pytest.mark.asyncio
    async def test_qa_no_freshness_check_when_no_main_sha(self) -> None:
        """054 T023: When latest_main_sha is empty, freshness is indeterminate (non-blocking)."""
        import json

        perf = self._make_perf()
        perf.score.latest_main_sha = ""  # not provided
        output = json.dumps({"failures": [], "criteria_checked": 2, "criteria_passed": 2})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        mock_subprocess = AsyncMock()
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="head-sha")),
            patch("asyncio.create_subprocess_exec", new=mock_subprocess),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        # No failure is added — QA still passes when coordinare hasn't provided a SHA.
        assert resp.status == "qa_passed"
        mock_subprocess.assert_not_called()
        # But the indeterminate block IS recorded in the report.
        freshness = (resp.report or {}).get("qa_freshness_check", {})
        assert freshness.get("up_to_date") is None
        assert freshness.get("detail") == "freshness_check_indeterminate"


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
        """044: Tech writer uses batch commit — single call for all files."""
        import json
        perf = self._make_perf()
        output = json.dumps({"files": [
            {"path": "CHANGELOG.md", "content": "## 1.0.0\n- New feature"},
            {"path": "README.md", "content": "# Updated README"},
        ]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_batch = AsyncMock(return_value=["CHANGELOG.md", "README.md"])
        with patch("performer.main.commit_files", new=mock_batch):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "docs_committed"
        assert resp.files_modified == ["CHANGELOG.md", "README.md"]
        mock_batch.assert_called_once()

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
        """044: Batch commit failure returns error with diagnostic."""
        import json
        perf = self._make_perf()
        output = json.dumps({"files": [{"path": "README.md", "content": "new content"}]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_batch = AsyncMock(side_effect=Exception("git push failed"))
        with patch("performer.main.commit_files", new=mock_batch):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "error"
        assert "batch-commit" in (resp.reason or "").lower() or "git push" in (resp.reason or "")

    @pytest.mark.asyncio
    async def test_docs_malformed_files_skipped(self) -> None:
        """044: Malformed file entries are filtered before batch commit."""
        import json
        perf = self._make_perf()
        output = json.dumps({"files": [
            "not a dict",
            {"path": "CHANGELOG.md", "content": "# Log"},
            {"path": "", "content": "no path"},
            {"no_path_key": True},
        ]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_batch = AsyncMock(return_value=["CHANGELOG.md"])
        with patch("performer.main.commit_files", new=mock_batch):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "docs_committed"
        assert resp.files_modified == ["CHANGELOG.md"]
        mock_batch.assert_called_once()
        # Only the valid file should be in the batch
        call_files = mock_batch.call_args[0][1]
        assert len(call_files) == 1
        assert call_files[0]["path"] == "CHANGELOG.md"

    @pytest.mark.asyncio
    async def test_docs_idempotent_commit_still_reports_file(self) -> None:
        """044: When batch commit returns empty (no changes), files list is empty."""
        import json
        perf = self._make_perf()
        output = json.dumps({"files": [{"path": "README.md", "content": "same content"}]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_batch = AsyncMock(return_value=[])  # no-op: no actual changes
        with patch("performer.main.commit_files", new=mock_batch):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "docs_committed"
        assert resp.files_modified == []


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
        """Empty backend output returns error when retry budget is 0."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="")
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, BACKEND_PARSE_RETRIES=0)

        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "error"
        assert "empty" in (resp.reason or "").lower()
        assert perf.state == "error"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self) -> None:
        """Invalid JSON backend output returns error when retry budget is 0."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="not json")
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, BACKEND_PARSE_RETRIES=0)

        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "error"
        assert "could not be parsed" in (resp.reason or "").lower()
        assert perf.state == "error"

    @pytest.mark.asyncio
    async def test_non_object_json_returns_error(self) -> None:
        """JSON that's not an object returns error when retry budget is 0."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="[1,2,3]")
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, BACKEND_PARSE_RETRIES=0)

        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "error"
        assert "could not be parsed" in (resp.reason or "").lower()
        assert perf.state == "error"

    @pytest.mark.asyncio
    async def test_invalid_json_retries_before_error(self) -> None:
        """045: With BACKEND_PARSE_RETRIES=1, a first parse failure schedules a
        backend re-run (status=working).  A second failure on the retry
        exhausts the budget and surfaces status=error with the output
        preview embedded in the reason.
        """
        from unittest.mock import AsyncMock

        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="prose, not json")
        perf.backend.start = AsyncMock()
        perf.backend.relay_feedback = AsyncMock()
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, BACKEND_PARSE_RETRIES=1)

        # First call: parse fails, JSON repair requested → working.
        resp1 = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp1.status == "working"
        assert perf.parse_retry_count == 1
        assert "requested JSON repair" in (resp1.progress or "")
        assert perf.backend.relay_feedback.await_count == 1
        assert perf.backend.start.await_count == 0

        # Second call: parse fails again, budget exhausted → error with preview.
        resp2 = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp2.status == "error"
        reason = resp2.reason or ""
        assert reason.startswith("BACKEND_FORMAT_ERROR:")
        assert "after 2 attempts" in reason
        assert "could not be parsed" in reason.lower()
        assert "prose, not json" in reason  # preview carried into reason
        assert perf.state == "error"

    @pytest.mark.asyncio
    async def test_invalid_json_then_valid_json_recovers(self) -> None:
        """JSON-required roles should recover without terminal error when retry output is valid."""
        import json

        perf = self._make_perf()
        perf.backend.get_status.side_effect = [
            BackendStatus(state="done", output="prose only"),
            BackendStatus(state="done", output=json.dumps({"sufficient": True, "questions": []})),
        ]
        perf.backend.relay_feedback = AsyncMock()
        perf.backend.start = AsyncMock()
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, BACKEND_PARSE_RETRIES=1)

        resp1 = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp1.status == "working"
        assert perf.backend.relay_feedback.await_count == 1
        assert perf.backend.start.await_count == 0

        resp2 = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp2.status == "assessment_complete"
        assert perf.state == "assessment_complete"

    @pytest.mark.asyncio
    async def test_invalid_json_redacts_secrets_in_preview_and_reason(self) -> None:
        """045 + Copilot round 4: the raw backend output is untrusted and may
        contain credentials.  Both the ``output_preview`` log field and the
        ``reason`` string embedded in ``PerformerResponse`` must be passed
        through ``_redact_secrets`` before surfacing anywhere — coordinare-side
        redaction is key-based only and would pass arbitrary text through.
        """
        from unittest.mock import AsyncMock

        # Use a concrete token that matches _SECRET_PATTERNS (classic PAT).
        fake_pat = "ghp_" + "A" * 36
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(
            state="done", output=f"oops, leaked {fake_pat} while parsing",
        )
        perf.backend.start = AsyncMock()
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, BACKEND_PARSE_RETRIES=0)

        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)
        reason = resp.reason or ""

        assert resp.status == "error"
        assert reason.startswith("BACKEND_FORMAT_ERROR:")
        assert fake_pat not in reason
        assert "[REDACTED]" in reason

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
