"""Unit tests for monitor_performer node (019-performer-lifecycle, T015).

Tests cover lifecycle advancement, terminal success states, error/blocked
handling, session_expired routing, and TransportError handling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.graph.nodes.monitor_performer import (
    TERMINAL_SUCCESS_STATES,
    _advance_stage,
    monitor_performer,
)
from coordinare.graph.state import initial_state
from coordinare.transport.base import TransportError

# ---------------------------------------------------------------------------
# Mock helpers (mirrors test_monitor_agent.py patterns)
# ---------------------------------------------------------------------------


class _GitHub:
    """Tracks move_card calls."""

    def __init__(self) -> None:
        self.move_calls: list[tuple[str, str]] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))


class _Performer:
    """Returns a configurable check_status response."""

    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


class _PerformerTransportError:
    """Raises TransportError from check_status."""

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        raise TransportError("connection refused")


class _WorkspaceManager:
    """Stub WorkspaceManager that records teardown calls."""

    def __init__(self) -> None:
        self.teardown_calls: list[Path] = []

    async def teardown(self, path: Path) -> None:
        self.teardown_calls.append(path)


# ---------------------------------------------------------------------------
# Helper to build state with performer_services for a given stage/sequence
# ---------------------------------------------------------------------------


def _make_state(
    *,
    service: object,
    stage: str = "implementing",
    sequence: list[str] | None = None,
    card: dict | None = None,
    github: object | None = None,
) -> dict:
    """Return a state dict pre-configured with performer_services and lifecycle_sequence."""
    state = initial_state()
    state["performer_services"] = {stage: service}
    state["performer_stage"] = stage
    state["lifecycle_sequence"] = sequence or [stage]
    state["current_card"] = card or {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    if github is not None:
        state["github_service"] = github
    return state


# ---------------------------------------------------------------------------
# T015-1: pr_opened triggers advancement to next stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_opened_advances_to_next_stage() -> None:
    """pr_opened on first of 2+ stages advances performer_stage and resets dispatch."""
    service = _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing", "reviewing"],
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


# ---------------------------------------------------------------------------
# T015-2: advancement from final stage transitions to monitoring_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_stage_pr_opened_transitions_to_monitoring_pr() -> None:
    """pr_opened on the only/final stage transitions to monitoring_pr."""
    gh = _GitHub()
    service = _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_NODE_42",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing"],
        card={"id": "ITEM_1", "status": "IN_PROGRESS"},
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert result["current_card"]["pr_url"] == "https://github.com/org/repo/pull/42"
    assert result["current_card"]["pr_node_id"] == "PR_NODE_42"
    assert result["current_card"]["status"] == "IN_REVIEW"
    assert result["current_card"]["previous_status"] == "IN_PROGRESS"
    assert ("ITEM_1", "IN_REVIEW") in gh.move_calls


# ---------------------------------------------------------------------------
# T015-3: error status sets phase="blocked", does NOT advance performer_stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_status_blocks_without_advancing_stage() -> None:
    """error status sets phase='blocked' and does NOT advance performer_stage."""
    service = _Performer({"status": "error", "reason": "Compilation failed"})
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing", "reviewing"],
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["performer_stage"] == "implementing"  # NOT advanced
    assert any("Compilation failed" in q for q in result["open_questions"])


# ---------------------------------------------------------------------------
# T015-4: in-progress returns unchanged state with phase="monitoring_performer"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_progress_returns_monitoring_performer() -> None:
    """Working/in-progress status keeps phase='monitoring_performer'."""
    service = _Performer({"status": "working"})
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"
    # performer_stage unchanged
    assert result["performer_stage"] == "implementing"


# ---------------------------------------------------------------------------
# T015-5: all terminal success states are recognized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_SUCCESS_STATES))
async def test_all_terminal_success_states_trigger_advancement(terminal_status: str) -> None:
    """Every member of TERMINAL_SUCCESS_STATES triggers _advance_stage."""
    service = _Performer({
        "status": terminal_status,
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing", "reviewing"],
    )

    result = await monitor_performer(state)

    # All terminal states on a non-final stage should advance
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"


def test_terminal_success_states_contains_expected_members() -> None:
    """Verify the exact set of terminal success states."""
    expected = {
        "pr_opened",
        "plan_committed",
        "approved",
        "security_passed",
        "qa_passed",
        "docs_committed",
        "assessment_complete",
    }
    assert expected == TERMINAL_SUCCESS_STATES


# ---------------------------------------------------------------------------
# T015-6 / SC-001: 4 configured roles — full sequence advancement ending in monitoring_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_four_role_lifecycle_full_advancement() -> None:
    """4 roles (implementer, reviewer, security, QA) — advance through all, ending in monitoring_pr."""
    gh = _GitHub()
    roles = ["implementing", "reviewing", "security", "qa"]

    # Build performer services — each role gets its own service
    services = {}
    for role in roles:
        services[role] = _Performer({
            "status": "pr_opened",
            "pr_url": "https://github.com/org/repo/pull/99",
            "pr_node_id": "PR_NODE_99",
        })

    state = initial_state()
    state["performer_services"] = services
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = roles
    state["current_card"] = {"id": "ITEM_SC001", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = gh

    # Stage 1: implementing -> reviewing
    result = await monitor_performer(state)
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"

    # Simulate re-dispatch: set phase back, set dispatch, keep stage
    result["agent_dispatch"] = {"session_id": "s2"}

    # Stage 2: reviewing -> security
    result = await monitor_performer(result)
    assert result["performer_stage"] == "security"
    assert result["phase"] == "dispatching"

    # Simulate re-dispatch
    result["agent_dispatch"] = {"session_id": "s3"}

    # Stage 3: security -> qa
    result = await monitor_performer(result)
    assert result["performer_stage"] == "qa"
    assert result["phase"] == "dispatching"

    # Simulate re-dispatch
    result["agent_dispatch"] = {"session_id": "s4"}

    # Stage 4 (final): qa -> monitoring_pr
    result = await monitor_performer(result)
    assert result["phase"] == "monitoring_pr"
    assert result["current_card"]["status"] == "IN_REVIEW"
    assert result["current_card"]["pr_url"] == "https://github.com/org/repo/pull/99"
    assert result["current_card"]["pr_node_id"] == "PR_NODE_99"
    assert ("ITEM_SC001", "IN_REVIEW") in gh.move_calls


# ---------------------------------------------------------------------------
# T015-7: blocked status with questions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_status_with_questions() -> None:
    """Blocked with questions sets phase='blocked' and populates open_questions."""
    service = _Performer({
        "status": "blocked",
        "questions": ["What API key?", "Which region?"],
    })
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["What API key?", "Which region?"]


@pytest.mark.asyncio
async def test_blocked_status_without_questions() -> None:
    """Blocked with no questions list sets open_questions to empty list."""
    service = _Performer({"status": "blocked"})
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == []


# ---------------------------------------------------------------------------
# 070: partial_progress + blocked-no-commits guardrail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_progress_routes_back_to_dispatching() -> None:
    """partial_progress relays next_focus and re-dispatches the implementing stage."""
    service = _Performer({
        "status": "partial_progress",
        "next_focus": "Finish wiring up the slot release path",
        "head_before": "aaa111",
        "head_after": "bbb222",
    })
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert result["agent_dispatch"] == {}
    relay = result["relay_feedback"]
    assert len(relay) == 1
    assert "Finish wiring up the slot release path" in relay[0]["body"]


@pytest.mark.asyncio
async def test_blocked_implementer_no_new_commits_routes_to_retry() -> None:
    """Implementer reporting blocked with head_before==head_after re-dispatches."""
    service = _Performer({
        "status": "blocked",
        "questions": ["should I keep going?"],
        "head_before": "aaa111",
        "head_after": "aaa111",
    })
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    relay = result["relay_feedback"]
    assert len(relay) == 1
    assert "without pushing any new commits" in relay[0]["body"]


@pytest.mark.asyncio
async def test_blocked_implementer_with_new_commits_stays_blocked() -> None:
    """Implementer reporting blocked WITH commit delta honors the blocked verdict."""
    service = _Performer({
        "status": "blocked",
        "questions": ["What credential should I use?"],
        "head_before": "aaa111",
        "head_after": "bbb222",
    })
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["What credential should I use?"]


@pytest.mark.asyncio
async def test_blocked_non_implementer_stage_ignores_commit_delta_guardrail() -> None:
    """Guardrail only fires for the implementing stage; reviewers can block freely."""
    service = _Performer({
        "status": "blocked",
        "questions": ["What is the threat model?"],
        "head_before": "aaa111",
        "head_after": "aaa111",
    })
    state = _make_state(service=service, stage="reviewing", sequence=["implementing", "reviewing"])

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["What is the threat model?"]


# ---------------------------------------------------------------------------
# T015-8: session_expired handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_expired_no_pr_requeues_to_idle() -> None:
    """session_expired with no PR requeues card to TODO and sets phase='idle'."""
    gh = _GitHub()
    service = _Performer({"status": "session_expired", "reason": "timeout"})
    state = _make_state(
        service=service,
        card={"id": "ITEM_1"},
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None
    assert ("ITEM_1", "TODO") in gh.move_calls


@pytest.mark.asyncio
async def test_session_expired_with_pr_resumes_monitoring_pr() -> None:
    """session_expired with pr_node_id set transitions to monitoring_pr."""
    gh = _GitHub()
    service = _Performer({"status": "session_expired"})
    state = _make_state(
        service=service,
        card={"id": "ITEM_1", "pr_node_id": "PR_NODE_1"},
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None
    # Should NOT move to TODO when PR exists
    assert ("ITEM_1", "TODO") not in gh.move_calls
    # 045: Must move card to IN_REVIEW on the board AND update local status
    # so check_board doesn't keep routing the card back to monitoring_agent
    # and reintroduce the session_expired loop.
    assert ("ITEM_1", "IN_REVIEW") in gh.move_calls
    assert result["current_card"]["status"] == "IN_REVIEW"
    assert result["current_card"]["previous_status"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_session_expired_with_pr_blocks_on_move_card_failure() -> None:
    """045: When ``move_card(IN_REVIEW)`` fails after session_expired we log
    the exception details and transition to ``blocked`` with a diagnostic
    question — not silently stay in ``monitoring_pr`` with a stale board.

    The local ``current_card["status"]`` still flips to IN_REVIEW so
    ``check_board`` doesn't reintroduce the loop while the operator
    investigates.
    """
    class _FailingGitHub(_GitHub):
        async def move_card(self, item_id: str, status: str) -> None:
            # Record intent so we know the code at least tried.
            self.move_calls.append((item_id, status))
            raise RuntimeError("GraphQL 503")

    gh = _FailingGitHub()
    service = _Performer({"status": "session_expired"})
    state = _make_state(
        service=service,
        card={"id": "ITEM_1", "pr_node_id": "PR_NODE_1"},
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert ("ITEM_1", "IN_REVIEW") in gh.move_calls
    assert result["current_card"]["status"] == "IN_REVIEW"
    questions = result.get("open_questions") or []
    assert any("IN_REVIEW" in q and "GraphQL 503" in q for q in questions)


@pytest.mark.asyncio
async def test_session_expired_preserves_open_questions_to_clarifications() -> None:
    """Open questions are saved to card_clarifications before clearing."""
    service = _Performer({"status": "session_expired"})
    state = _make_state(service=service, card={"id": "ITEM_1"})
    state["open_questions"] = ["Pending question"]

    result = await monitor_performer(state)

    assert result["open_questions"] == []
    assert len(result["card_clarifications"]) == 1
    assert result["card_clarifications"][0]["questions"] == ["Pending question"]
    assert result["card_clarifications"][0]["answer"] == ""


# ---------------------------------------------------------------------------
# T015-9: TransportError -> system_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_routes_to_system_error() -> None:
    """TransportError from check_status sets phase='system_error'."""
    service = _PerformerTransportError()
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 1
    assert result["system_error_reason"] is not None
    assert "TransportError" in result["system_error_reason"]
    # 065 Fix 22: upstream exception message is preserved for operator triage.
    assert "connection refused" in result["system_error_reason"]
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


@pytest.mark.asyncio
async def test_transport_error_resets_stale_notified_state() -> None:
    """Stale system_error_notified=True is reset before incrementing."""
    service = _PerformerTransportError()
    state = _make_state(service=service)
    state["system_error_count"] = 5
    state["system_error_notified"] = True

    result = await monitor_performer(state)

    assert result["phase"] == "system_error"
    # Count was reset to 0, then incremented to 1
    assert result["system_error_count"] == 1
    assert result["system_error_notified"] is False


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_service_and_no_card_returns_idle() -> None:
    """No performer service and no card -> idle."""
    state = initial_state()

    result = await monitor_performer(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_fallback_to_legacy_agent_service() -> None:
    """When performer_services is empty, falls back to agent_service."""
    service = _Performer({"status": "working"})
    state = initial_state()
    state["performer_services"] = {}
    state["agent_service"] = service
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"


@pytest.mark.asyncio
async def test_advancement_mid_sequence_resets_dispatch_state() -> None:
    """Advancing to a non-final stage resets agent_dispatch and agent_dispatch_at."""
    service = _Performer({
        "status": "plan_committed",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing", "reviewing", "qa"],
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


@pytest.mark.asyncio
async def test_final_stage_missing_pr_fields_routes_to_system_error() -> None:
    """Final stage pr_opened with missing pr_url/pr_node_id routes to system_error."""
    gh = _GitHub()
    service = _Performer({
        "status": "pr_opened",
        "pr_url": None,
        "pr_node_id": None,
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing"],
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "system_error"
    assert ("ITEM_1", "IN_REVIEW") not in gh.move_calls


@pytest.mark.asyncio
async def test_workspace_teardown_on_terminal_state() -> None:
    """Workspace teardown is called on terminal state (pr_opened final stage)."""
    wm = _WorkspaceManager()
    fake_ws = Path("/tmp/fake-ws")
    gh = _GitHub()
    service = _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing"],
        github=gh,
    )
    state["workspace_manager"] = wm
    state["workspace_path"] = fake_ws

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert wm.teardown_calls == [fake_ws]
    assert result["workspace_path"] is None
    assert result["workspace_branch"] is None


@pytest.mark.asyncio
async def test_no_workspace_teardown_when_still_working() -> None:
    """Workspace is NOT torn down while performer is still working."""
    wm = _WorkspaceManager()
    fake_ws = Path("/tmp/fake-ws")
    service = _Performer({"status": "working"})
    state = _make_state(service=service)
    state["workspace_manager"] = wm
    state["workspace_path"] = fake_ws

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert wm.teardown_calls == []
    assert result["workspace_path"] == fake_ws


@pytest.mark.asyncio
async def test_events_accumulated_from_status() -> None:
    """Performer events are accumulated (capped at 100)."""
    service = _Performer({"status": "working", "events": [{"type": "new_event"}]})
    state = _make_state(service=service)
    state["performer_events"] = [{"type": "old_event"}]

    result = await monitor_performer(state)

    assert result["performer_events"] == [{"type": "old_event"}, {"type": "new_event"}]


@pytest.mark.asyncio
async def test_metrics_stored_from_status() -> None:
    """Performer metrics are updated from status."""
    service = _Performer({"status": "working", "metrics": {"cpu": 42}})
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["performer_metrics"] == {"cpu": 42}


@pytest.mark.asyncio
async def test_error_status_includes_reason_in_open_questions() -> None:
    """Error status includes the reason in open_questions."""
    service = _Performer({"status": "error", "reason": "Out of memory"})
    state = _make_state(
        service=service,
        stage="reviewing",
        sequence=["implementing", "reviewing"],
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert len(result["open_questions"]) == 1
    assert "Out of memory" in result["open_questions"][0]
    assert "reviewing" in result["open_questions"][0]


@pytest.mark.asyncio
async def test_backend_format_error_routes_to_system_error() -> None:
    """Format-contract failures should route through retryable system_error path first."""
    service = _Performer(
        {"status": "error", "reason": "BACKEND_FORMAT_ERROR: invalid reviewer JSON after retries"},
    )
    state = _make_state(
        service=service,
        stage="reviewing",
        sequence=["implementing", "reviewing"],
    )
    state["open_questions"] = ["stale question to clear"]
    state["relay_feedback"] = [{"body": "stale feedback to clear"}]  # type: ignore[typeddict-unknown-key]

    result = await monitor_performer(state)

    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 1
    assert "BACKEND_FORMAT_ERROR" in (result.get("system_error_reason") or "")
    assert result.get("open_questions", []) == []
    assert result.get("relay_feedback", []) == []


@pytest.mark.asyncio
async def test_error_status_without_reason() -> None:
    """Error status with no reason still produces a meaningful open_questions entry."""
    service = _Performer({"status": "error"})
    state = _make_state(service=service)

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert len(result["open_questions"]) == 1
    assert "error" in result["open_questions"][0].lower()


@pytest.mark.asyncio
async def test_workflow_permission_push_error_reroutes_to_implementer() -> None:
    """Workflow-permission push failures should auto-reroute with feedback."""
    service = _Performer({
        "status": "error",
        "reason": (
            "git push failed (exit 1): Push rejected: attempted to modify workflow file "
            "`.github/workflows/main-branch-build.yml` but the GitHub App token lacks "
            "`workflows` permission."
        ),
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["assessing", "implementing", "reviewing"],
    )

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None
    assert result["open_questions"] == []
    feedback = result.get("relay_feedback") or []
    assert len(feedback) == 1
    assert "workflow" in str(feedback[0].get("body", "")).lower()
    assert result["feedback_cycle_count"] == 1


@pytest.mark.asyncio
async def test_workflow_permission_push_error_obeys_feedback_cycle_limit() -> None:
    """When the retry budget is exhausted, workflow push errors block the card."""
    from types import SimpleNamespace

    service = _Performer({
        "status": "error",
        "reason": (
            "git push failed (exit 1): refusing to allow a GitHub App to create or "
            "update workflow `.github/workflows/main-branch-build.yml` without "
            "`workflows` permission"
        ),
    })
    state = _make_state(
        service=service,
        stage="implementing",
        sequence=["implementing", "reviewing"],
        card={
            "id": "ITEM_1",
            "status": "IN_PROGRESS",
            "title": "Copy code button",
            "issue_number": 89,
            "pr_url": "https://github.com/ViviDynamics/website/pull/98",
        },
    )
    state["config"] = SimpleNamespace(max_feedback_cycles=1)
    state["feedback_cycle_count"] = 1  # type: ignore[typeddict-unknown-key]

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["feedback_cycle_count"] == 2
    msg = (result.get("open_questions") or [""])[0]
    assert "workflow_push_permission" in msg
    assert "pull/98" in msg


@pytest.mark.asyncio
async def test_workflow_permission_push_error_without_implementing_stage_logs_and_blocks() -> None:
    """If lifecycle lacks implementing, workflow permission errors should block with an explicit log."""
    from unittest.mock import patch

    service = _Performer({
        "status": "error",
        "reason": (
            "git push failed (exit 1): refusing to allow a GitHub App to create or "
            "update workflow `.github/workflows/main-branch-build.yml` without "
            "`workflows` permission"
        ),
    })
    state = _make_state(
        service=service,
        stage="reviewing",
        sequence=["reviewing"],
    )

    with patch("coordinare.graph.nodes.monitor_performer.logger.info") as info_mock:
        result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["performer_stage"] == "reviewing"
    assert any("Performer (reviewing) encountered an error" in q for q in result.get("open_questions", []))
    assert any(
        call.args
        and call.args[0] == "monitor_performer.workflow_push_permission_no_implementing_stage"
        and call.kwargs.get("performer_stage") == "reviewing"
        for call in info_mock.call_args_list
    )


# ---------------------------------------------------------------------------
# 020 — plan_path persistence via _advance_stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_path_persisted_to_card_mid_sequence() -> None:
    """Architect returns plan_committed with plan_path — card gets plan_path
    when advancing to the next stage (mid-sequence, not final)."""
    service = _Performer({
        "status": "plan_committed",
        "plan_path": "docs/plan.md",
    })
    state = _make_state(
        service=service,
        stage="architecting",
        sequence=["architecting", "implementing"],
        card={"id": "ITEM_1", "status": "IN_PROGRESS"},
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result["current_card"]["plan_path"] == "docs/plan.md"


@pytest.mark.asyncio
async def test_final_stage_plan_path_persisted() -> None:
    """Single-stage lifecycle (architecting only) — plan_committed with
    plan_path and pr_url persists plan_path on the card when transitioning
    to monitoring_pr."""
    gh = _GitHub()
    service = _Performer({
        "status": "plan_committed",
        "plan_path": "docs/architecture.md",
        "pr_url": "https://github.com/org/repo/pull/7",
        "pr_node_id": "PR_NODE_7",
    })
    state = _make_state(
        service=service,
        stage="architecting",
        sequence=["architecting"],
        card={"id": "ITEM_2", "status": "IN_PROGRESS"},
        github=gh,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert result["current_card"]["plan_path"] == "docs/architecture.md"
    assert result["current_card"]["pr_url"] == "https://github.com/org/repo/pull/7"
    assert result["current_card"]["pr_node_id"] == "PR_NODE_7"


def test_advance_stage_plan_path_none_status() -> None:
    """_advance_stage with status=None does not crash and does not set
    plan_path on the card."""
    state = initial_state()
    state["lifecycle_sequence"] = ["architecting", "implementing"]
    state["performer_stage"] = "architecting"
    state["current_card"] = {"id": "ITEM_3", "status": "IN_PROGRESS"}

    updates = _advance_stage(state, status=None)

    assert updates["performer_stage"] == "implementing"
    assert updates["phase"] == "dispatching"
    # No current_card update should be present when status is None.
    assert "current_card" not in updates


# ---------------------------------------------------------------------------
# 021 — changes_requested handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changes_requested_routes_to_implementer() -> None:
    """changes_requested stores comments as relay_feedback and re-dispatches implementer."""
    state = initial_state()
    svc = _Performer(response={"status": "changes_requested", "comments": [
        {"file": "src/main.py", "line": 10, "body": "Missing null check"},
    ]})
    state["performer_services"] = {"reviewing": svc}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result.get("relay_feedback") == [
        {"file": "src/main.py", "line": 10, "body": "Missing null check"},
    ]
    assert result["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_changes_requested_with_no_actionable_feedback_blocks() -> None:
    """065 Fix 4c: when a performer reports changes_requested with neither
    structured comments nor a prose body, coordinare must NOT re-dispatch the
    implementer (nothing to relay).  It blocks for operator triage."""
    state = initial_state()
    svc = _Performer(response={"status": "changes_requested", "comments": []})
    state["performer_services"] = {"closing_review": svc}
    state["performer_stage"] = "closing_review"
    state["lifecycle_sequence"] = ["implementing", "closing_review"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_REVIEW"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result.get("performer_stage") != "implementing"
    assert "no actionable feedback" in (result.get("system_error_reason") or "")
    questions = result.get("open_questions") or []
    assert any("closing_review" in q for q in questions)


@pytest.mark.asyncio
async def test_changes_requested_with_body_only_relays_body_as_comment() -> None:
    """065 Fix 4b: when structured comments are empty but a prose body is
    present, coordinare synthesises a single comment from the body and
    relays it to the implementer so the rejection is actionable."""
    state = initial_state()
    svc = _Performer(response={
        "status": "changes_requested",
        "comments": [],
        "body": "PR scope is too broad — split into two PRs.",
    })
    state["performer_services"] = {"closing_review": svc}
    state["performer_stage"] = "closing_review"
    state["lifecycle_sequence"] = ["implementing", "closing_review"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_REVIEW"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    relay = result.get("relay_feedback") or []
    assert len(relay) == 1
    assert relay[0]["body"] == "PR scope is too broad — split into two PRs."
    assert relay[0]["author_login"] == "coordinare"


@pytest.mark.asyncio
async def test_changes_requested_does_not_advance_lifecycle() -> None:
    """changes_requested does NOT advance to the next role — it routes back."""
    state = initial_state()
    svc = _Performer(response={
        "status": "changes_requested",
        "comments": [],
        "body": "Fix the thing.",
    })
    state["performer_services"] = {"reviewing": svc}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    # Should route back to implementing, NOT advance to security
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_feedback_cycle_limit_blocks_loop() -> None:
    """045: reviewer/security/qa → implementer loops are bounded by
    config.max_feedback_cycles.  On the Nth ``changes_requested`` we
    block the card instead of re-dispatching indefinitely.

    Observed failure mode (card #89, 2026-04-15): 5 review→security→qa
    →implement cycles in a row before a backend-parse failure finally
    broke the loop.  Per-Performance cycle counters reset on every
    dispatch, so the bound has to live on the coordinare side.
    """
    from types import SimpleNamespace

    state = initial_state()
    svc = _Performer(response={"status": "changes_requested", "comments": [
        {"file": "app/views/posts/show.html.erb", "line": 42, "body": "Copy button still not keyboard-accessible."},
        {"file": "spec/features/copy_spec.rb", "line": 88, "body": "Regression test is missing for the iOS tap case."},
    ]})
    state["performer_services"] = {"reviewing": svc}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {
        "id": "ITEM_1",
        "status": "IN_PROGRESS",
        "title": "Copy code blocks on blog posts",
        "issue_number": 89,
        "pr_url": "https://github.com/o/r/pull/94",
    }
    state["agent_dispatch"] = {"session_id": "s1"}
    state["config"] = SimpleNamespace(max_feedback_cycles=2)
    state["human_reviewers"] = ["alice", "bob"]
    # Simulate one prior cycle already used.
    state["feedback_cycle_count"] = 2  # type: ignore[typeddict-unknown-key]

    result = await monitor_performer(state)

    # Hit the limit — don't loop again.
    assert result["phase"] == "blocked"
    assert result["feedback_cycle_count"] == 3
    questions = result.get("open_questions") or []
    assert len(questions) == 1
    msg = questions[0]
    # Break-case notifies configured human reviewers by @-mention.
    assert "@alice" in msg and "@bob" in msg
    # Actionable context: PR link, card title, issue number, cycle count,
    # and the most recent feedback items that weren't getting resolved.
    assert "pull/94" in msg
    assert "#89" in msg
    assert "Copy code blocks on blog posts" in msg
    assert "Copy button still not keyboard-accessible." in msg
    assert "Regression test is missing" in msg
    assert "2" in msg  # max cycles appears
    # Must NOT have staged relay_feedback for another implementer run.
    assert result.get("performer_stage") != "implementing"


@pytest.mark.asyncio
async def test_feedback_cycle_zero_disables_bound() -> None:
    """045: ``max_feedback_cycles=0`` disables the bound (escape hatch)."""
    from types import SimpleNamespace

    state = initial_state()
    svc = _Performer(response={
        "status": "changes_requested",
        "comments": [],
        "body": "Fix the thing.",
    })
    state["performer_services"] = {"reviewing": svc}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["config"] = SimpleNamespace(max_feedback_cycles=0)
    state["feedback_cycle_count"] = 99  # type: ignore[typeddict-unknown-key]

    result = await monitor_performer(state)

    # Bound disabled — should re-dispatch normally even at high counts.
    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"


# ---------------------------------------------------------------------------
# 022 — security_failed handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_failed_routes_findings_to_implementer() -> None:
    """security_failed with implementer-routed findings resets to implementing."""
    state = initial_state()
    findings = [{"severity": "critical", "category": "injection", "routing": "implementer"}]
    svc = _Performer(response={"status": "security_failed", "findings": findings})
    state["performer_services"] = {"security": svc}
    state["performer_stage"] = "security"
    state["lifecycle_sequence"] = ["implementing", "security", "qa"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result.get("relay_feedback") == findings


@pytest.mark.asyncio
async def test_security_failed_routes_architecture_findings_to_architect() -> None:
    """security_failed with architect-routed findings resets to architecting."""
    state = initial_state()
    findings = [{"severity": "high", "category": "insecure_design", "routing": "architect"}]
    svc = _Performer(response={"status": "security_failed", "findings": findings})
    state["performer_services"] = {"security": svc}
    state["performer_stage"] = "security"
    state["lifecycle_sequence"] = ["architecting", "implementing", "security"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "architecting"
    assert result["phase"] == "dispatching"
    assert result.get("relay_feedback") == findings


# ---------------------------------------------------------------------------
# 023 — qa_failed handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_failed_routes_to_implementer() -> None:
    """qa_failed routes failures to implementer for remediation."""
    state = initial_state()
    failures = [{"criterion": "Login works", "expected": "200", "actual": "500", "test": "test_login"}]
    svc = _Performer(response={"status": "qa_failed", "failures": failures})
    state["performer_services"] = {"qa": svc}
    state["performer_stage"] = "qa"
    state["lifecycle_sequence"] = ["implementing", "qa"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result.get("relay_feedback") == failures


# ---------------------------------------------------------------------------
# 027 — Per-role timeout tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_timeout_blocks_card() -> None:
    """When role timeout is exceeded, card is blocked with timeout message."""
    from datetime import UTC, datetime, timedelta

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_dispatch_at"] = datetime.now(UTC) - timedelta(seconds=600)
    state["role_timeouts"] = {"implementing": 300}  # 5 min timeout, 10 min elapsed

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert any("timed out" in q for q in result.get("open_questions", []))


@pytest.mark.asyncio
async def test_no_timeout_when_within_limit() -> None:
    """When elapsed time is within timeout, normal monitoring continues."""
    from datetime import UTC, datetime, timedelta

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_dispatch_at"] = datetime.now(UTC) - timedelta(seconds=10)
    state["role_timeouts"] = {"implementing": 300}  # 5 min timeout, 10s elapsed

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # still working


@pytest.mark.asyncio
async def test_no_timeout_when_not_configured() -> None:
    """No role_timeouts entry → no timeout enforcement."""
    from datetime import UTC, datetime, timedelta

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_dispatch_at"] = datetime.now(UTC) - timedelta(hours=2)
    state["role_timeouts"] = {}  # no timeout configured

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # no enforcement


# ---------------------------------------------------------------------------
# 032 — Active Board Reconciliation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_consistent_column_no_action() -> None:
    """Card in expected column → normal monitoring continues."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": ["ITEM_1"], "TODO": [], "IN_REVIEW": []}

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # no reconciliation


@pytest.mark.asyncio
async def test_reconcile_backward_move_to_todo() -> None:
    """Card moved backward to TODO → idle."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": [], "TODO": ["ITEM_1"], "IN_REVIEW": []}

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_reconcile_forward_to_done() -> None:
    """Card moved to DONE → idle with cleared card."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": [], "TODO": [], "DONE": ["ITEM_1"]}

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["current_card"] is None


@pytest.mark.asyncio
async def test_reconcile_moved_to_blocked() -> None:
    """Card moved to BLOCKED → blocked phase."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": [], "BLOCKED": ["ITEM_1"], "TODO": []}

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_reconcile_no_board_snapshot_skips() -> None:
    """No board_snapshot → reconciliation skipped, normal monitoring."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    # board_snapshot defaults to {} from initial_state — reconciliation skipped

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # reconciliation skipped


@pytest.mark.asyncio
async def test_reconcile_card_not_found_in_populated_board() -> None:
    """Card not in any column of a populated board → idle."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_GONE", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": ["OTHER_CARD"], "TODO": [], "IN_REVIEW": []}

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["current_card"] is None


# ---------------------------------------------------------------------------
# 066 FR-010 — retirement semantics in reconcile paths
# ---------------------------------------------------------------------------


def _seed_active_session(state: dict, card_id: str) -> None:
    """Seed a single active session so reconcile paths can be exercised."""
    card = {"id": card_id, "status": "IN_PROGRESS"}
    state["active_card_id"] = card_id
    state["active_sessions"] = {
        card_id: {
            "current_card": card,
            "agent_dispatch": {"session_id": "s1"},
            "performer_stage": "implementing",
            "phase": "monitoring_performer",
        }
    }
    state["current_card"] = card


@pytest.mark.asyncio
async def test_reconcile_card_not_found_retires_session() -> None:
    """066 FR-010: card-not-found path retires the active session entirely."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": ["OTHER"], "TODO": [], "IN_REVIEW": []}
    _seed_active_session(state, "ITEM_GONE")

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["current_card"] is None
    assert result["active_card_id"] is None
    assert "ITEM_GONE" not in result["active_sessions"]


@pytest.mark.asyncio
async def test_reconcile_backward_move_retires_session() -> None:
    """066 FR-010: backward-move path retires the active session entirely."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": [], "TODO": ["ITEM_1"], "IN_REVIEW": []}
    _seed_active_session(state, "ITEM_1")

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["current_card"] is None
    assert result["active_card_id"] is None
    assert "ITEM_1" not in result["active_sessions"]


@pytest.mark.asyncio
async def test_reconcile_forward_to_done_retires_session() -> None:
    """066 FR-010: forward-to-DONE path retires the active session entirely."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["board_snapshot"] = {"IN_PROGRESS": [], "TODO": [], "DONE": ["ITEM_1"]}
    _seed_active_session(state, "ITEM_1")

    result = await monitor_performer(state)

    assert result["phase"] == "idle"
    assert result["current_card"] is None
    assert result["active_card_id"] is None
    assert "ITEM_1" not in result["active_sessions"]


# ---------------------------------------------------------------------------
# 030 — Live Requirement Sync tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requirement_change_detected_warn_policy() -> None:
    """Description change → requirements_changed=True, phase stays monitoring."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "NEW description"})

    config = MagicMock()
    config.requirement_change_policy = "warn"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "OLD description", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    assert result["requirements_changed"] is True
    assert result["phase"] == "monitoring_performer"  # warn doesn't re-dispatch


@pytest.mark.asyncio
async def test_requirement_unchanged_no_flag() -> None:
    """Same description → requirements_changed remains False."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "Same description"})

    config = MagicMock()
    config.requirement_change_policy = "warn"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "Same description", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    assert result["requirements_changed"] is False


@pytest.mark.asyncio
async def test_requirement_change_redispatch_policy() -> None:
    """re-dispatch policy → phase set to dispatching with updated card."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "UPDATED requirements"})

    config = MagicMock()
    config.requirement_change_policy = "re-dispatch"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "OLD requirements", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"]["description"] == "UPDATED requirements"
    assert result["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_requirement_check_api_failure_no_crash() -> None:
    """GitHub API failure → no crash, monitoring continues."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(side_effect=ConnectionError("api down"))

    config = MagicMock()
    config.requirement_change_policy = "warn"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "desc", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"  # continues despite error


@pytest.mark.asyncio
async def test_requirement_change_ignore_policy() -> None:
    """ignore policy → requirement change check skipped, no warning, performer continues."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "NEW description"})

    config = MagicMock()
    config.requirement_change_policy = "ignore"

    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "OLD description", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    # ignore policy skips the check entirely — requirements_changed stays False
    assert result["requirements_changed"] is False
    assert result["phase"] == "monitoring_performer"
    # and no GitHub requirement refetch should be performed
    github.get_issue_details.assert_not_awaited()


@pytest.mark.asyncio
async def test_requirement_sync_skipped_on_terminal_status() -> None:
    """Terminal performer status → requirement sync skipped, no re-dispatch."""
    from unittest.mock import AsyncMock, MagicMock

    github = MagicMock()
    github.get_issue_details = AsyncMock(return_value={"body": "CHANGED"})
    github.move_card = AsyncMock()

    config = MagicMock()
    config.requirement_change_policy = "re-dispatch"

    state = initial_state()
    svc = _Performer(response={"status": "pr_opened", "pr_url": "https://github.com/test/1", "pr_node_id": "PR_1"})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISS_1", "description": "OLD", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["github_service"] = github
    state["config"] = config

    result = await monitor_performer(state)

    # Terminal status takes priority — no re-dispatch even with re-dispatch policy
    assert result["phase"] != "dispatching"
    github.get_issue_details.assert_not_awaited()


# ---------------------------------------------------------------------------
# 031 — Human Override Controls tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_skip_advances_stage() -> None:
    """pending_override skip → advances to next role in lifecycle."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["pending_override"] = {"action": "skip"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_skip_last_role_transitions_to_monitoring_pr() -> None:
    """pending_override skip on final role → monitoring_pr."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "security"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["pending_override"] = {"action": "skip"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["phase"] == "monitoring_pr"
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_restart_sets_target_stage() -> None:
    """pending_override restart → sets performer_stage to target."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["pending_override"] = {"action": "restart", "target_stage": "implementing"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_restart_invalid_role_noop() -> None:
    """pending_override restart with invalid role → no stage change."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["pending_override"] = {"action": "restart", "target_stage": "nonexistent"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["performer_stage"] == "reviewing"  # unchanged
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_veto_blocks_card() -> None:
    """pending_override veto → phase set to blocked."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "implementing"
    state["current_card"] = {"id": "ITEM_1"}
    state["pending_override"] = {"action": "veto"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["phase"] == "blocked"
    assert "vetoed" in result["open_questions"][0].lower()
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_none_returns_none() -> None:
    """No pending_override → returns None (no-op)."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["pending_override"] = None

    result = _apply_pending_override(state)

    assert result is None


@pytest.mark.asyncio
async def test_monitor_performer_applies_override_before_polling() -> None:
    """monitor_performer consumes pending_override before checking status."""
    state = initial_state()
    svc = _Performer(response={"status": "working"})
    state["performer_services"] = {"implementing": svc, "reviewing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["pending_override"] = {"action": "skip"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert result["pending_override"] is None


@pytest.mark.asyncio
async def test_override_dashboard_precedence_over_pr_comment() -> None:
    """Dashboard override already set takes precedence (FR-010)."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    # Dashboard set veto; PR comment would have set skip — but dashboard wins
    # because it's already in pending_override when the graph runs
    state["pending_override"] = {"action": "veto"}
    state["current_card"] = {"id": "ITEM_1"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_override_on_idle_phase_noop() -> None:
    """Override when card is idle — helper still applies it (API guards prevent this)."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["phase"] = "idle"
    state["pending_override"] = {"action": "skip"}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]

    result = _apply_pending_override(state)

    # Helper applies it; the API endpoints prevent setting overrides on idle
    assert result is not None
    assert result["performer_stage"] == "reviewing"


@pytest.mark.asyncio
async def test_override_restart_earlier_than_first_role() -> None:
    """restart-from to a valid early role works correctly."""
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override

    state = initial_state()
    state["performer_stage"] = "security"
    state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    state["pending_override"] = {"action": "restart", "target_stage": "implementing"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# 034 — Cost & Token Tracking tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_accumulation_across_polls() -> None:
    """tokens_processed accumulates into card_tokens_total across polls."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 1500}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 1500

    # Poll again with more tokens
    svc2 = _Performer(response={"status": "working", "metrics": {"tokens_processed": 2000}})
    result["performer_services"] = {"implementing": svc2}
    result["agent_dispatch"] = {"session_id": "s1"}
    result = await monitor_performer(result)
    assert result["card_tokens_total"] == 3500


@pytest.mark.asyncio
async def test_no_tokens_field_leaves_total_unchanged() -> None:
    """Missing tokens_processed leaves card_tokens_total unchanged."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["card_tokens_total"] = 500

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 500


@pytest.mark.asyncio
async def test_negative_tokens_clamped_to_zero() -> None:
    """Negative tokens_processed is clamped to 0."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": -100}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["card_tokens_total"] = 500

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 500  # unchanged


@pytest.mark.asyncio
async def test_cost_estimate_calculated() -> None:
    """card_cost_estimate is calculated from tokens and config rate."""
    from unittest.mock import MagicMock

    config = MagicMock()
    config.cost_tracking.cost_per_million_tokens = 3.0
    config.cost_tracking.cost_budget_per_card = None
    config.requirement_change_policy = "ignore"

    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 1_000_000}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["config"] = config

    result = await monitor_performer(state)
    assert result["card_cost_estimate"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_budget_exceeded_notification_fires_once() -> None:
    """Budget exceeded dispatches notification exactly once."""
    from unittest.mock import AsyncMock, MagicMock

    config = MagicMock()
    config.cost_tracking.cost_per_million_tokens = 3.0
    config.cost_tracking.cost_budget_per_card = 1.0
    config.requirement_change_policy = "ignore"

    notification_service = AsyncMock()

    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 500_000}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["config"] = config
    state["notification_service"] = notification_service

    # First poll: cost = $1.50, exceeds $1.00 budget
    result = await monitor_performer(state)
    assert result["card_budget_alert_sent"] is True
    assert notification_service.dispatch.await_count == 1

    # Second poll: more tokens, but alert already sent
    svc2 = _Performer(response={"status": "working", "metrics": {"tokens_processed": 200_000}})
    result["performer_services"] = {"implementing": svc2}
    result["agent_dispatch"] = {"session_id": "s1"}
    result = await monitor_performer(result)
    assert notification_service.dispatch.await_count == 1  # no duplicate


@pytest.mark.asyncio
async def test_no_budget_configured_skips_check() -> None:
    """No cost_budget_per_card → no budget check."""
    from unittest.mock import MagicMock

    config = MagicMock()
    config.cost_tracking.cost_per_million_tokens = 3.0
    config.cost_tracking.cost_budget_per_card = None
    config.requirement_change_policy = "ignore"

    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 10_000_000}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["config"] = config

    result = await monitor_performer(state)
    assert result["card_budget_alert_sent"] is False


@pytest.mark.asyncio
async def test_float_tokens_processed_rejected() -> None:
    """Float tokens_processed is rejected (spec requires integers only)."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": 100.5}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["card_tokens_total"] = 0

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 0  # float rejected


@pytest.mark.asyncio
async def test_none_tokens_processed_silently_ignored(caplog) -> None:
    """tokens_processed=None is treated as not-reported, not invalid (no warning)."""
    import logging

    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": None}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["card_tokens_total"] = 250

    with caplog.at_level(logging.WARNING):
        result = await monitor_performer(state)

    assert result["card_tokens_total"] == 250  # unchanged
    assert "invalid_tokens_processed" not in caplog.text
    assert "non_integer_tokens_processed" not in caplog.text


@pytest.mark.asyncio
async def test_non_numeric_tokens_ignored() -> None:
    """Non-numeric tokens_processed leaves card_tokens_total unchanged."""
    state = initial_state()
    svc = _Performer(response={"status": "working", "metrics": {"tokens_processed": "not-a-number"}})
    state["performer_services"] = {"implementing": svc}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["phase"] = "monitoring_performer"
    state["card_tokens_total"] = 500

    result = await monitor_performer(state)
    assert result["card_tokens_total"] == 500  # unchanged


# ---------------------------------------------------------------------------
# Assessor performer — assessment_complete advances lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessment_complete_advances_to_next_stage() -> None:
    """assessment_complete on first stage advances to the next performer role."""
    service = _Performer({
        "status": "assessment_complete",
    })
    state = _make_state(
        service=service,
        stage="assessing",
        sequence=["assessing", "implementing"],
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


@pytest.mark.asyncio
async def test_assessment_complete_is_in_terminal_success_states() -> None:
    """Confirm assessment_complete is recognised as a terminal success status."""
    assert "assessment_complete" in TERMINAL_SUCCESS_STATES


@pytest.mark.asyncio
async def test_assessor_blocked_routes_to_blocked() -> None:
    """When the assessor performer reports blocked, phase should be 'blocked'."""
    service = _Performer({
        "status": "blocked",
        "questions": ["What is the acceptance criteria?"],
    })
    state = _make_state(
        service=service,
        stage="assessing",
        sequence=["assessing", "implementing"],
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["performer_stage"] == "assessing"  # NOT advanced
    assert "What is the acceptance criteria?" in result["open_questions"]


# --- 048: SlotManager service resolution in monitor_performer ---


@pytest.mark.asyncio
async def test_monitor_performer_uses_slot_manager_service() -> None:
    """048: monitor_performer resolves the card's specific transport via
    SlotManager.acquire (idempotent) so status polls go to the correct
    subprocess, not just the primary service."""
    from unittest.mock import AsyncMock, MagicMock

    from coordinare.services.slot_manager import SlotManager

    # Build a SlotManager with a specific service allocated to the card
    specific_service = MagicMock()
    primary_service = MagicMock()
    specific_service.check_status = AsyncMock(
        return_value={"status": "working", "session_id": "sess_A"}
    )
    primary_service.check_status = AsyncMock(
        return_value={"status": "working", "session_id": "sess_A"}
    )

    sm = SlotManager()
    sm.register_pool("implementing", [primary_service, specific_service], max_concurrency=2)
    sm.acquire("implementing", "OTHER_CARD")  # allocates primary (index 0)
    sm.acquire("implementing", "CARD_A")       # allocates specific (index 1)

    state = _make_state(service=primary_service, stage="implementing")
    state["slot_manager"] = sm
    state["current_card"] = {"id": "CARD_A", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "sess_A"}

    await monitor_performer(state)

    # SlotManager should have returned specific_service for CARD_A
    specific_service.check_status.assert_called_once()
    primary_service.check_status.assert_not_called()


# --- 052: T006a — _refresh_backend_ui wiring ---


@pytest.mark.asyncio
async def test_refresh_backend_ui_writes_url_to_state() -> None:
    """T006a-a: URL discovered from agent logs is written to state."""
    from unittest.mock import AsyncMock, patch

    from coordinare.graph.nodes.monitor_performer import _refresh_backend_ui

    service = _Performer({"status": "working"})
    service.get_agent_logs = lambda: ['{"server":"http://127.0.0.1:3000"}']  # type: ignore[attr-defined]

    state: dict = {}

    with patch(
        "coordinare.transport.subprocess_transport._fetch_session_stats",
        new=AsyncMock(return_value=None),
    ):
        await _refresh_backend_ui(state, service, "CARD_1")

    assert state.get("backend_ui_url") == "http://127.0.0.1:3000"


@pytest.mark.asyncio
async def test_refresh_backend_ui_throttles_stats_polling() -> None:
    """T006a-b: Second call within 30 s skips the HTTP stats request."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    from coordinare.graph.nodes.monitor_performer import _refresh_backend_ui

    service = _Performer({"status": "working"})
    service.get_agent_logs = lambda: ['{"server":"http://127.0.0.1:3000"}']  # type: ignore[attr-defined]

    mock_stats = AsyncMock(return_value=None)
    state: dict = {"_backend_stats_fetched_at": datetime.now(UTC)}

    with patch(
        "coordinare.transport.subprocess_transport._fetch_session_stats",
        new=mock_stats,
    ):
        await _refresh_backend_ui(state, service, "CARD_1")

    mock_stats.assert_not_called()


@pytest.mark.asyncio
async def test_teardown_workspace_clears_backend_ui_fields() -> None:
    """T006a-c: _teardown_workspace sets backend_ui_url and session_stats to None."""
    from coordinare.graph.nodes.monitor_performer import _teardown_workspace

    state: dict = {
        "workspace_manager": None,
        "workspace_path": None,
        "workspace_branch": "coordinare/ITEM-1/test",
        "backend_ui_url": "http://127.0.0.1:3000",
        "session_stats": {"title": "fix bug", "files_changed": 2, "lines_added": 5, "lines_removed": 1},
    }

    await _teardown_workspace(state)  # type: ignore[arg-type]

    assert state["backend_ui_url"] is None
    assert state["session_stats"] is None


# ---------------------------------------------------------------------------
# 054: QA freshness failure routes to implementer (T024)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_freshness_failure_routes_to_implementer() -> None:
    """054 T024: qa_failed with freshness type routes to implementer like any qa failure."""
    state = initial_state()
    failures = [
        {
            "file": None, "line": None,
            "message": "Branch is behind latest main — rebase required before QA can pass",
            "type": "freshness",
        }
    ]
    report = {
        "criteria_checked": 3,
        "criteria_passed": 3,
        "qa_freshness_check": {
            "latest_main_sha": "abc1234",
            "branch_head_sha": "def5678",
            "up_to_date": False,
            "detail": "abc1234 is not an ancestor of HEAD",
        },
    }
    svc = _Performer(response={"status": "qa_failed", "failures": failures, "report": report})
    state["performer_services"] = {"qa": svc}
    state["performer_stage"] = "qa"
    state["lifecycle_sequence"] = ["implementing", "qa"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    relay = result.get("relay_feedback") or []
    assert any(f.get("type") == "freshness" for f in relay)


@pytest.mark.asyncio
async def test_qa_freshness_indeterminate_routes_to_implementer() -> None:
    """054 T024: qa_failed with freshness_indeterminate type also routes to implementer."""
    state = initial_state()
    failures = [
        {
            "file": None, "line": None,
            "message": "Branch freshness could not be verified — environment/git failure",
            "type": "freshness_indeterminate",
        }
    ]
    svc = _Performer(response={"status": "qa_failed", "failures": failures})
    state["performer_services"] = {"qa": svc}
    state["performer_stage"] = "qa"
    state["lifecycle_sequence"] = ["implementing", "qa"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_qa_freshness_up_to_date_does_not_route_to_implementer() -> None:
    """054 T024: qa_passed with up-to-date freshness check does not route back to implementer."""
    state = initial_state()
    report = {
        "criteria_checked": 2,
        "criteria_passed": 2,
        "qa_freshness_check": {
            "latest_main_sha": "abc1234",
            "branch_head_sha": "abc1234",
            "up_to_date": True,
            "detail": "branch includes latest main (git merge-base confirmed)",
        },
    }
    svc = _Performer(response={"status": "qa_passed", "report": report})
    state["performer_services"] = {"qa": svc}
    state["performer_stage"] = "qa"
    state["lifecycle_sequence"] = ["implementing", "qa"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_REVIEW"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    # qa_passed must NOT route back to implementing
    assert not (result.get("phase") == "dispatching" and result.get("performer_stage") == "implementing")


# ---------------------------------------------------------------------------
# Spec 064: Closer PR-checks gate (T024-T028)
# ---------------------------------------------------------------------------


def _rollup_payload(
    *,
    pushed_date: str | None = None,
    contexts: list[dict] | None = None,
    bpr_nodes: list[dict] | None = None,
) -> dict:
    if pushed_date is None:
        from datetime import UTC, datetime
        pushed_date = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "repository": {
            "pullRequest": {
                "number": 42,
                "baseRefName": "main",
                "headRefOid": "deadbeef",
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": "deadbeef",
                                "pushedDate": pushed_date,
                                "statusCheckRollup": {
                                    "state": "PENDING",
                                    "contexts": {"nodes": contexts or []},
                                },
                            }
                        }
                    ]
                },
            },
            "branchProtectionRules": {"nodes": bpr_nodes or []},
        }
    }


class _GitHubWithRollup:
    """Mock github service exposing both move_card and _execute."""

    def __init__(self, rollup_payload: dict | None = None, raise_on_execute: bool = False) -> None:
        self.move_calls: list[tuple[str, str]] = []
        self.request_reviewer_calls: list[tuple] = []
        self._payload = rollup_payload
        self._raise = raise_on_execute

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))

    async def request_reviewers(self, owner: str, repo: str, pr_num: int, reviewers: list) -> None:
        self.request_reviewer_calls.append((owner, repo, pr_num, reviewers))

    async def _execute(self, query: str, variables: dict) -> dict:
        if self._raise:
            raise RuntimeError("graphql blew up")
        return self._payload or {}


def _gate_state(github: object, symphony_cfg: object | None = None) -> dict:
    """Build a state that will hit the gate when monitor_performer advances.

    Always wires a symphony_configs entry so the gate is active: legacy
    single-symphony mode (no symphony_configs) deliberately disables the
    gate per `_get_closer_pr_checks_config`.
    """
    state = initial_state()
    service = _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_NODE_42",
    })
    state["performer_services"] = {"implementing": service}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = github
    if symphony_cfg is None:
        from coordinare.config import CloserPrChecksConfig

        class _DefaultSym:
            closer_pr_checks = CloserPrChecksConfig()

        symphony_cfg = _DefaultSym()
    state["current_symphony"] = "default"
    state["symphony_configs"] = {"default": symphony_cfg}
    return state


@pytest.mark.asyncio
async def test_064_gate_forwards_when_required_checks_pass() -> None:
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    gh = _GitHubWithRollup(payload)
    state = _gate_state(gh)

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert ("ITEM_1", "IN_REVIEW") in gh.move_calls
    # On FORWARD, the per-card cache is GC'd as the card hands off to monitoring_pr.
    assert "ITEM_1" not in (result.get("card_checks_state") or {})


@pytest.mark.asyncio
async def test_064_gate_holds_when_required_check_pending() -> None:
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "IN_PROGRESS", "conclusion": None},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    gh = _GitHubWithRollup(payload)
    state = _gate_state(gh)

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert gh.move_calls == []
    assert result["card_checks_state"]["ITEM_1"]["last_decision"] == "HOLD"


@pytest.mark.asyncio
async def test_064_gate_bounces_to_implementer_on_failure() -> None:
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    gh = _GitHubWithRollup(payload)
    state = _gate_state(gh)

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert gh.move_calls == []
    relay = result.get("relay_feedback") or []
    assert relay and "ci/test" in relay[0]["body"]


@pytest.mark.asyncio
async def test_064_gate_disabled_in_legacy_mode_no_symphony() -> None:
    """Legacy single-symphony mode (no symphony_configs) leaves the gate off
    so upgrades don't silently start blocking handoff on red checks.
    """
    state = _gate_state(_GitHubWithRollup(raise_on_execute=True))
    # Tear down the default symphony cfg that `_gate_state` injects.
    state["current_symphony"] = None
    state["symphony_configs"] = {}
    gh = state["github_service"]

    result = await monitor_performer(state)

    assert result["phase"] == "monitoring_pr"
    assert ("ITEM_1", "IN_REVIEW") in gh.move_calls


@pytest.mark.asyncio
async def test_064_gate_disabled_in_config_bypasses() -> None:
    from coordinare.config import CloserPrChecksConfig

    class _Sym:
        closer_pr_checks = CloserPrChecksConfig(enabled=False)

    gh = _GitHubWithRollup(raise_on_execute=True)
    state = _gate_state(gh, symphony_cfg=_Sym())

    result = await monitor_performer(state)

    # Disabled → no rollup query, normal handoff path runs.
    assert result["phase"] == "monitoring_pr"
    assert ("ITEM_1", "IN_REVIEW") in gh.move_calls


class _CountingGitHub(_GitHubWithRollup):
    def __init__(self, rollup_payload: dict) -> None:
        super().__init__(rollup_payload)
        self.execute_calls = 0

    async def _execute(self, query: str, variables: dict) -> dict:
        self.execute_calls += 1
        return await super()._execute(query, variables)


@pytest.mark.asyncio
async def test_064_tick_fast_path_skips_graphql_within_poll_interval() -> None:
    """T034: a second tick within poll_interval_seconds reuses prior HOLD without re-querying."""
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "IN_PROGRESS", "conclusion": None},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    gh = _CountingGitHub(payload)
    state = _gate_state(gh)

    # First tick: HOLD, one query.
    state1 = await monitor_performer(state)
    assert state1["phase"] == "monitoring_performer"
    assert gh.execute_calls == 1

    # Reset performer dispatch so monitor_performer runs again on next tick.
    state1["performer_services"] = {"implementing": _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_NODE_42",
    })}
    state1["agent_dispatch"] = {"session_id": "s2"}

    # Second tick (immediately): fast-path reuse, no new query.
    state2 = await monitor_performer(state1)
    assert state2["phase"] == "monitoring_performer"
    assert gh.execute_calls == 1  # unchanged


@pytest.mark.asyncio
async def test_064_new_head_resets_timeout_clock() -> None:
    """T035: pushing a new HEAD (different headRefOid + pushedDate) restarts the timeout clock."""
    from datetime import UTC, datetime, timedelta

    old_pushed = (datetime.now(UTC) - timedelta(seconds=2000)).isoformat().replace("+00:00", "Z")
    old_payload = _rollup_payload(
        pushed_date=old_pushed,
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "IN_PROGRESS", "conclusion": None},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    # Old head, elapsed > 900s → pending_timeout BOUNCE
    gh = _GitHubWithRollup(old_payload)
    state = _gate_state(gh)
    result1 = await monitor_performer(state)
    assert result1["phase"] == "dispatching"  # BOUNCE on pending_timeout

    # Now simulate new HEAD with fresh pushedDate → HOLD, not BOUNCE
    fresh_pushed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    new_payload = _rollup_payload(
        pushed_date=fresh_pushed,
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "IN_PROGRESS", "conclusion": None},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    new_payload["repository"]["pullRequest"]["headRefOid"] = "newhead1"
    new_payload["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["oid"] = "newhead1"
    gh2 = _GitHubWithRollup(new_payload)
    state2 = _gate_state(gh2)
    result2 = await monitor_performer(state2)
    assert result2["phase"] == "monitoring_performer"  # HOLD, not BOUNCE


@pytest.mark.asyncio
async def test_064_hold_does_not_redispatch_closer() -> None:
    """T036: HOLD keeps phase=monitoring_performer; closer is NOT re-dispatched."""
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "IN_PROGRESS", "conclusion": None},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    gh = _GitHubWithRollup(payload)
    state = _gate_state(gh)
    result = await monitor_performer(state)
    # HOLD = stay in monitoring_performer, not "dispatching" (which would re-dispatch closer)
    assert result["phase"] == "monitoring_performer"
    assert result.get("performer_stage") != "closer"


@pytest.mark.asyncio
async def test_064_gate_fail_open_on_infra_error() -> None:
    gh = _GitHubWithRollup(raise_on_execute=True)
    state = _gate_state(gh)

    result = await monitor_performer(state)

    # Default config has fail_open_on_error=True → FORWARD despite error.
    assert result["phase"] == "monitoring_pr"
    assert ("ITEM_1", "IN_REVIEW") in gh.move_calls


@pytest.mark.asyncio
async def test_064_gate_fail_closed_on_infra_error_bounces() -> None:
    """fail_open_on_error=False: GraphQL failure routes back to implementing."""
    from coordinare.config import CloserPrChecksConfig

    class _Sym:
        closer_pr_checks = CloserPrChecksConfig(fail_open_on_error=False)

    gh = _GitHubWithRollup(raise_on_execute=True)
    state = _gate_state(gh, symphony_cfg=_Sym())

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert gh.move_calls == []
    relay = result.get("relay_feedback") or []
    assert relay and "failed to query GitHub" in relay[0]["body"]


@pytest.mark.asyncio
async def test_064_gate_block_mode_bounces_when_bp_unreadable() -> None:
    """treat_unknown_required_as='block': missing BP block bounces to implementer."""
    from coordinare.config import CloserPrChecksConfig

    class _Sym:
        closer_pr_checks = CloserPrChecksConfig(treat_unknown_required_as="block")

    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    )
    # Signal unreadable BP by setting branchProtectionRules to None.
    payload["repository"]["branchProtectionRules"] = None
    gh = _GitHubWithRollup(payload)
    state = _gate_state(gh, symphony_cfg=_Sym())

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert gh.move_calls == []


@pytest.mark.asyncio
async def test_064_fast_path_expires_after_poll_interval() -> None:
    """After poll_interval_seconds elapses, the next tick must re-query GraphQL."""
    from datetime import UTC, datetime, timedelta

    from coordinare.config import CloserPrChecksConfig

    class _Sym:
        closer_pr_checks = CloserPrChecksConfig(poll_interval_seconds=5)

    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "IN_PROGRESS", "conclusion": None},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    gh = _CountingGitHub(payload)
    state = _gate_state(gh, symphony_cfg=_Sym())

    state1 = await monitor_performer(state)
    assert state1["phase"] == "monitoring_performer"
    assert gh.execute_calls == 1

    # Backdate the cache entry so it falls outside poll_interval_seconds=5.
    ccs = dict(state1["card_checks_state"])
    entry = dict(ccs["ITEM_1"])
    entry["last_polled_at"] = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    ccs["ITEM_1"] = entry
    state1["card_checks_state"] = ccs

    state1["performer_services"] = {"implementing": _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_NODE_42",
    })}
    state1["agent_dispatch"] = {"session_id": "s3"}

    state2 = await monitor_performer(state1)
    assert state2["phase"] == "monitoring_performer"
    assert gh.execute_calls == 2  # re-queried
