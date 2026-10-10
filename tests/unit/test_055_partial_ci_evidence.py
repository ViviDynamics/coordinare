"""A partial evidence denial cannot override an accessible failed Actions log."""
from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from performer import main
from performer.github import get_check_runs
from performer.infrastructure import CIInfrastructureBlocked, check_infrastructure_reason
from performer.workflows.implementer.ci import CIFailed, run_ci_phase


@pytest.mark.asyncio
@pytest.mark.parametrize("denied_status", [401, 403])
@pytest.mark.parametrize("denied_source", ["annotations", "workflow"])
@pytest.mark.parametrize("log_status", [200, 403])
async def test_partial_denial_uses_readable_current_check_log(
    monkeypatch, denied_status, denied_source, log_status,
):
    calls = []
    failure = "FAILED test_boolean: DID NOT RAISE ValueError"

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/commits/head/check-runs"):
            return httpx.Response(200, json={"check_runs": [{
                "id": 197, "name": "ci", "status": "completed", "conclusion": "failure",
                "details_url": "https://github.com/example/sample/actions/runs/3/job/7",
                "output": {"title": "Tests failed", "summary": ""},
            }]})
        if path.endswith("/annotations"):
            return (httpx.Response(denied_status) if denied_source == "annotations"
                    else httpx.Response(200, json=[{"annotation_level": "failure", "message": failure}]))
        if path.endswith("/actions/jobs/7"):
            return httpx.Response(200, json={"name": "ci", "steps": [{"name": "Run tests", "conclusion": "failure"}]})
        if path.endswith("/actions/runs/3"):
            return httpx.Response(200, json={"path": ".github/workflows/ci.yml", "head_sha": "head"})
        if "/contents/" in path:
            document = "jobs:\n  ci:\n    steps:\n      - name: Run tests\n        run: pytest tests/\n"
            return (httpx.Response(denied_status) if denied_source == "workflow"
                    else httpx.Response(200, json={"content": base64.b64encode(document.encode()).decode()}))
        if path.endswith("/actions/jobs/197/logs"):
            return httpx.Response(200, text="UNRELATED_JOB_ERROR")
        if path.endswith("/actions/jobs/7/logs"):
            return httpx.Response(log_status, text=failure if log_status == 200 else "")
        raise AssertionError(path)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    checks = await get_check_runs("example", "sample", "head", "synthetic-token")
    assert checks[0]["conclusion"] == "failure"
    assert checks[0]["evidence_access_denied"] is True
    assert checks[0]["setup_failure"] is False
    reason = check_infrastructure_reason(checks)
    if log_status == 200:
        assert reason is None
        assert failure in checks[0]["output"]["text"]
        assert checks[0]["evidence_log_available"] is True
    else:
        assert reason is not None and "denied access" in reason
    assert calls.count("/repos/example/sample/actions/jobs/7/logs") == 1
    assert not any("/actions/jobs/197/logs" in path for path in calls)

    ctx = SimpleNamespace(
        get_check_runs=AsyncMock(return_value=checks), github_api_calls=0,
        budgets=SimpleNamespace(ci_wait_s=1, ci_repairs=0),
        get_check_run_logs=AsyncMock(return_value="UNRELATED_JOB_ERROR" if log_status == 200 else ""),
        push=AsyncMock(), toolkit=SimpleNamespace(agent_turn=AsyncMock()),
    )
    expected = CIFailed if log_status == 200 else CIInfrastructureBlocked
    with pytest.raises(expected) as caught:
        await run_ci_phase(ctx, head_sha="head", quality=[], rerun_green=AsyncMock())
    ctx.push.assert_not_awaited()
    ctx.toolkit.agent_turn.assert_not_awaited()

    if log_status == 200:
        assert failure in caught.value.excerpt
        assert "UNRELATED_JOB_ERROR" not in caught.value.excerpt
        ctx.get_check_run_logs.assert_not_awaited()
        monkeypatch.setattr(main, "get_check_runs", AsyncMock(return_value=checks))
        perf = SimpleNamespace(
            pr_head_sha="head", session_id="session", state="checking",
            pr_url="https://github.com/example/sample/pull/1", pr_node_id="PR",
            check_attempt=0, check_no_progress_streak=0, last_check_failure_signature="",
            score=SimpleNamespace(owner_repo=("example", "sample"), effective_github_token="synthetic-token"),
            backend=SimpleNamespace(relay_feedback=AsyncMock()), open_questions=[],
        )
        result = await main._poll_check_runs(perf, None)
        assert result.status == "working"
        excerpt = perf.backend.relay_feedback.await_args.args[0]
        assert failure in excerpt and "UNRELATED_JOB_ERROR" not in excerpt
        assert not any("/actions/jobs/197/logs" in path for path in calls)


def test_real_infrastructure_hold_survives_readable_log():
    check = {"evidence_access_denied": True, "evidence_log_available": True,
             "output": {"text": "Cannot connect to the Docker daemon"}}
    assert "Runner container" in check_infrastructure_reason([check])
    check["setup_failure"] = True
    assert "setup" in check_infrastructure_reason([check])
