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
    _backfill_terminal_failure_reason,
    _doc_folder,
    _extract_pr_number,
    _safe_doc_page_path,
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
    wedged LiteLLM proxy can't hang the env_bootstrap job indefinitely.

    076 (live QA #150): a timeout is NON-FATAL — the dev-env install already
    ran into the mounted cache, and service_inference is best-effort.  A
    timeout must report terminal success (cache ready) with the inference
    flagged as skipped, NOT status=error (which caused infinite re-dispatch).
    """

    async def test_inference_timeout_is_non_fatal_and_marks_complete(self) -> None:
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

        # Bootstrap completes (cache becomes ready) despite the inference timeout.
        assert resp.status == "env_bootstrap_complete"
        assert perf.state == "env_bootstrap_complete"
        # Telemetry preserved so the dashboard shows inference as degraded.
        assert resp.inference_succeeded is False
        assert resp.inference_skipped_reason == "timeout"

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


class TestEnvBootstrapVerifyGate:
    """077 Tier 2: the bootstrap must confirm the install before reporting
    success. The agent writes verify.sh; the performer runs it and FAILS the
    bootstrap on a non-zero exit, so a silent install failure (the bug that left
    Chromium uninstalled while the bootstrap reported success) now triggers a
    retry instead of marking a broken cache ready.
    """

    async def test_verify_failure_fails_the_bootstrap(self, tmp_path) -> None:
        perf = _make_perf(session_id="sid")
        perf.role = "env_bootstrap"
        perf.score.env_cache_path = str(tmp_path)
        perf.backend.get_status.return_value = BackendStatus(state="done")
        (tmp_path / "verify.sh").write_text(
            "#!/bin/sh\necho 'chromium not found' >&2\nexit 1\n"
        )
        settings = Settings(AGENT_BACKEND="opencode", SERVICE_INFERENCE_TIMEOUT=60)
        with (
            patch("performer.main._run_service_inference", new=AsyncMock(return_value={})),
            patch("performer.main.get_settings", return_value=settings),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "error"
        assert "verif" in (resp.reason or "").lower()
        assert perf.state == "error"

    async def test_verify_pass_completes_the_bootstrap(self, tmp_path) -> None:
        perf = _make_perf(session_id="sid")
        perf.role = "env_bootstrap"
        perf.score.env_cache_path = str(tmp_path)
        perf.backend.get_status.return_value = BackendStatus(state="done")
        (tmp_path / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
        settings = Settings(AGENT_BACKEND="opencode", SERVICE_INFERENCE_TIMEOUT=60)
        with (
            patch(
                "performer.main._run_service_inference",
                new=AsyncMock(return_value={"inference_succeeded": True}),
            ),
            patch("performer.main.get_settings", return_value=settings),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "env_bootstrap_complete"
        assert perf.state == "env_bootstrap_complete"

    async def test_service_readiness_failure_fails_the_bootstrap(self, tmp_path) -> None:
        # 101: a required service that isn't connectable → bootstrap error, not complete.
        perf = _make_perf(session_id="sid")
        perf.role = "env_bootstrap"
        perf.score.env_cache_path = str(tmp_path)
        perf.backend.get_status.return_value = BackendStatus(state="done")
        (tmp_path / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
        settings = Settings(AGENT_BACKEND="opencode", SERVICE_INFERENCE_TIMEOUT=60)
        with (
            patch(
                "performer.main._run_service_inference",
                new=AsyncMock(return_value={"inference_succeeded": True}),
            ),
            patch(
                "performer.workspace.run_service_readiness",
                new=AsyncMock(return_value=(False, [{"service": "postgres", "reason": "not connectable"}])),
            ),
            patch("performer.main.get_settings", return_value=settings),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "error"
        assert "postgres" in (resp.reason or "") or "not ready" in (resp.reason or "").lower()
        assert perf.state == "error"

    async def test_service_readiness_ok_completes_the_bootstrap(self, tmp_path) -> None:
        # 101: required services connectable (or none declared) → completes as before.
        perf = _make_perf(session_id="sid")
        perf.role = "env_bootstrap"
        perf.score.env_cache_path = str(tmp_path)
        perf.backend.get_status.return_value = BackendStatus(state="done")
        (tmp_path / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
        settings = Settings(AGENT_BACKEND="opencode", SERVICE_INFERENCE_TIMEOUT=60)
        with (
            patch(
                "performer.main._run_service_inference",
                new=AsyncMock(return_value={"inference_succeeded": True}),
            ),
            patch(
                "performer.workspace.run_service_readiness",
                new=AsyncMock(return_value=(True, [])),
            ),
            patch("performer.main.get_settings", return_value=settings),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "env_bootstrap_complete"
        assert perf.state == "env_bootstrap_complete"

    async def test_coordinare_manages_services_false_skips_readiness_gate(self, tmp_path) -> None:
        # 116: when coordinare does NOT manage services, the performer owns env setup and
        # the 101 readiness gate is skipped entirely — run_service_readiness must NOT be
        # called, and bootstrap success is decided by the toolchain verify.sh.
        perf = _make_perf(session_id="sid")
        perf.role = "env_bootstrap"
        perf.score.env_cache_path = str(tmp_path)
        perf.score.coordinare_manages_services = False
        perf.backend.get_status.return_value = BackendStatus(state="done")
        (tmp_path / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
        settings = Settings(AGENT_BACKEND="opencode", SERVICE_INFERENCE_TIMEOUT=60)
        readiness = AsyncMock(return_value=(False, [{"service": "postgres", "reason": "x"}]))
        with (
            patch(
                "performer.main._run_service_inference",
                new=AsyncMock(return_value={"inference_succeeded": True}),
            ),
            patch("performer.workspace.run_service_readiness", new=readiness),
            patch("performer.main.get_settings", return_value=settings),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        # Gate skipped: the (failing) readiness mock was never invoked, yet bootstrap completes.
        readiness.assert_not_awaited()
        assert resp.status == "env_bootstrap_complete"
        assert perf.state == "env_bootstrap_complete"

    async def test_verify_missing_is_degraded_not_fatal(self, tmp_path) -> None:
        # No verify.sh written → legacy/degraded bootstrap: log + proceed, not fail.
        perf = _make_perf(session_id="sid")
        perf.role = "env_bootstrap"
        perf.score.env_cache_path = str(tmp_path)
        perf.backend.get_status.return_value = BackendStatus(state="done")
        settings = Settings(AGENT_BACKEND="opencode", SERVICE_INFERENCE_TIMEOUT=60)
        with (
            patch("performer.main._run_service_inference", new=AsyncMock(return_value={})),
            patch("performer.main.get_settings", return_value=settings),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "env_bootstrap_complete"

    async def test_readiness_runs_before_verify(self, tmp_path) -> None:
        """107/SC-001: declared services must be STARTED (run_service_readiness)
        BEFORE verify.sh runs, so verify's live pg_isready/PING probe (spec-093)
        observes a running service instead of failing on a not-yet-started one."""
        perf = _make_perf(session_id="sid")
        perf.role = "env_bootstrap"
        perf.score.env_cache_path = str(tmp_path)
        perf.backend.get_status.return_value = BackendStatus(state="done")
        (tmp_path / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
        settings = Settings(AGENT_BACKEND="opencode", SERVICE_INFERENCE_TIMEOUT=60)
        order: list[str] = []

        async def _readiness(*_a, **_k):
            order.append("readiness")
            return (True, [])

        async def _verify(*_a, **_k):
            order.append("verify")
            return (True, "")

        with (
            patch(
                "performer.main._run_service_inference",
                new=AsyncMock(return_value={"inference_succeeded": True}),
            ),
            patch("performer.workspace.run_service_readiness", new=_readiness),
            patch("performer.workspace.run_env_cache_verify", new=_verify),
            patch("performer.main.get_settings", return_value=settings),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "env_bootstrap_complete"
        assert order == ["readiness", "verify"], (
            "readiness (which starts services) must run before verify (which probes them)"
        )


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
        # 077: a standardized attribution header now precedes the verdict header.
        assert "🤖 **Reviewer**" in posted_body and "harness" in posted_body
        assert posted_body.endswith("**Bot Review: APPROVED**\n\nLooks good")

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
    async def test_reviewer_empty_rejection_retries_backend(self) -> None:
        """153: a parsed ``{"approved": false}`` with no comments and no body is
        not actionable — it must trigger a JSON-repair retry (status=working)
        against the same warm backend, NOT a contentless changes_requested that
        the coordinare can only re-review-then-block on."""
        import json
        perf = self._make_perf()
        perf.backend.relay_feedback = AsyncMock()
        output = json.dumps({"approved": False, "comments": [], "body": ""})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(
            AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800,
            REVIEWER_MAX_CYCLES=3, BACKEND_PARSE_RETRIES=1,
        )

        mock_post = AsyncMock(return_value={})
        with patch("performer.main.post_pull_request_review", new=mock_post):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        # Retry scheduled — backend nudged for a real verdict, nothing posted.
        assert resp.status == "working"
        assert perf.parse_retry_count == 1
        perf.backend.relay_feedback.assert_called_once()
        mock_post.assert_not_called()
        # NEVER auto-approve on ambiguity.
        assert perf.state != "approved"

    @pytest.mark.asyncio
    async def test_reviewer_empty_rejection_blocks_after_retries(self) -> None:
        """153: when the reviewer STILL rejects with no comments/body after the
        parse-retry budget is spent, block for operator triage with an explicit
        reason — never park the card on a contentless changes_requested and
        never auto-approve."""
        import json
        perf = self._make_perf()
        perf.backend.relay_feedback = AsyncMock()
        perf.parse_retry_count = 1  # budget already spent
        output = json.dumps({"approved": False, "comments": [], "body": "   "})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        settings = Settings(
            AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800,
            REVIEWER_MAX_CYCLES=3, BACKEND_PARSE_RETRIES=1,
        )

        mock_post = AsyncMock(return_value={})
        with patch("performer.main.post_pull_request_review", new=mock_post):
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "blocked"
        assert perf.state == "blocked"
        assert any("no actionable feedback" in q.lower() for q in resp.questions)
        mock_post.assert_not_called()

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
        # 077: persona tag precedes the header; the closer header + Closer persona must both appear.
        assert "**Bot Closer Review:" in posted_body, f"got: {posted_body!r}"
        assert "**Closer**" in posted_body

        # And confirm the reviewer role uses the original header + Reviewer persona
        perf_rev = self._make_perf(role="reviewing")
        perf_rev.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_post2 = AsyncMock(return_value={})
        with patch("performer.main.post_pull_request_review", new=mock_post2):
            await handle_status(_msg("status", session_id="sid"), perf_rev, settings)
        posted_body2 = mock_post2.call_args.kwargs.get("body", "")
        assert "**Bot Review:" in posted_body2, f"got: {posted_body2!r}"
        assert "**Reviewer**" in posted_body2

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
        output = json.dumps({
            "failures": [],
            "criteria_checked": 5,
            "criteria_passed": 5,
            "executed_checks": [
                {"command": "pytest -q", "exit_code": 0, "output": "5 passed"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"
        assert resp.report["criteria_checked"] == 5

    @pytest.mark.asyncio
    async def test_qa_unsubstantiated_pass_refused(self) -> None:
        """083: a claimed pass with positive criteria but ZERO execution evidence
        (no executed_checks, no committed tests, no visual proof) and no
        environment_error is the gpt-oss:120b rubber-stamp. It must be refused —
        not honored — and surface a non-environmental defect failure."""
        import json
        perf = self._make_perf()
        output = json.dumps({
            "failures": [],
            "criteria_checked": 10,
            "criteria_passed": 10,
            "verification_steps": ["Run the app and click the button."],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status in ("qa_failed", "blocked")
        assert resp.status != "qa_passed"

    @pytest.mark.asyncio
    async def test_qa_zero_criteria_pass_refused(self) -> None:
        """083 follow-up: a claimed pass that checked NOTHING (criteria_passed=0,
        criteria_checked=0), ran nothing (executed_checks=[]), committed no tests
        and is NOT environment-limited is the purest rubber-stamp — observed from
        local/qwen3-14b on the clean fixture. The original guard exempted
        criteria_passed<=0 ('asserted nothing positive') and let this slip through.
        A confident pass resting on zero observed evidence must be refused
        regardless of the criteria count."""
        import json
        perf = self._make_perf()
        output = json.dumps({
            "failures": [],
            "criteria_checked": 0,
            "criteria_passed": 0,
            "executed_checks": [],
            "verification_steps": ["Verify checkout_total behaves correctly for typical carts"],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status in ("qa_failed", "blocked")
        assert resp.status != "qa_passed"

    @pytest.mark.asyncio
    async def test_qa_pass_with_executed_checks_is_honored(self) -> None:
        """083: the SAME claimed pass becomes legitimate once the model supplies
        real execution evidence (a command + exit code)."""
        import json
        perf = self._make_perf()
        output = json.dumps({
            "failures": [],
            "criteria_checked": 10,
            "criteria_passed": 10,
            "executed_checks": [
                {"command": "pytest -q tests/", "exit_code": 0, "output": "10 passed"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"

    @pytest.mark.asyncio
    async def test_qa_env_limited_zero_evidence_is_env_blocked(self) -> None:
        """088 (supersedes the 083 env-limited exemption): 'couldn't verify' is
        honest, but it is NOT a pass. A claimed pass with zero execution
        evidence and a genuine environment_error is the PR #159 shape — it
        classifies as the terminal status qa_env_blocked, never qa_passed."""
        import json
        perf = self._make_perf()
        output = json.dumps({
            "failures": [],
            "criteria_checked": 4,
            "criteria_passed": 4,
            "environment_error": "could not connect to postgres at localhost:5432",
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_env_blocked"
        assert resp.report["env_limited"] is True
        assert resp.report["evidence_count"] == 0

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
            # 088 (FR-005): visual criteria need app-boot proof referenced in
            # executed_checks for the pass to stand.
            "executed_checks": [
                {"command": "curl -fsS http://localhost:3000/", "exit_code": 0, "output": "ok"},
            ],
            "app_boot_check": {"command": "curl -fsS http://localhost:3000/", "exit_code": 0},
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

        # 077 classified a missing-screenshot limitation as environmental →
        # advisory; 088 goes further: a pass claim with ZERO evidence behind it
        # under an environmental limit is qa_env_blocked (couldn't verify ≠
        # verified). The evidence comment still records the capture
        # steps/blockers for humans.
        assert resp.status == "qa_env_blocked"
        assert (resp.report or {}).get("env_limited") is True
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "Visual Capture Setup Steps" in posted_body
        assert "Visual Capture Commands Attempted" in posted_body
        assert "Capture blockers" in posted_body
        assert "No headless browser screenshot tooling was configured" in posted_body
        qa_report_markdown = next(
            c.args[2] for c in mock_commit.call_args_list
            if len(c.args) > 2 and isinstance(c.args[1], str) and c.args[1].endswith("qa.md")
        )
        assert "## Visual Capture Setup Steps" in qa_report_markdown
        assert "## Visual Capture Commands Attempted" in qa_report_markdown
        assert "### Capture blockers" in qa_report_markdown
        # The missing-visual-evidence failure is environmental → advisory, so it is
        # NOT returned as a blocking defect (resp.failures); it's recorded in the
        # report/comment for humans instead.
        assert not resp.failures

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
            "executed_checks": [{"command": "pytest -q", "exit_code": 0, "output": "2 passed"}],
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
            "executed_checks": [{"command": "pytest -q", "exit_code": 0, "output": "1 passed"}],
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
            "executed_checks": [{"command": "pytest -q", "exit_code": 0, "output": "1 passed"}],
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
        output = json.dumps({
            "failures": [], "criteria_checked": 1, "criteria_passed": 1,
            "executed_checks": [{"command": "pytest -q", "exit_code": 0, "output": "1 passed"}],
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
    async def test_qa_environment_error_is_advisory_when_evidence_backed(self) -> None:
        """077 (pipeline-tolerant): an environment_error with no real defects is
        ADVISORY — QA passes DEGRADED rather than blocking the lifecycle —
        088: PROVIDED the run still produced execution evidence. (The
        zero-evidence variant is qa_env_blocked; see
        test_qa_env_limited_zero_evidence_is_env_blocked.)"""
        import json
        perf = self._make_perf()
        output = json.dumps({
            "environment_error": "Missing runtime: node — frontend checks skipped",
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 1,
            "executed_checks": [
                {"command": "pytest -q tests/backend", "exit_code": 0, "output": "12 passed"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"
        assert perf.state == "qa_passed"
        assert (resp.report or {}).get("env_limited") is True

    def test_qa_natural_environmental_phrasings_classified_as_environmental(self) -> None:
        """077 regression: the verbatim 'couldn't check' phrasings real models emit
        (captured from PR ViviDynamics/website#159) MUST classify as environmental so
        they stay advisory instead of blocking the card. A FAILED verdict must mean
        'checked and broken', not 'couldn't check'."""
        from performer.main import _qa_failure_is_environmental
        environmental = [
            {"criterion": "Run rubocop", "actual": "Rubocop could not be executed (bundle missing)"},
            {"criterion": "Run rspec", "actual": "RSpec could not be executed (bundle missing)"},
            {"criterion": "Run full test suite", "expected": "All tests pass",
             "actual": "Tests fail to start due to missing libyaml/psych library"},
            {"criterion": "Verify",
             "actual": "Cannot verify automatically in this environment (Ruby and Docker are unavailable)"},
            {"criterion": "Lint", "actual": "Cannot run rubocop in this environment"},
            {"criterion": "Tests", "actual": "Cannot execute test suite here"},
            {"criterion": "Toolchain", "message": "Ruby and Docker are not available"},
        ]
        for f in environmental:
            assert _qa_failure_is_environmental(f) is True, f

    def test_qa_real_defect_not_misclassified_as_environmental(self) -> None:
        """The broadened environmental detection must NOT swallow genuine defects:
        a concrete assertion failure stays a blocking defect."""
        from performer.main import _qa_failure_is_environmental
        assert _qa_failure_is_environmental({
            "criterion": "margin is 0 on mobile", "expected": "0px",
            "actual": "16px still present", "message": "assertion failed: margin not removed",
        }) is False

    def test_has_local_visual_artifact_true_for_existing_file(self, tmp_path) -> None:
        """A local path the QA agent genuinely produced counts — no re-capture."""
        from performer.main import _has_local_visual_artifact
        f = tmp_path / "shot.png"
        f.write_bytes(b"\x89PNG" + b"x" * 32)
        assert _has_local_visual_artifact([
            {"label": "e", "kind": "screenshot", "path_or_url": str(f), "note": ""},
        ]) is True

    def test_has_local_visual_artifact_true_for_uploaded_url(self) -> None:
        """An already-uploaded URL counts — nothing to re-capture."""
        from performer.main import _has_local_visual_artifact
        assert _has_local_visual_artifact([
            {"label": "e", "kind": "screenshot",
             "path_or_url": "https://cdn.example.com/a.png", "note": ""},
        ]) is True

    def test_has_local_visual_artifact_false_for_claimed_absent_path(self) -> None:
        """A path the capture never created must NOT count — this is exactly the
        faked-screenshot case the deterministic backstop exists to cover."""
        from performer.main import _has_local_visual_artifact
        assert _has_local_visual_artifact([
            {"label": "e", "kind": "screenshot",
             "path_or_url": "/tmp/does-not-exist-qa.png", "note": ""},
        ]) is False
        assert _has_local_visual_artifact([]) is False

    @pytest.mark.asyncio
    async def test_qa_couldnt_check_failures_route_to_env_blocked_not_qa_failed(self) -> None:
        """End-to-end: a QA run whose failures are all 'couldn't check' (PR #159
        cycle D shape) must not loop the card into qa_failed — and since 088 it
        must not pass either: with zero execution evidence it is qa_env_blocked
        so the environment gets repaired instead of the code 'fixed'."""
        import json
        perf = self._make_perf()
        output = json.dumps({
            "environment_error": "Ruby and related tooling (bundle, rails) are not installed in the sandbox.",
            "failures": [
                {"criterion": "Run rubocop", "expected": "rubocop clean",
                 "actual": "Rubocop could not be executed (bundle missing)"},
                {"criterion": "Run rspec", "expected": "all specs pass",
                 "actual": "RSpec could not be executed (bundle missing)"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_env_blocked"
        assert perf.state == "qa_env_blocked"
        assert (resp.report or {}).get("env_limited") is True

    @pytest.mark.asyncio
    async def test_qa_services_start_failure_folds_into_env_blocked(self) -> None:
        """088 US6 (FR-013): a coordinare-recorded env-cache services-start
        failure (e.g. Postgres could not start without a password) must join the
        environment_error channel even when the agent's own QA JSON omits it.
        Otherwise a zero-evidence pass claim classifies as unsubstantiated FAILED
        (a code defect) instead of qa_env_blocked (a held environment blocker)."""
        import json
        perf = self._make_perf()
        # Agent self-reports a clean pass with NO environment_error and zero
        # execution evidence — the verdict hinges on whether the recorded
        # services-start failure reaches env_error.
        output = json.dumps({
            "failures": [],
            "criteria_checked": 3,
            "criteria_passed": 3,
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch(
                "performer.workspace.consume_services_start_failure",
                return_value=(
                    "env-cache services-start failed: services-start.sh "
                    "(returncode=1): could not create PostgreSQL test database "
                    "without a password"
                ),
            ),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_env_blocked"
        assert (resp.report or {}).get("env_limited") is True
        assert (resp.report or {}).get("evidence_count") == 0

    @pytest.mark.asyncio
    async def test_qa_unparseable_output_with_services_failure_is_env_blocked(self) -> None:
        """088 US6 (FR-013): a recorded services-start failure must classify
        qa_env_blocked even when the agent's output is unusable.

        The fold-in sits after the JSON parse, so this path missed it entirely.
        An agent that could not reach the database is MORE likely to emit
        degenerate output — and routing that to malformed_output loses the env
        blocker, skips the cache invalidation, and spends the bounded retries
        against the same broken cache.
        """
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(
            state="done", output="I could not run any tests. Sorry!"
        )
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch(
                "performer.workspace.consume_services_start_failure",
                return_value=(
                    "env-cache services-start failed: services-start.sh "
                    "(returncode=1): postgres could not start"
                ),
            ),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_env_blocked"
        assert "postgres could not start" in (resp.reason or "")
        assert (resp.report or {}).get("env_limited") is True

    @pytest.mark.asyncio
    async def test_qa_unparseable_output_without_services_failure_still_malformed(self) -> None:
        """The complement: with no recorded env blocker, unusable output must
        still take the malformed-output path (spec-119 bounded retry) — the new
        env branch must not swallow genuine backend format failures."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(
            state="done", output="I could not run any tests. Sorry!"
        )
        # The malformed path restarts the backend for its bounded retry.
        perf.backend.stop = AsyncMock()
        perf.backend.start = AsyncMock()
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status != "qa_env_blocked"
        # Restarting the backend is the malformed-output retry path (spec-119).
        perf.backend.start.assert_awaited()

    @pytest.mark.asyncio
    async def test_qa_real_defect_still_blocks(self) -> None:
        """A genuine (non-environmental) acceptance-criterion failure must still
        gate the lifecycle — tolerance applies ONLY to environmental limits."""
        import json
        perf = self._make_perf()
        output = json.dumps({"failures": [{
            "criterion": "margin is 0 on mobile", "expected": "0px",
            "actual": "16px still present", "message": "assertion failed: margin not removed",
        }]})
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status in ("qa_failed", "blocked")
        assert perf.state in ("qa_failed", "blocked")

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
        output = json.dumps({
            "failures": [], "criteria_checked": 2, "criteria_passed": 2,
            "executed_checks": [{"command": "pytest -q", "exit_code": 0, "output": "2 passed"}],
        })
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
        output = json.dumps({
            "failures": [], "criteria_checked": 2, "criteria_passed": 2,
            "executed_checks": [{"command": "pytest -q", "exit_code": 0, "output": "2 passed"}],
        })
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
# 088 US1 — QA verdict integrity (qa_env_blocked, evidence gate, app-boot proof,
# visual-evidence render filtering)
# ---------------------------------------------------------------------------


class TestQAVerdictIntegrity088:
    """A QA pass must always rest on evidence; env-limited zero-evidence pass
    claims become the terminal status ``qa_env_blocked`` (never an unflagged
    clean PASS — the website PR #159 false-pass)."""

    def _make_perf(self) -> Performance:
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="qa")

    # --- T002: PR #159 replay — the canonical SC-001 regression fixture ---

    @pytest.mark.asyncio
    async def test_qa_pr159_replay_env_blocked_never_passes(self) -> None:
        """SC-001: the exact PR #159 payload shape — criteria_passed=4 with ALL
        evidence channels empty and environment_error set — must classify as
        ``qa_env_blocked``, never ``qa_passed``, and the PR comment must lead
        with the blocker instead of an unflagged clean PASS."""
        perf = self._make_perf()
        perf.pr_url = "https://github.com/acme/repo/pull/159"
        output = json.dumps({
            "failures": [],
            "criteria_checked": 4,
            "criteria_passed": 4,
            "executed_checks": [],
            "new_test_files": [],
            "visual_evidence": [],
            "environment_error": (
                "Ruby toolchain unavailable: bundler/rails not on PATH in this container"
            ),
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.post_pr_comment", new=mock_comment),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_env_blocked"
        assert perf.state == "qa_env_blocked"
        assert "bundler/rails" in (resp.reason or "")
        assert (resp.report or {}).get("environment_error", "").startswith("Ruby toolchain")
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "ENVIRONMENT-BLOCKED" in posted_body
        assert "bundler/rails not on PATH" in posted_body
        assert "**Result:** PASSED" not in posted_body

    @pytest.mark.asyncio
    async def test_qa_env_blocked_is_terminal_on_subsequent_polls(self) -> None:
        perf = self._make_perf()
        perf.state = "qa_env_blocked"
        perf.qa_report = {"environment_error": "no toolchain"}
        resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_env_blocked"
        assert resp.report == {"environment_error": "no toolchain"}

    # --- T003: the evidence check always runs (no env_limited early-return) ---

    def test_unsubstantiated_pass_check_has_no_env_limited_exemption(self) -> None:
        """FR-001: a pass claim with zero evidence is unsubstantiated regardless
        of environment_error — the env_limited early-return is gone (the caller
        decides the *classification*, never the *exemption*)."""
        from performer.main import _qa_unsubstantiated_pass
        assert _qa_unsubstantiated_pass(
            qa_passed_flag=True,
            criteria_passed=4,
            executed_checks=[],
            new_tests=[],
            visual_evidence=[],
        ) is True

    # --- T004: cross-validation of claimed counts vs evidence ---

    @pytest.mark.asyncio
    async def test_qa_zero_evidence_no_env_error_refused_not_env_blocked(self) -> None:
        """Pass claim + zero evidence + NO environment_error stays the existing
        unsubstantiated refusal — qa_env_blocked is reserved for env-limited runs."""
        perf = self._make_perf()
        output = json.dumps({
            "failures": [],
            "criteria_checked": 3,
            "criteria_passed": 3,
            "executed_checks": [],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status in ("qa_failed", "blocked")
        assert resp.status != "qa_env_blocked"

    @pytest.mark.asyncio
    async def test_qa_pass_with_evidence_unchanged(self) -> None:
        """SC-006: an ordinary evidence-backed pass is unchanged."""
        perf = self._make_perf()
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "executed_checks": [
                {"command": "pytest -q", "exit_code": 0, "output": "2 passed"},
                {"command": "ruff check src", "exit_code": 0, "output": "clean"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"

    @pytest.mark.asyncio
    async def test_qa_divergent_criteria_counts_annotated_in_comment(self) -> None:
        """FR-007: criteria_passed greater than the evidence count renders the
        'N claimed / M evidence-backed' annotation in the PR comment."""
        perf = self._make_perf()
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        output = json.dumps({
            "failures": [],
            "criteria_checked": 4,
            "criteria_passed": 4,
            "executed_checks": [
                {"command": "pytest -q tests/a.py", "exit_code": 0, "output": "1 passed"},
                {"command": "pytest -q tests/b.py", "exit_code": 0, "output": "1 passed"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.post_pr_comment", new=mock_comment),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "4 claimed / 2 evidence-backed" in posted_body

    @pytest.mark.asyncio
    async def test_qa_matching_criteria_counts_not_annotated(self) -> None:
        """No divergence → the plain criteria line is unchanged (SC-006)."""
        perf = self._make_perf()
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "executed_checks": [
                {"command": "pytest -q tests/a.py", "exit_code": 0, "output": "1 passed"},
                {"command": "pytest -q tests/b.py", "exit_code": 0, "output": "1 passed"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.post_pr_comment", new=mock_comment),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "- Criteria passed: 2" in posted_body
        assert "evidence-backed" not in posted_body

    # --- T005: app-boot proof gates visual/UI criteria ---

    @pytest.mark.asyncio
    async def test_qa_visual_criteria_without_app_boot_proof_not_passed(self) -> None:
        """FR-005: a visual card whose only 'evidence' is a screenshot URL but
        no app-boot proof (no app_boot_check, no executed_checks) counts those
        criteria unverified — the pass claim is refused, not honored."""
        perf = self._make_perf()
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "visual_validation_required": True,
            "executed_checks": [],
            "visual_evidence": [
                {"label": "After", "kind": "screenshot",
                 "path_or_url": "https://example.com/after.png"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status != "qa_passed"

    @pytest.mark.asyncio
    async def test_qa_visual_criteria_with_app_boot_proof_passes(self) -> None:
        """The same visual card passes once app_boot_check references a
        zero-exit executed check (boot proof) alongside the screenshot."""
        perf = self._make_perf()
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "visual_validation_required": True,
            "executed_checks": [
                {"command": "curl -fsS http://localhost:3000/healthz", "exit_code": 0, "output": "ok"},
            ],
            "app_boot_check": {"command": "curl -fsS http://localhost:3000/healthz", "exit_code": 0},
            "visual_evidence": [
                {"label": "After", "kind": "screenshot",
                 "path_or_url": "https://example.com/after.png"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"

    @pytest.mark.asyncio
    async def test_qa_app_boot_check_must_reference_executed_checks(self) -> None:
        """An app_boot_check that does not correspond to any executed_checks
        entry is not boot proof — the visual pass claim is still refused."""
        perf = self._make_perf()
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "visual_validation_required": True,
            "executed_checks": [],
            "app_boot_check": {"command": "curl -fsS http://localhost:3000/healthz", "exit_code": 0},
            "visual_evidence": [
                {"label": "After", "kind": "screenshot",
                 "path_or_url": "https://example.com/after.png"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status != "qa_passed"

    @pytest.mark.asyncio
    async def test_qa_nonzero_app_boot_exit_discounts_visual_evidence(self) -> None:
        """A non-zero-exit boot check means the app never came up: the screenshot
        cannot back the visual criteria, so the claimed count is annotated down
        to the executed-check evidence only."""
        perf = self._make_perf()
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "visual_validation_required": True,
            "executed_checks": [
                {"command": "curl -fsS http://localhost:3000/healthz", "exit_code": 7, "output": "connection refused"},
            ],
            "app_boot_check": {"command": "curl -fsS http://localhost:3000/healthz", "exit_code": 7},
            "visual_evidence": [
                {"label": "After", "kind": "screenshot",
                 "path_or_url": "https://example.com/after.png"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.post_pr_comment", new=mock_comment),
        ):
            await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "2 claimed / 1 evidence-backed" in posted_body

    @pytest.mark.asyncio
    async def test_qa_null_app_boot_check_ignored_without_visual_criteria(self) -> None:
        """Cards without visual criteria are unaffected by a null app_boot_check."""
        perf = self._make_perf()
        perf.score.title = "Harden GitHub retry backoff"
        perf.score.description = "Improve retry handling for API outages."
        output = json.dumps({
            "failures": [],
            "criteria_checked": 1,
            "criteria_passed": 1,
            "visual_validation_required": False,
            "app_boot_check": None,
            "executed_checks": [{"command": "pytest -q", "exit_code": 0, "output": "1 passed"}],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))
        assert resp.status == "qa_passed"

    # --- T006: visual-evidence render filtering (FR-006) ---

    @pytest.mark.asyncio
    async def test_qa_unpublished_visual_evidence_moves_to_capture_blockers(self) -> None:
        """Only upload-validated (CDN URL) entries render as links; a local path
        whose upload failed moves to the capture-blockers list with the reason,
        never rendering a dead link."""
        perf = self._make_perf()
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        perf.score.issue_number = 99
        output = json.dumps({
            "failures": [],
            "criteria_checked": 2,
            "criteria_passed": 2,
            "visual_validation_required": False,
            "executed_checks": [
                {"command": "pytest -q", "exit_code": 0, "output": "2 passed"},
                {"command": "bin/screenshot", "exit_code": 0, "output": "saved"},
            ],
            "visual_evidence": [
                {"label": "Published", "kind": "screenshot",
                 "path_or_url": "https://github.com/user-attachments/assets/ok.png"},
                {"label": "Unpublished", "kind": "screenshot",
                 "path_or_url": "/tmp/screenshots/broken.png"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        async def fake_resolve(evidence, **kwargs):
            out = []
            for ev in evidence:
                ev2 = dict(ev)
                if ev2["path_or_url"].startswith("/tmp/"):
                    ev2["upload_error"] = "CDN upload failed: HTTP 403"
                out.append(ev2)
            return out

        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.post_pr_comment", new=mock_comment),
            patch("performer.main.post_issue_comment", new=AsyncMock(return_value={})),
            patch("performer.main.resolve_visual_evidence_urls", new=fake_resolve),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_passed"
        posted_body = mock_comment.call_args.kwargs["body"]
        # The published artifact renders as a link.
        assert "[https://github.com/user-attachments/assets/ok.png]" in posted_body
        # The unpublished one never renders as a link/image…
        assert "](/tmp/screenshots/broken.png)" not in posted_body
        assert "![Unpublished]" not in posted_body
        # …it is listed under capture blockers with the failure reason.
        assert "Capture blockers" in posted_body
        assert "/tmp/screenshots/broken.png" in posted_body
        assert "CDN upload failed: HTTP 403" in posted_body

    @pytest.mark.asyncio
    async def test_qa_all_uploads_failed_renders_no_links(self) -> None:
        """When every artifact failed to publish, the Visual Evidence section
        renders no links at all — only the capture-blockers list."""
        perf = self._make_perf()
        perf.pr_url = "https://github.com/acme/repo/pull/42"
        perf.score.issue_number = 99
        output = json.dumps({
            "failures": [],
            "criteria_checked": 1,
            "criteria_passed": 1,
            "visual_validation_required": False,
            "executed_checks": [{"command": "pytest -q", "exit_code": 0, "output": "1 passed"}],
            "visual_evidence": [
                {"label": "Only", "kind": "screenshot", "path_or_url": "/tmp/only.png"},
            ],
        })
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

        async def fake_resolve(evidence, **kwargs):
            return [dict(ev, upload_error="upload timed out") for ev in evidence]

        mock_comment = AsyncMock(return_value={})
        with (
            patch("performer.main.commit_file", new=AsyncMock()),
            patch("performer.main.post_pr_comment", new=mock_comment),
            patch("performer.main.post_issue_comment", new=AsyncMock(return_value={})),
            patch("performer.main.resolve_visual_evidence_urls", new=fake_resolve),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

        assert resp.status == "qa_passed"
        posted_body = mock_comment.call_args.kwargs["body"]
        assert "](/tmp/only.png)" not in posted_body
        assert "![Only]" not in posted_body
        assert "Capture blockers" in posted_body
        assert "upload timed out" in posted_body


# ---------------------------------------------------------------------------
# 024 — Tech writer performer tests
# ---------------------------------------------------------------------------


class TestTechWriterPerformer:
    """124 (C): the documenter runs a plan->write decomposition. The FIRST backend
    completion is a PLAN (pages to write/retire); each later completion writes ONE
    page; the queue drains into a single batch commit."""

    def _make_perf(self) -> Performance:
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(title="Test", repo_url="https://github.com/acme/repo",
                      branch="feat/test", role="tech_writer")
        backend = MagicMock()
        backend.start = AsyncMock()
        backend.relay_feedback = AsyncMock()
        backend.stop = AsyncMock()
        return Performance(session_id="sid", stand=stand, score=score, backend=backend, role="documenting")

    def _done(self, perf: Performance, output: str) -> None:
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)

    async def _status(self, perf: Performance):
        return await handle_status(_msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"))

    @pytest.mark.asyncio
    async def test_plan_phase_dispatches_first_write(self) -> None:
        """The plan completion queues pages, records deletions, and dispatches the
        first per-page WRITE (a fresh, diff-free write Score)."""
        import json
        perf = self._make_perf()
        self._done(perf, json.dumps({"pages": [
            {"path": "docs/wiki/README.md", "intent": "overview"},
            {"path": "docs/wiki/architecture.md", "intent": "modules"},
        ], "deletions": ["docs/wiki/old.md"]}))
        resp = await self._status(perf)
        assert resp.status == "working"
        assert perf.doc_phase == "writing"
        assert len(perf.doc_write_queue) == 2
        assert perf.doc_deletions == ["docs/wiki/old.md"]
        perf.backend.start.assert_awaited()
        write_score = perf.backend.start.call_args[0][1]
        assert write_score.doc_write_target["path"] == "docs/wiki/README.md"
        assert write_score.pr_diff == ""  # per-page context kept small

    def test_safe_doc_page_path_filter(self) -> None:
        """124 (review): page paths from the plan are untrusted — accept only
        docs/ + known root doc files; reject traversal/absolute/source paths."""
        for ok in ("docs/wiki/README.md", "docs/cards/1/x.md", "AGENTS.md",
                   "CLAUDE.md", "CHANGELOG.md", "README.md"):
            assert _safe_doc_page_path(ok), ok
        for bad in ("../../etc/passwd", "/etc/shadow", "docs/../src/x.py",
                    "src/app.py", "config/routes.rb", "", None, 123):
            assert not _safe_doc_page_path(bad), bad

    @pytest.mark.asyncio
    async def test_plan_drops_unsafe_page_paths(self) -> None:
        """124 (review, HIGH): the write queue drops traversal/absolute/non-doc
        page paths so a hallucinated path can't read or overwrite files outside
        the documenter's surface."""
        import json
        perf = self._make_perf()
        self._done(perf, json.dumps({"pages": [
            {"path": "../../etc/passwd", "intent": "evil"},
            {"path": "/etc/shadow", "intent": "evil"},
            {"path": "src/app.py", "intent": "not docs"},
            {"path": "docs/wiki/ok.md", "intent": "good"},
            {"path": "AGENTS.md", "intent": "pointer"},
        ], "deletions": []}))
        resp = await self._status(perf)
        assert resp.status == "working"
        assert [p["path"] for p in perf.doc_write_queue] == ["docs/wiki/ok.md", "AGENTS.md"]
        assert perf.backend.start.call_args[0][1].doc_write_target["path"] == "docs/wiki/ok.md"

    @pytest.mark.asyncio
    async def test_full_plan_write_commits_batch(self) -> None:
        """plan(1 page) -> write -> single batch commit -> docs_committed."""
        import json
        perf = self._make_perf()
        self._done(perf, json.dumps({"pages": [{"path": "docs/wiki/README.md", "intent": "x"}], "deletions": []}))
        mock_commit = AsyncMock(return_value=["docs/wiki/README.md"])
        with patch("performer.main.commit_files", new=mock_commit):
            r1 = await self._status(perf)
            assert r1.status == "working"
            self._done(perf, json.dumps({"files": [{"path": "docs/wiki/README.md", "content": "# Wiki"}]}))
            r2 = await self._status(perf)
        assert r2.status == "docs_committed"
        assert r2.files_modified == ["docs/wiki/README.md"]
        mock_commit.assert_awaited_once()
        committed_files = mock_commit.call_args[0][1]
        assert committed_files[0] == {"path": "docs/wiki/README.md", "content": "# Wiki"}

    @pytest.mark.asyncio
    async def test_empty_plan_commits_deletions_only(self) -> None:
        """A plan with no pages but a retirement commits the deletion, no writes."""
        import json
        perf = self._make_perf()
        self._done(perf, json.dumps({"pages": [], "deletions": ["docs/wiki/dead.md"]}))
        mock_commit = AsyncMock(return_value=["docs/wiki/dead.md"])
        with patch("performer.main.commit_files", new=mock_commit):
            resp = await self._status(perf)
        assert resp.status == "docs_committed"
        assert resp.files_modified == ["docs/wiki/dead.md"]
        perf.backend.start.assert_not_awaited()  # no page writes
        assert mock_commit.call_args.kwargs["deletions"] == ["docs/wiki/dead.md"]

    @pytest.mark.asyncio
    async def test_empty_plan_and_no_deletions_is_noop(self) -> None:
        """The significance gate at work: empty plan -> docs_committed, no commit."""
        import json
        perf = self._make_perf()
        self._done(perf, json.dumps({"pages": [], "deletions": []}))
        mock_commit = AsyncMock(return_value=[])
        with patch("performer.main.commit_files", new=mock_commit):
            resp = await self._status(perf)
        assert resp.status == "docs_committed"
        assert resp.files_modified == []
        mock_commit.assert_not_awaited()
        perf.backend.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_single_shot_files_commits_directly(self) -> None:
        """Backward-compat: a backend that emits the legacy {files} manifest (no
        "pages" key — e.g. a non-hermes documenter, or an old-image hermes) commits
        directly, NOT mis-read as an empty plan that silently writes nothing."""
        import json
        perf = self._make_perf()
        self._done(perf, json.dumps({"files": [
            {"path": "docs/wiki/README.md", "content": "# Wiki"},
            {"path": "CHANGELOG.md", "content": "## 1.0"},
        ], "deletions": ["docs/wiki/old.md"]}))
        mock_commit = AsyncMock(return_value=["docs/wiki/README.md", "CHANGELOG.md", "docs/wiki/old.md"])
        with patch("performer.main.commit_files", new=mock_commit):
            resp = await self._status(perf)
        assert resp.status == "docs_committed"
        perf.backend.start.assert_not_awaited()  # direct commit, no plan->write loop
        committed = mock_commit.call_args[0][1]
        assert {f["path"] for f in committed} == {"docs/wiki/README.md", "CHANGELOG.md"}
        assert mock_commit.call_args.kwargs["deletions"] == ["docs/wiki/old.md"]

    @pytest.mark.asyncio
    async def test_empty_output_docs_committed(self) -> None:
        """Empty output → docs_committed with empty files_modified (FR-010)."""
        perf = self._make_perf()
        self._done(perf, "")
        resp = await self._status(perf)
        assert resp.status == "docs_committed"
        assert resp.files_modified == []

    @pytest.mark.asyncio
    async def test_docs_committed_is_terminal(self) -> None:
        perf = self._make_perf()
        perf.state = "docs_committed"
        perf.docs_files_modified = ["docs/wiki/README.md"]
        resp = await self._status(perf)
        assert resp.status == "docs_committed"
        assert resp.files_modified == ["docs/wiki/README.md"]

    @pytest.mark.asyncio
    async def test_unparsed_write_page_skipped_but_others_commit(self) -> None:
        """A page whose write output can't be parsed is skipped; the rest commit."""
        import json
        perf = self._make_perf()
        self._done(perf, json.dumps({"pages": [
            {"path": "docs/wiki/a.md", "intent": "a"},
            {"path": "docs/wiki/b.md", "intent": "b"},
        ], "deletions": []}))
        mock_commit = AsyncMock(return_value=["docs/wiki/b.md"])
        with patch("performer.main.commit_files", new=mock_commit):
            await self._status(perf)  # plan -> dispatch write a
            self._done(perf, "not json — a verification report")  # page a unparsed
            r2 = await self._status(perf)  # skip a, dispatch write b
            assert r2.status == "working"
            self._done(perf, json.dumps({"files": [{"path": "docs/wiki/b.md", "content": "# B"}]}))
            r3 = await self._status(perf)  # b done -> commit
        assert r3.status == "docs_committed"
        committed = mock_commit.call_args[0][1]
        assert len(committed) == 1 and committed[0]["path"] == "docs/wiki/b.md"

    @pytest.mark.asyncio
    async def test_commit_failure_returns_error(self) -> None:
        import json
        perf = self._make_perf()
        self._done(perf, json.dumps({"pages": [{"path": "docs/wiki/x.md", "intent": "x"}], "deletions": []}))
        mock_commit = AsyncMock(side_effect=Exception("git push failed"))
        with patch("performer.main.commit_files", new=mock_commit):
            await self._status(perf)  # plan -> write
            self._done(perf, json.dumps({"files": [{"path": "docs/wiki/x.md", "content": "# X"}]}))
            resp = await self._status(perf)
        assert resp.status == "error"
        assert "batch-commit" in (resp.reason or "").lower() or "git push" in (resp.reason or "")

    @pytest.mark.asyncio
    async def test_init_mode_opens_seed_pr(self) -> None:
        """124(US2): a cardless init dispatch (doc_mode='init') opens a seed PR
        after committing and returns its node id for WikiInitService to auto-merge.
        A normal (update) run opens no PR."""
        import json
        perf = self._make_perf()
        perf.score.doc_mode = "init"
        self._done(perf, json.dumps({"pages": [{"path": "docs/wiki/README.md", "intent": "x"}], "deletions": []}))
        mock_commit = AsyncMock(return_value=["docs/wiki/README.md"])
        mock_pr = AsyncMock(return_value=("https://github.com/acme/repo/pull/7", "PR_node_7"))
        with patch("performer.main.commit_files", new=mock_commit), \
             patch("performer.main.create_pull_request", new=mock_pr):
            await self._status(perf)  # plan -> dispatch write
            self._done(perf, json.dumps({"files": [{"path": "docs/wiki/README.md", "content": "# W"}]}))
            resp = await self._status(perf)  # write done -> commit + open seed PR
        assert resp.status == "docs_committed"
        mock_pr.assert_awaited_once()
        assert resp.pr_url == "https://github.com/acme/repo/pull/7"
        assert resp.pr_node_id == "PR_node_7"

    @pytest.mark.asyncio
    async def test_update_mode_opens_no_pr(self) -> None:
        """The normal in-card documenting run (doc_mode='update', the default) must
        NOT open a PR — it commits into the card's existing PR."""
        import json
        perf = self._make_perf()  # default doc_mode == "update"
        self._done(perf, json.dumps({"pages": [{"path": "docs/wiki/README.md", "intent": "x"}], "deletions": []}))
        mock_commit = AsyncMock(return_value=["docs/wiki/README.md"])
        mock_pr = AsyncMock(return_value=("url", "node"))
        with patch("performer.main.commit_files", new=mock_commit), \
             patch("performer.main.create_pull_request", new=mock_pr):
            await self._status(perf)
            self._done(perf, json.dumps({"files": [{"path": "docs/wiki/README.md", "content": "# W"}]}))
            resp = await self._status(perf)
        assert resp.status == "docs_committed"
        mock_pr.assert_not_awaited()
        assert resp.pr_url is None

    @pytest.mark.asyncio
    async def test_write_dispatch_failure_returns_clean_error(self) -> None:
        """124 (review): if a per-page write dispatch (backend.start) raises, the
        job fails fast with a clean terminal error — not an unhandled exception
        out of handle_status that leaves perf half-set at doc_phase='writing'."""
        import json
        perf = self._make_perf()
        perf.backend.start = AsyncMock(side_effect=RuntimeError("backend down"))
        self._done(perf, json.dumps({"pages": [{"path": "docs/wiki/x.md", "intent": "x"}], "deletions": []}))
        resp = await self._status(perf)
        assert resp.status == "error"
        assert perf.state == "error"
        assert "dispatch" in (resp.reason or "").lower()
        assert "backend down" in (resp.reason or "")

    @pytest.mark.asyncio
    async def test_malformed_plan_does_not_crash(self) -> None:
        """A non-JSON plan routes through the parse-failure retry, never crashes."""
        perf = self._make_perf()
        self._done(perf, "this is a verification report, not JSON")
        resp = await self._status(perf)
        assert resp.status in ("working", "error")  # retried or bubbled, not a crash


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
    async def test_invalid_json_lenient_sufficient(self) -> None:
        """077: prose (non-JSON) assessor output, retries exhausted, falls back to
        sufficient rather than hard-erroring (a correct prose assessment must not
        park the card in the Blocked column)."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="not json")
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, BACKEND_PARSE_RETRIES=0)

        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "assessment_complete"
        assert perf.state == "assessment_complete"

    @pytest.mark.asyncio
    async def test_non_object_json_lenient_sufficient(self) -> None:
        """077: JSON that isn't an object also falls back to sufficient (lenient)."""
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output="[1,2,3]")
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, BACKEND_PARSE_RETRIES=0)

        resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        assert resp.status == "assessment_complete"
        assert perf.state == "assessment_complete"

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

        # Second call: parse fails again, budget exhausted. 077: the assessor now
        # falls back to sufficient (lenient) instead of a terminal format error —
        # but only AFTER the JSON-repair retry was attempted above.
        resp2 = await handle_status(_msg("status", session_id="sid"), perf, settings)
        assert resp2.status == "assessment_complete"
        assert perf.state == "assessment_complete"

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
    async def test_invalid_json_lenient_path_redacts_secrets(self) -> None:
        """045 + Copilot round 4 (077): the raw backend output is untrusted and may
        contain credentials. Redaction must hold on the NEW lenient path too — the
        secret must be scrubbed from the committed assessment (the repo is a
        surfaced artifact) and must never appear in the response. Coordinare-side
        redaction is key-based only and would pass arbitrary text through.
        """
        from unittest.mock import AsyncMock, patch

        # Use a concrete token that matches _SECRET_PATTERNS (classic PAT).
        fake_pat = "ghp_" + "A" * 36
        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(
            state="done", output=f"oops, leaked {fake_pat} while parsing",
        )
        perf.backend.start = AsyncMock()
        settings = Settings(AGENT_BACKEND="opencode", AGENT_TIMEOUT=1800, BACKEND_PARSE_RETRIES=0)

        with patch("performer.main.commit_file", new=AsyncMock()) as mock_commit:
            resp = await handle_status(_msg("status", session_id="sid"), perf, settings)

        # Lenient fallback taken (no terminal format error).
        assert resp.status == "assessment_complete"
        # The committed assessment content (3rd positional arg) is redacted.
        assert mock_commit.await_count == 1
        committed_content = mock_commit.await_args.args[2]
        assert fake_pat not in committed_content
        assert "[REDACTED]" in committed_content
        # And the secret never leaks into the response.
        assert fake_pat not in (resp.reason or "")

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


class TestPersonaTag:
    """077: bot comments must attribute which persona/backend/model produced them."""

    def test_persona_tag_includes_role_backend_model(self) -> None:
        from performer.main import _persona_tag
        from performer.models import Score

        score = Score(title="t", repo_url="https://github.com/o/r", branch="b", backend="opencode", model="gpt-oss:120b", role="qa")
        tag = _persona_tag(score)
        assert "QA" in tag and "opencode" in tag and "gpt-oss:120b" in tag

    def test_persona_tag_role_override_wins(self) -> None:
        from performer.main import _persona_tag
        from performer.models import Score

        # score.role stale ("implementing") but the running stage is closing_review.
        score = Score(title="t", repo_url="https://github.com/o/r", branch="b", backend="openclaw", model="m", role="implementing")
        assert "**Closer**" in _persona_tag(score, "closing_review")

    def test_persona_tag_unknown_role_passthrough(self) -> None:
        from performer.main import _persona_tag
        from performer.models import Score

        assert "weird" in _persona_tag(Score(title="t", repo_url="https://github.com/o/r", branch="b", role="weird"))


# ---------------------------------------------------------------------------
# 089 US1 — implementer local test gate
# ---------------------------------------------------------------------------


def _ci_run_result(
    success: bool,
    *,
    stdout: str = "",
    stderr: str = "",
    command: str = "pytest",
    duration: float = 1.5,
    timed_out: bool = False,
):
    """Build a CIRunResult for mocking run_command."""
    from performer.workspace import CIRunResult

    return CIRunResult(
        success=success,
        exit_code=-1 if timed_out else (0 if success else 1),
        stdout=stdout,
        stderr=stderr,
        command=command,
        duration_seconds=duration,
        timed_out=timed_out,
    )


def _detection(test_command: str | None):
    from coordinare.services.ci_detection import CIDetectionResult

    return CIDetectionResult(stack="python", lint_command=None, test_command=test_command)


@pytest.mark.asyncio
class TestRunTestCheck:
    async def test_standalone_import_error_passes_through(self) -> None:
        """T009: coordinare package absent → pass-through (passed=True, command=None)."""
        import sys

        from performer.main import _run_test_check

        with patch.dict(sys.modules, {"coordinare.services.ci_detection": None}):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is True
        assert result.command is None

    async def test_no_test_command_skips(self) -> None:
        """T009: detect().test_command is None → skip (passed=True, command=None)."""
        from performer.main import _run_test_check

        with patch("coordinare.services.ci_detection.detect", return_value=_detection(None)):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is True
        assert result.command is None

    async def test_green_run_reports_command_and_duration(self) -> None:
        """T010: green run → passed=True, command=<cmd>, duration>0."""
        from performer.main import _run_test_check

        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=AsyncMock(return_value=_ci_run_result(True, duration=2.0))),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is True
        assert result.command == "pytest"
        assert result.duration_seconds > 0
        assert result.env_blocked is False

    async def test_code_failure_no_env_signal(self) -> None:
        """T010: non-zero exit, no env signal → passed=False, env_blocked=False, output=<tail>."""
        from performer.main import _run_test_check

        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch(
                "performer.main.run_command",
                new=AsyncMock(return_value=_ci_run_result(False, stdout="3 failed", stderr="AssertionError")),
            ),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert result.env_blocked is False
        assert "AssertionError" in result.output or "3 failed" in result.output

    async def test_env_signature_in_output_classifies_env_blocked(self) -> None:
        """No spec-088 signal, but the failure output carries an environment
        signature (refused socket) → env_blocked=True, not a code defect."""
        from performer.main import _run_test_check

        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch(
                "performer.main.run_command",
                new=AsyncMock(
                    return_value=_ci_run_result(
                        False, stderr="ConnectionError: Connection refused (localhost:5432)"
                    )
                ),
            ),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert result.env_blocked is True
        assert result.env_reason is not None
        assert "connection refused" in result.env_reason

    async def test_ambiguous_signature_not_env_blocked(self) -> None:
        """ModuleNotFoundError is a plausible code defect (forgotten dependency in
        the diff) — it MUST NOT be classified as env_blocked, or a real bug is
        masked.  Both attempts fail with it → code failure."""
        from performer.main import _run_test_check

        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch(
                "performer.main.run_command",
                new=AsyncMock(
                    return_value=_ci_run_result(
                        False, stderr="ModuleNotFoundError: No module named 'foo'"
                    )
                ),
            ),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert result.env_blocked is False

    async def test_flake_recovers_on_retry(self) -> None:
        """First run red (code-shaped), retry green → passed=True (a single flaky
        failure does not cost a self-fix cycle)."""
        from performer.main import _run_test_check

        run_command = AsyncMock(
            side_effect=[
                _ci_run_result(False, stdout="1 failed", duration=2.0),
                _ci_run_result(True, duration=2.0),
            ]
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is True
        assert run_command.await_count == 2

    async def test_code_failure_confirmed_after_retry(self) -> None:
        """Both attempts red with code-shaped output → passed=False, env_blocked=False."""
        from performer.main import _run_test_check

        run_command = AsyncMock(
            side_effect=[
                _ci_run_result(False, stdout="1 failed: assert 1 == 2"),
                _ci_run_result(False, stdout="1 failed: assert 1 == 2"),
            ]
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert result.env_blocked is False
        assert run_command.await_count == 2
        assert result.exit_code == 1

    async def test_env_signature_surfaces_only_on_retry(self) -> None:
        """First attempt code-shaped, retry surfaces an env signature → env_blocked."""
        from performer.main import _run_test_check

        run_command = AsyncMock(
            side_effect=[
                _ci_run_result(False, stdout="1 failed"),
                _ci_run_result(False, stderr="OSError: [Errno 98] Address already in use"),
            ]
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert result.env_blocked is True
        assert "address already in use" in (result.env_reason or "")

    async def test_env_signal_short_circuits_before_retry(self) -> None:
        """A spec-088 services-start signal env-blocks immediately — no retry."""
        from performer.main import _run_test_check

        run_command = AsyncMock(return_value=_ci_run_result(False, stdout="1 failed"))
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch(
                "performer.workspace.consume_services_start_failure",
                return_value="postgres failed to start",
            ),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.env_blocked is True
        assert result.env_reason == "postgres failed to start"
        assert run_command.await_count == 1

    async def test_exit_code_propagated_on_green(self) -> None:
        from performer.main import _run_test_check

        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch(
                "performer.main.run_command",
                new=AsyncMock(return_value=_ci_run_result(True)),
            ),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.exit_code == 0

    async def test_real_failure_with_env_phrase_in_assertion_not_env_blocked(self) -> None:
        """A red test whose assertion text embeds an env-shaped phrase is a CODE
        defect, not an environment block.

        This repo's own suite asserts on the literal "connection refused" in 20
        files (e.g. ``assert "connection refused" in
        result["system_error_reason"]``), so a genuine failure of one of those
        tests prints the phrase in pytest's assertion diff.  A naive substring
        match would hold the card as env_blocked and never hand the real red
        test back to the implementer — the exact inverse of the bug this gate
        was hardened to fix.
        """
        from performer.main import _run_test_check

        failure = (
            "=================================== FAILURES ==========================\n"
            "_______________________ test_monitor_reports_reason ___________________\n"
            'E       assert "connection refused" in result["system_error_reason"]\n'
            "E       AssertionError\n"
            "========================= 1 failed, 402 passed in 12.3s ===============\n"
        )
        run_command = AsyncMock(return_value=_ci_run_result(False, stdout=failure))
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert result.env_blocked is False, (
            "a runner-reported test failure must outrank an env signature found "
            "in the same output"
        )
        assert result.env_reason is None

    async def test_env_signature_still_wins_on_collection_error(self) -> None:
        """The complement: when the suite ERRORS out before running tests (no
        ``N failed``), the env signature must still classify env_blocked — a
        fixture that cannot reach Postgres is the case this gate exists for."""
        from performer.main import _run_test_check

        failure = (
            "ERROR tests/conftest.py::db - OperationalError: connection refused\n"
            "========================= 1 error in 0.42s ===========================\n"
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch(
                "performer.main.run_command",
                new=AsyncMock(return_value=_ci_run_result(False, stdout=failure)),
            ),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.env_blocked is True
        assert "connection refused" in (result.env_reason or "")

    async def test_env_blocked_reason_carries_output_evidence(self) -> None:
        """A signature-classified env block sends the card to a human HOLD, so the
        reason must carry the output that triggered the (heuristic) call — naming
        only the matched phrase leaves the operator unable to judge it without
        digging out the performer log."""
        from performer.main import _run_test_check

        failure = (
            "ERROR tests/conftest.py::db\n"
            "psycopg.OperationalError: connection refused: host=localhost port=5432\n"
            "1 error in 0.42s\n"
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch(
                "performer.main.run_command",
                new=AsyncMock(return_value=_ci_run_result(False, stdout=failure)),
            ),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.env_blocked is True
        reason = result.env_reason or ""
        assert "connection refused" in reason
        assert "port=5432" in reason, "the triggering output must reach the operator"
        assert len(reason) < 700, "reason travels into notifications — keep it bounded"

    async def test_env_signature_precedence_across_runners(self) -> None:
        """The failure-vs-environment split must hold for every runner
        ci_detection can hand us (pytest, jest/mocha, rspec, minitest via
        ``bundle exec rake test``, and make-wrapped maven/gradle).

        Minitest was the concrete gap: it reports "1 failures" (plural) and
        "1) Failure:" (singular header), so a count pattern covering only
        "failed"/"failing" let a red Ruby test whose output mentions Postgres
        be misclassified as env_blocked.
        """
        from performer.main import _TEST_FAILURE_MARKERS, _match_env_signature

        red_tests = {
            "rspec": (
                "Failures:\n  1) Api connects\n     Failure/Error: "
                "expect(e).to eq 'connection refused'\n\n3 examples, 1 failure\n"
            ),
            "minitest": (
                "  1) Failure:\nFooTest#test_x [test/foo.rb:12]:\n"
                "Expected 'connection refused' to equal 'ok'.\n\n"
                "5 runs, 6 assertions, 1 failures, 0 errors, 0 skips\n"
            ),
            "jest": (
                "Tests:       1 failed, 4 passed, 5 total\n"
                "  expect(msg).toBe('connection refused')\n"
            ),
            "pytest": (
                "E       assert 'connection refused' in reason\n"
                "===== 1 failed, 402 passed =====\n"
            ),
            "maven": "Tests run: 5, Failures: 1, Errors: 0\n  expected 'connection refused'\n",
        }
        for runner, output in red_tests.items():
            assert _match_env_signature(output) is None, (
                f"{runner}: a runner-reported test failure must stay a code defect "
                "even though the output mentions an environment-shaped phrase"
            )

        env_failures = {
            "rspec load error": (
                "An error occurred while loading ./spec/api_spec.rb.\n"
                "PG::ConnectionBad: connection refused\n0 examples, 0 failures\n"
            ),
            "rake aborted": (
                "rake aborted!\nPG::ConnectionBad: could not connect to server: "
                "connection refused\n"
            ),
            "pytest collection": (
                "ERROR tests/conftest.py::db - OperationalError: connection refused\n"
                "1 error in 0.42s\n"
            ),
            "port clash": "OSError: [Errno 98] Address already in use\n",
            # An env failure can surface THROUGH an assertion: a fixture or setup
            # helper asserting that a service came up. The exception name
            # "AssertionError" (and pytest's "E   assert" detail line) is
            # therefore NOT evidence of a failed test, and must not outrank the
            # signature — doing so bounces the implementer on an environment
            # problem it cannot fix.
            "fixture asserts db is up": (
                "ERROR tests/conftest.py::db_ready\n"
                "E       assert wait_for_port('localhost', 5432)\n"
                "E       AssertionError: connection refused\n"
                "1 error in 3.10s\n"
            ),
            "setup helper asserts": (
                "AssertionError: redis not reachable: connection refused\n"
                "rake aborted!\n"
            ),
            # Prose that merely contains a number and the word "failures" is not
            # a runner summary; only a comma-anchored count is.
            "healthcheck prose": (
                "waiting for postgres...\n3 failures to connect, giving up\n"
                "could not connect to server\n"
            ),
        }
        for scenario, output in env_failures.items():
            assert _match_env_signature(output) is not None, (
                f"{scenario}: the suite never reported a test failure, so the "
                "environment signature must still classify this env_blocked"
            )

        # Deliberately NOT environment signatures. Review's point, accepted: a
        # missing executable is the shell-level twin of ModuleNotFoundError,
        # which this gate already excludes because a forgotten dependency in the
        # diff IS a defect the implementer can fix. Ambiguous both ways, and the
        # gate settles ambiguity toward handing it back, with remote CI as the
        # backstop.
        not_env = {
            "missing toolchain": "bundle: command not found\n",
            "missing script": "./scripts/migrate.sh: command not found\n",
        }
        for scenario, output in not_env.items():
            assert _match_env_signature(output) is None, (
                f"{scenario}: this is as likely a code defect as an environment "
                "problem, and the gate must not mask a defect"
            )

        # Prose that contains a count and a failure word is not a runner summary.
        # Every one of these matched the pattern before review anchored it, so an
        # environment failure was being handed to the implementer as a red suite.
        prose_not_a_test_result = (
            "2 failed to connect to postgres:5432",
            "1 failed attempt to reach redis, retrying",
            "5 failing attempts to bind port 5432",
            "3 failures to connect, giving up",
            "connection refused after 3 failed retries in the pool",
        )
        for text in prose_not_a_test_result:
            assert not _TEST_FAILURE_MARKERS.search(text), (
                f"{text!r} is prose, not a runner summary; treating it as a test "
                "result bounces the implementer onto an environment problem"
            )

        # ...while every real runner summary still reads as one.
        real_summaries = (
            "=== 1 failed, 12 passed in 3.2s ===",
            "== 1 failed in 3.21s ==",
            "  1 failing",
            "3 examples, 1 failure",
            "Tests run: 4, Failures: 1, Errors: 0",
            "  1) Failure:",
            "  Failure/Error: expect(x).to eq(1)",
            "FAILED tests/unit/test_x.py::test_y - AssertionError",
        )
        for text in real_summaries:
            assert _TEST_FAILURE_MARKERS.search(text), (
                f"{text!r} is a runner summary and must outrank any signature"
            )

    async def test_a_coloured_red_suite_is_not_held_as_an_environment_block(self) -> None:
        """The dangerous direction, found by review.

        A runner told to colour its output writes "\x1b[31m1 failed\x1b[0m," --
        the escape sits between "failed" and the comma every summary pattern
        looks for, so the precedence check goes blind while the env signatures,
        being plain substrings, still match. A genuinely red suite was therefore
        held as an environment block and the implementer never heard about its
        own bug, which is precisely what the precedence rule exists to prevent.
        """
        from performer.main import _run_test_check

        coloured = (
            "\x1b[31m1 failed\x1b[0m, 99 passed in 3.20s\n"
            "E   ConnectionError: connection refused\n"
        )
        run_command = AsyncMock(side_effect=[
            _ci_run_result(False, stdout=coloured),
            _ci_run_result(False, stdout=coloured),
        ])
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))

        assert result.env_blocked is False, (
            "a red suite with coloured output was held as an environment block; "
            "the implementer never hears about a real defect"
        )
        assert "\x1b[" not in result.output, "escape codes reached the feedback body"

    async def test_the_failure_count_reads_the_run_that_just_happened(self) -> None:
        """Review: max() across the output let a stale count win.

        A runner prints its summary when it finishes and the output is
        tail-truncated, so the last count is the real one. An earlier, larger
        count is captured output from something else -- a nested suite, a
        subprocess under test -- and letting it win skipped the retry, bouncing
        a flake as a broken diff.
        """
        from performer.main import _reported_failure_count

        embedded = "captured stderr: 15 failed, 10 passed\n---\n3 failed, 9 passed in 1.2s"

        assert _reported_failure_count(embedded) == 3

    async def test_the_count_survives_colour(self) -> None:
        from performer.main import _reported_failure_count

        assert _reported_failure_count("\x1b[31m2 failed\x1b[0m, 9 passed in 1.0s") is None, (
            "raw ANSI is expected to defeat the count; _run_test_check strips first"
        )

    async def test_a_broad_failure_is_not_retried(self) -> None:
        """Review: the expected outcome was paying the cost of the rare one.

        Every genuine red suite ran twice in full before the implementer heard
        anything -- ten extra minutes on a ten-minute suite -- to recover a flake
        that is uncommon by definition. A wall of failing tests is the diff, not
        timing.
        """
        from performer.main import _run_test_check

        run_command = AsyncMock(
            return_value=_ci_run_result(False, stdout="40 failed, 2 passed in 91.02s")
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))

        assert result.passed is False
        assert result.env_blocked is False
        assert run_command.await_count == 1, (
            "40 failing tests is a broken diff; re-running it is latency before "
            "feedback the implementer could already have had"
        )

    async def test_a_narrow_failure_is_still_retried(self) -> None:
        """The flake path survives the gating: one failure is plausibly timing."""
        from performer.main import _run_test_check

        run_command = AsyncMock(
            side_effect=[
                _ci_run_result(False, stdout="1 failed, 12 passed in 3.20s"),
                _ci_run_result(True, stdout="13 passed in 3.10s"),
            ]
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))

        assert result.passed is True, "a recovered flake must clear the gate"
        assert run_command.await_count == 2

    async def test_an_unreadable_summary_still_retries(self) -> None:
        """Unknown count is the conservative direction.

        Guessing "broad" would remove flake recovery from every runner whose
        summary this cannot parse, including a runner that crashed outright.
        """
        from performer.main import _run_test_check

        run_command = AsyncMock(
            side_effect=[
                _ci_run_result(False, stderr="Segmentation fault (core dumped)"),
                _ci_run_result(True, stdout="ok"),
            ]
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))

        assert result.passed is True
        assert run_command.await_count == 2

    async def test_two_different_red_attempts_keep_the_first(self) -> None:
        """A non-idempotent suite can fail two ways; reporting only the second
        leaves nothing to say the first differed."""
        from performer.main import _run_test_check

        run_command = AsyncMock(
            side_effect=[
                _ci_run_result(False, stdout="1 failed, 9 passed in 2.0s\ntest_alpha failed"),
                _ci_run_result(False, stdout="1 failed, 9 passed in 2.0s\ntest_beta failed"),
            ]
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))

        assert result.retried is True
        assert result.first_attempt_output and "test_alpha" in result.first_attempt_output
        assert "test_beta" in result.output

    async def test_two_identical_red_attempts_do_not_duplicate_the_output(self) -> None:
        """Carrying the first attempt only matters when it differs."""
        from performer.main import _run_test_check

        same = "1 failed, 9 passed in 2.0s\ntest_alpha failed"
        run_command = AsyncMock(
            side_effect=[_ci_run_result(False, stdout=same), _ci_run_result(False, stdout=same)]
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))

        assert result.retried is True
        assert result.first_attempt_output is None

    async def test_the_feedback_excerpt_redacts_an_auth_header(self) -> None:
        """Review: this change widened the excerpt window, so it closes the leak.

        The body went from a 500-char head slice to a 1500-char head+tail, which
        newly includes the end of the output where config dumps and connection
        strings land.
        """
        from performer.main import _env_signature_reason, _format_failure_excerpt

        leaky = "GET /repos\nAuthorization: Bearer ghp_secrettoken\nconnection refused"

        assert "ghp_secrettoken" not in _format_failure_excerpt(leaky)
        assert "ghp_secrettoken" not in _env_signature_reason("connection refused", leaky)

    async def test_timeout_is_not_retried(self) -> None:
        """A timed-out run already burned the whole timeout budget; retrying it
        would double the gate's worst case (2×600s by default) for a suite that
        is hanging rather than flaking."""
        from performer.main import _run_test_check

        run_command = AsyncMock(
            return_value=_ci_run_result(
                False, stderr="Command timed out after 600s", timed_out=True
            )
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert run_command.await_count == 1, "a timeout must not be retried"

    async def test_a_timeout_is_held_not_handed_back(self) -> None:
        """Review: the classification went the wrong way for the likeliest cause.

        A suite that hangs to its deadline is more often waiting on a service
        that never came up than failing an assertion, which returns quickly --
        and the timeout path discards both streams, so the signature matcher has
        nothing to work with. The case with the strongest prior for being
        environmental is the one where the classifier is blind, so the prior
        decides it: a human HOLD, which is the outcome an operator wants for a
        hung suite, rather than an implementer re-reading its own diff.
        """
        from performer.main import _run_test_check

        run_command = AsyncMock(
            return_value=_ci_run_result(
                False, stderr="Command timed out after 600s", timed_out=True
            )
        )
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=run_command),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))

        assert result.env_blocked is True, (
            "a hung suite was handed to the implementer as a code defect"
        )
        assert result.env_reason and "did not finish" in result.env_reason
        assert "600" in result.env_reason, "the reason should name the deadline it hit"


@pytest.mark.asyncio
class TestImplementerLocalTestGateDonePath:
    """T011: gate runs after lint, before push, on the implementer done-path."""

    def _impl_perf(self):
        perf = _make_perf(session_id="sid")
        perf.score.role = "implementer"
        perf.score.local_test_gate = {"enabled": True, "timeout_seconds": 600}
        perf.backend.get_status.return_value = BackendStatus(state="done")
        return perf

    async def test_green_pushes_and_opens_pr(self) -> None:
        perf = self._impl_perf()
        push = AsyncMock()
        with (
            patch("performer.main._run_ci_check", new=AsyncMock(return_value=(True, ""))),
            patch(
                "performer.main._run_test_check",
                new=AsyncMock(return_value=_local_test_result(passed=True, command="pytest")),
            ),
            patch("performer.main.push_branch", new=push),
            patch(
                "performer.main.create_pull_request",
                new=AsyncMock(return_value=("https://github.com/org/repo/pull/1", "PR_n1")),
            ),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "working"
        push.assert_awaited_once()

    async def test_code_failure_blocks_push(self) -> None:
        perf = self._impl_perf()
        push = AsyncMock()
        with (
            patch("performer.main._run_ci_check", new=AsyncMock(return_value=(True, ""))),
            patch(
                "performer.main._run_test_check",
                new=AsyncMock(
                    return_value=_local_test_result(
                        passed=False, command="pytest", output="E AssertionError: boom", env_blocked=False
                    )
                ),
            ),
            patch("performer.main.push_branch", new=push),
            patch("performer.main.create_pull_request", new=AsyncMock()),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="headsha1")),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "changes_requested"
        assert resp.local_test_failed is True
        assert "boom" in (resp.comments[0]["body"] if resp.comments else "")
        # head_after lets the coordinare key local_fix_counter and reset on a new HEAD.
        assert resp.head_after == "headsha1"
        push.assert_not_awaited()

    async def test_failure_body_includes_command_and_exit_code(self) -> None:
        """Feedback body names the command and exit code so the implementer sees
        how the run terminated, not just an opaque output tail."""
        perf = self._impl_perf()
        with (
            patch("performer.main._run_ci_check", new=AsyncMock(return_value=(True, ""))),
            patch(
                "performer.main._run_test_check",
                new=AsyncMock(
                    return_value=_local_test_result(
                        passed=False,
                        command="uv run pytest",
                        output="E AssertionError: boom",
                        env_blocked=False,
                        exit_code=1,
                    )
                ),
            ),
            patch("performer.main.push_branch", new=AsyncMock()),
            patch("performer.main.create_pull_request", new=AsyncMock()),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="headsha1")),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        body = resp.comments[0]["body"]
        assert "uv run pytest" in body
        assert "exit code 1" in body
        assert "boom" in body

    async def test_gate_dormant_when_unconfigured(self) -> None:
        """SC-005: no local_test_gate on score → no _run_test_check call, push proceeds."""
        perf = self._impl_perf()
        perf.score.local_test_gate = None
        test_check = AsyncMock()
        with (
            patch("performer.main._run_ci_check", new=AsyncMock(return_value=(True, ""))),
            patch("performer.main._run_test_check", new=test_check),
            patch("performer.main.push_branch", new=AsyncMock()),
            patch(
                "performer.main.create_pull_request",
                new=AsyncMock(return_value=("https://github.com/org/repo/pull/1", "PR_n1")),
            ),
            patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "working"
        test_check.assert_not_awaited()


def _local_test_result(
    *,
    passed: bool,
    command: str | None,
    output: str = "",
    env_blocked: bool = False,
    env_reason: str | None = None,
    exit_code: int | None = None,
):
    from performer.main import LocalTestResult

    return LocalTestResult(
        passed=passed,
        command=command,
        output=output,
        duration_seconds=1.0,
        env_blocked=env_blocked,
        env_reason=env_reason,
        exit_code=exit_code,
    )


# ---------------------------------------------------------------------------
# 089 US2 — broken env-cache classified as env-blocked, not a code defect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunTestCheckEnvBlocked:
    """T014: a failing run coinciding with a spec-088 env signal → env_blocked."""

    async def test_code_fail_with_services_start_failure_is_env_blocked(self) -> None:
        from performer.main import _run_test_check

        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=AsyncMock(return_value=_ci_run_result(False, stderr="boom"))),
            patch("performer.workspace.consume_services_start_failure", return_value="postgres failed to start"),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert result.env_blocked is True
        assert result.env_reason == "postgres failed to start"

    async def test_code_fail_with_env_cache_health_failure_is_env_blocked(self) -> None:
        from performer.main import _run_test_check

        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=AsyncMock(return_value=_ci_run_result(False, stderr="boom"))),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=True),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert result.env_blocked is True
        assert result.env_reason

    async def test_timeout_with_env_signal_is_env_blocked(self) -> None:
        from performer.main import _run_test_check

        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=AsyncMock(return_value=_ci_run_result(False, stderr="timed out"))),
            patch("performer.workspace.consume_services_start_failure", return_value="services never came up"),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.env_blocked is True

    async def test_timeout_no_signal_is_code_failure(self) -> None:
        from performer.main import _run_test_check

        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=AsyncMock(return_value=_ci_run_result(False, stderr="timed out"))),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is False
        assert result.env_blocked is False

    async def test_green_run_does_not_consult_env_signal(self) -> None:
        """A green run pushes; env signals are never consulted (no false env-block)."""
        from performer.main import _run_test_check

        services = MagicMock(return_value="should not be read")
        health = MagicMock(return_value=True)
        with (
            patch("coordinare.services.ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=AsyncMock(return_value=_ci_run_result(True))),
            patch("performer.workspace.consume_services_start_failure", new=services),
            patch("performer.workspace.consume_env_cache_health_failure", new=health),
        ):
            result = await _run_test_check(Path("/tmp/x"))
        assert result.passed is True
        assert result.env_blocked is False
        services.assert_not_called()
        health.assert_not_called()


@pytest.mark.asyncio
class TestImplementerLocalTestGateEnvBlockedDonePath:
    """T015: helper env_blocked → PerformerResponse(status='env_blocked'), no push."""

    def _impl_perf(self):
        perf = _make_perf(session_id="sid")
        perf.score.role = "implementer"
        perf.score.local_test_gate = {"enabled": True, "timeout_seconds": 600}
        perf.backend.get_status.return_value = BackendStatus(state="done")
        return perf

    async def test_env_blocked_holds_without_push(self) -> None:
        perf = self._impl_perf()
        push = AsyncMock()
        with (
            patch("performer.main._run_ci_check", new=AsyncMock(return_value=(True, ""))),
            patch(
                "performer.main._run_test_check",
                new=AsyncMock(
                    return_value=_local_test_result(
                        passed=False,
                        command="pytest",
                        output="boom",
                        env_blocked=True,
                        env_reason="postgres failed to start",
                    )
                ),
            ),
            patch("performer.main.push_branch", new=push),
            patch("performer.main.create_pull_request", new=AsyncMock()),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf)
        assert resp.status == "env_blocked"
        assert resp.reason == "postgres failed to start"
        assert resp.local_test_failed is False
        push.assert_not_awaited()


# ---------------------------------------------------------------------------
# Terminal-failure reason backfill (silent-card-block observability fix)
# ---------------------------------------------------------------------------


def test_backfill_terminal_failure_reason_synthesizes_status_reason() -> None:
    """A failing status with no reason/diagnostic fields gets a synthesized
    reason naming the status, so exclude_none can't drop the only signal."""
    resp = PerformerResponse(status="error", session_id="sid")
    out = _backfill_terminal_failure_reason(resp)
    assert out.reason
    assert "error" in out.reason
    # The serialized summary the coordinare reads now carries the reason.
    assert "reason" in out.model_dump_json(exclude_none=True)


def test_backfill_terminal_failure_reason_prefers_diagnostic_fields() -> None:
    """When present, inference_skipped_reason is preferred over a synthesized
    status string."""
    resp = PerformerResponse(
        status="error",
        session_id="sid",
        inference_skipped_reason="upstream model unreachable",
    )
    out = _backfill_terminal_failure_reason(resp)
    assert out.reason == "upstream model unreachable"


def test_backfill_terminal_failure_reason_leaves_explicit_reason() -> None:
    """An already-reasoned failure is returned unchanged."""
    resp = PerformerResponse(status="error", session_id="sid", reason="boom")
    out = _backfill_terminal_failure_reason(resp)
    assert out.reason == "boom"


def test_backfill_terminal_failure_reason_ignores_success() -> None:
    """A non-failure status is never mutated (no spurious reason added)."""
    resp = PerformerResponse(status="pr_opened", session_id="sid")
    out = _backfill_terminal_failure_reason(resp)
    assert out.reason is None
