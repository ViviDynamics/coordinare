"""System dispatch failures need operator diagnostics, not product questions."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.handle_blocked import handle_blocked
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state
from tests.unit.graph.nodes.test_handle_blocked import _GitHubFallback
from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [
    {"system_error_reason": "ProxyLaunchError: startup probe failed"},
    {"env_blocked": {"cause": "Runner unavailable"}},
    {"latest_ci_gate_decision": {"verdict": "escalate"}},
])
async def test_system_block_posts_operator_diagnostic(marker):
    github = _GitHubFallback()
    state = initial_state()
    state.update(marker)
    state["card_clarifications"] = [
        {"author": "operator", "created_at": "2026-09-08T10:00:00Z", "body": "Requirements clarified"},
        {"author": "coordinare[bot]", "created_at": "2026-09-08T11:00:00Z", "body": "Earlier product question"},
    ]
    state.update(github_service=github, current_card={"id": "ITEM_1", "issue_id": "ISSUE_1"}, open_questions=[])
    result = await handle_blocked(state)
    assert result["phase"] == "blocked"
    assert "System blocked" in github.comment_body
    assert "Needs input" not in github.comment_body


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["assessing", "qa", "tech_writing"])
async def test_monitor_preserves_proxy_failure_for_operator_comment(stage):
    github = _GitHubFallback()
    reason = "ProxyLaunchError: startup probe failed"
    state = _make_state(service=_Performer({"status": "error", "reason": reason}), stage=stage,
                        github=github, card={"id": "ITEM_1", "issue_id": "ISSUE_1"})
    result = await monitor_performer(state)
    assert result["system_error_reason"] == reason
    assert result["phase"] == "blocked"
    await handle_blocked(result)
    assert "System blocked" in github.comment_body
    assert reason in github.comment_body
    assert "Needs input" not in github.comment_body


@pytest.mark.asyncio
async def test_product_clarification_remains_a_question():
    github = _GitHubFallback()
    state = initial_state()
    state.update(github_service=github, current_card={"id": "ITEM_1", "issue_id": "ISSUE_1"},
                 open_questions=["Which behavior should this feature use?"])
    await handle_blocked(state)
    assert "Needs input" in github.comment_body
    assert "Which behavior" in github.comment_body
    assert "System blocked" not in github.comment_body


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True])
async def test_recovered_retry_restores_product_clarification_semantics(answered):
    github = _GitHubFallback()
    question = "Which behavior should this feature use?"
    state = _make_state(service=_Performer({"status": "blocked", "questions": [question]}), stage="qa",
                        github=github, card={"id": "ITEM_1", "issue_id": "ISSUE_1"})
    state["system_error_reason"] = "server disconnected on the previous attempt"
    if answered:
        state["card_clarifications"] = [
            {"author": "operator", "created_at": "2026-09-08T10:00:00Z", "body": "Use behavior A"},
            {"author": "coordinare[bot]", "created_at": "2026-09-08T11:00:00Z", "body": question},
        ]
    result = await monitor_performer(state)
    assert result["system_error_reason"] is None
    result = await handle_blocked(result)
    if answered:
        assert result["phase"] == "idle"
    else:
        assert "Needs input" in github.comment_body
        assert question in github.comment_body
        assert "System blocked" not in github.comment_body


@pytest.mark.asyncio
@pytest.mark.parametrize("current_hold", [
    {"env_blocked": {"cause": "Registry unavailable"}},
    {"latest_ci_gate_decision": {"verdict": "escalate"}},
])
async def test_current_infrastructure_hold_survives_retry_reason_cleanup(current_hold):
    github = _GitHubFallback()
    state = _make_state(service=_Performer({"status": "blocked", "questions": ["Operator repair required"]}), stage="qa",
                        github=github, card={"id": "ITEM_1", "issue_id": "ISSUE_1"})
    state.update(current_hold)
    state["system_error_reason"] = "old transport error"
    result = await monitor_performer(state)
    assert result["system_error_reason"] is None
    await handle_blocked(result)
    assert "System blocked" in github.comment_body


@pytest.mark.asyncio
@pytest.mark.parametrize('hold,expected', [
    ({'pattern_id': 'registry_auth', 'head_sha': 'abc'}, ['registry_auth']),
    ({'pattern_id': 'registry_auth', 'head_sha': 'abc', 'action': 'Renew registry credentials'},
     ['registry_auth', 'Renew registry credentials']),
    ({'action': 'Restore runner connectivity'}, ['Restore runner connectivity']),
])
async def test_restored_infrastructure_hold_keeps_operator_diagnostic(hold, expected):
    github = _GitHubFallback()
    state = initial_state()
    state.update(github_service=github, current_card={'id': 'ITEM_1', 'issue_id': 'ISSUE_1'},
                 open_questions=[], env_blocked=hold)
    result = await handle_blocked(state)
    assert result['phase'] == 'blocked'
    assert 'System blocked' in github.comment_body
    for diagnostic in expected:
        assert diagnostic in github.comment_body
    assert 'Needs input' not in github.comment_body


@pytest.mark.asyncio
async def test_ci_escalation_reports_failed_checks_without_fabricating_a_question():
    github = _GitHubFallback()
    state = initial_state()
    state.update(github_service=github, current_card={"id": "ITEM_1", "issue_id": "ISSUE_1"},
                 open_questions=[], latest_ci_gate_decision={
                     "verdict": "escalate", "failed_checks": [
                         {"name": "Lint", "conclusion": "failure"},
                         {"name": "Unit tests", "conclusion": "failure"},
                     ],
                 })
    await handle_blocked(state)
    assert "CI gate escalated" in github.comment_body
    assert "Lint, Unit tests" in github.comment_body
    assert "repair or rerun CI" in github.comment_body
    assert "CI could not complete" not in github.comment_body
    assert "Needs input" not in github.comment_body
