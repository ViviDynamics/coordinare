from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from performer.models import Score
from performer.server.models import JobInitPayload

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.workspace import WorkspaceManager
from tests.unit.graph.nodes.test_monitor_performer import _sec_state
from tests.unit.services.test_http_performer_service import _persistent_config, _workspace
from tests.unit.test_workspace import _make_config


def _scan(findings: list[dict] | None = None) -> dict:
    return {"version": 1, "complete": True, "tools": ["semgrep", "bandit"], "findings": findings or []}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["ssh", "kubernetes", "http", "docker"])
@pytest.mark.parametrize("local_clone", [True, False])
@pytest.mark.parametrize("existing_pr", [True, False])
async def test_non_subprocess_prepare_never_runs_git(transport, local_clone, existing_pr):
    manager = WorkspaceManager(_make_config(agent_transport=transport))
    manager._resolve_pr_head = AsyncMock(return_value="feature")
    card = {"id": "card"}
    if existing_pr:
        card["pr_url"] = "https://github.com/a/b/pull/1"
    with (
        patch("coordinare.workspace._run_git", new=AsyncMock(side_effect=AssertionError("daemon git"))),
        patch("coordinare.workspace.tempfile.mkdtemp", side_effect=AssertionError("daemon clone directory")),
    ):
        info = await manager.prepare(card, local_clone=local_clone)
    assert info.path is None
    assert info.branch == ("feature" if existing_pr else "coordinare/card/untitled")
    assert info.github_token


def test_scan_request_survives_http_metadata_and_score_validation():
    service = HTTPPerformerService(_persistent_config(roles=["security"]))
    scan_request = {"version": 1, "files": ["app.py"], "diff_status": "ok"}
    payload = service._build_job_payload({
        "id": "card", "title": "Card", "role": "security", "backend": "codex",
        "security_floor": scan_request,
    }, _workspace())
    received = JobInitPayload.model_validate(payload.model_dump(mode="json"))
    score = Score.model_validate({
        **received.metadata, "repo_url": received.repo_url, "branch": received.branch,
    })
    assert score.security_floor == scan_request


@pytest.mark.asyncio
@pytest.mark.parametrize("scan", [None, {}, {"version": 99, "complete": True, "findings": []},
    {**_scan(), "complete": False}, {**_scan(), "complete": "true"},
    {**_scan(), "findings": [{}]}, {**_scan(), "findings": "clean"},
    {**_scan(), "tools": ["bandit"]}, {**_scan(), "version": True}])
async def test_missing_old_or_malformed_scan_holds_even_with_stale_clean_state(scan):
    response = {"status": "security_passed", "report": {"security_floor": scan}}
    state = _sec_state(response=response)
    state["scanner_findings"] = []
    result = await monitor_performer(state)
    assert result["phase"] == "blocked"
    assert result["performer_stage"] == "security"
    assert any(f["category"] == "scanner_unavailable" for f in result["relay_feedback"])


@pytest.mark.asyncio
async def test_reported_clean_scan_advances():
    result = await monitor_performer(_sec_state(response={
        "status": "security_passed", "report": {"security_floor": _scan()},
    }))
    assert result["performer_stage"] == "qa"


@pytest.mark.asyncio
async def test_reported_high_finding_overrides_pass_and_routes_to_implementer():
    finding = {"severity": "high", "category": "injection", "description": "semgrep:rule",
               "file": "app.py", "line": 1, "routing": "implementer"}
    response = {"status": "security_passed", "findings": [finding],
                "report": {"security_floor": _scan([finding])}}
    result = await monitor_performer(_sec_state(response=response))
    assert result["performer_stage"] == "implementing"
    assert len(result["relay_feedback"]) == 1
    assert response["status"] == "security_passed"


@pytest.mark.asyncio
async def test_prose_response_cannot_bypass_floor_by_claiming_workflow_report():
    result = await monitor_performer(_sec_state(response={
        "status": "security_passed", "report": {"security": {"verdict": "security_passed"}},
    }))
    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_working_response_does_not_require_finished_scan():
    result = await monitor_performer(_sec_state(response={"status": "working"}))
    assert result["performer_stage"] == "security"
    assert result["phase"] != "blocked"


@pytest.mark.asyncio
async def test_old_performer_ignoring_workflow_config_cannot_pass():
    state = _sec_state(response={"status": "security_passed"})
    state["config"] = SimpleNamespace(performers=SimpleNamespace(
        resolved_role=lambda role: SimpleNamespace(workflow="security"),
    ))
    result = await monitor_performer(state)
    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_scanner_replaces_a_model_downgrade_of_the_same_finding():
    finding = {"severity": "critical", "category": "injection", "description": "semgrep:rule",
               "file": "app.py", "line": 1, "routing": "implementer"}
    response = {"status": "security_passed", "findings": [{**finding, "severity": "low"}],
                "report": {"security_floor": _scan([finding])}}
    result = await monitor_performer(_sec_state(response=response))
    assert result["performer_stage"] == "implementing"
    assert len(result["relay_feedback"]) == 1
    assert result["relay_feedback"][0]["severity"] == "critical"
