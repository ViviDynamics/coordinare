"""Reviewer instructions must survive reassessment before implementation."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from performer import main
from performer.backends.base import BackendStatus
from performer.backends.claude_code import _build_task_prompt
from performer.models import Performance, Score, Stand
from performer.protocol import PerformerMessage

from coordinare.daemon import _persist_one_session, _restored_session_dict
from coordinare.graph.nodes.dispatch_performer import (
    _base_card_context,
    _feedback_for_dispatch,
    _finalise_success,
    _inject_documentation_findings,
)
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state
from coordinare.state_store import PersistedSession, WorkflowSnapshot

MARKER = "SYNTHETIC_TRY004_REVIEW_FIX"
TEXT = (
    MARKER
    + ": implementation bug: remove TRY004 suppression; preserve ValueError for bool by using type(value) is bool."
)
DESIGN = "The design/architecture pattern must retain the public exception contract."
OLD = "SYNTHETIC_PRIOR_YES"


class Service:
    def __init__(self, response):
        self.response = response

    async def check_status(self, session_id, **kwargs):
        return copy.deepcopy(self.response)


class Backend:
    def __init__(self, output):
        self.output = output

    def get_status(self):
        return BackendStatus(state="done", output=json.dumps(self.output))


async def context(state, stage):
    card = state["current_card"]
    ctx, role = _base_card_context(state, card, card["id"], stage)
    await _inject_documentation_findings(
        state,
        {"performer_stage": stage, "card": card, "card_context": ctx, "role": role},
    )
    return ctx


async def routed(single=False):
    state = initial_state()
    comments = [{"file": "module.py", "line": 129, "body": TEXT}]
    if not single:
        comments.append({"file": "module.py", "line": 130, "body": DESIGN})
    state.update(
        performer_services={
            "reviewing": Service({"status": "changes_requested", "comments": comments}),
        },
        performer_stage="reviewing",
        lifecycle_sequence=["assessing", "implementing", "reviewing"],
        current_card={
            "id": "SYNTHETIC_CARD",
            "status": "IN_PROGRESS",
            "title": "Synthetic bool rejection",
            "pr_url": "https://github.com/example/sample/pull/40",
            "pr_number": 40,
            "head_after": "synthetic-current-head",
            "pushed_branch": "synthetic/held",
        },
        agent_dispatch={"session_id": "synthetic-reviewer"},
        config=SimpleNamespace(max_feedback_cycles=5),
        card_clarifications=[{"question": "Continue?", "answer": OLD, "source": "clarification"}],
    )
    result = await monitor_performer(state)
    assert result["performer_stage"] == ("implementing" if single else "assessing")
    assert MARKER in json.dumps(result["relay_feedback"])
    return result


async def chain(mode):
    state = await routed(single=mode == "direct")
    if mode == "direct":
        ctx = await context(state, "implementing")
        prompt = _build_task_prompt(
            Score.model_validate(
                {
                    **ctx,
                    "repo_url": "https://github.com/example/sample",
                    "branch": "synthetic/held",
                },
            ),
        )
        assert MARKER in prompt and OLD in prompt
        print(
            json.dumps(
                {
                    "case": mode,
                    "original_review_in_payload": True,
                    "original_review_in_prompt": True,
                    "prior_answer_in_prompt": True,
                },
            ),
        )
        return
    assess_ctx = await context(state, "assessing")
    assert MARKER in json.dumps(assess_ctx["relay_feedback"])
    await _finalise_success(
        state,
        {"session_id": "synthetic-assessor", "performer_id": "assessing"},
        {"card_context": assess_ctx, "performer_stage": "assessing", "card": state["current_card"]},
    )
    assert not state["relay_feedback"] and state["dispatched_feedback"]["stage"] == "assessing"
    assert MARKER in json.dumps(_feedback_for_dispatch(state, "assessing"))
    assert not _feedback_for_dispatch(state, "implementing")
    if mode == "restore":
        persisted = PersistedSession.model_validate_json(
            _persist_one_session("SYNTHETIC_CARD", state).model_dump_json(),
        )
        snap = WorkflowSnapshot(
            snapshot_at=datetime.now(UTC),
            phase="monitoring_performer",
            active_card_id="SYNTHETIC_CARD",
            lifecycle_sequence=state["lifecycle_sequence"],
        )
        restored = _restored_session_dict(
            "SYNTHETIC_CARD",
            persisted,
            snap,
            copy.deepcopy(state["current_card"]),
        )
        state.update(restored)
        assert MARKER in json.dumps(_feedback_for_dispatch(state, "assessing"))
    if mode == "neighbors":
        assert MARKER in json.dumps(await context(state, "assessing"))
        assert MARKER not in json.dumps(await context(state, "implementing"))
        saved = copy.deepcopy(state["dispatched_feedback"])
        returned = _feedback_for_dispatch(state, "assessing")
        returned[0]["body"] = "modified-copy"
        assert state["dispatched_feedback"] == saved
        print(
            json.dumps(
                {
                    "case": mode,
                    "replacement_assessor_has_full_review": True,
                    "wrong_stage_isolation": True,
                    "deep_copy_isolation": True,
                    "ledger_open": len(state["feedback_ledger"]),
                },
            ),
        )
        return
    output = {"sufficient": True, "questions": []}
    if mode == "workflow":
        output = {
            "assessment": {
                "ready": True,
                "questions": [],
                "goal": TEXT,
                "expected_behavior": DESIGN,
                "verdict": "work",
            },
        }
    perf = Performance(
        session_id="synthetic-assessor",
        stand=Stand(path=Path("/synthetic-never-created"), branch="synthetic/held"),
        score=Score.model_validate(
            {
                **assess_ctx,
                "repo_url": "https://github.com/example/sample",
                "branch": "synthetic/held",
            },
        ),
        backend=Backend(output),
        role="assessing",
        state="working",
    )
    with patch.object(main, "commit_file", new=AsyncMock()) as commit:
        wire = await main.handle_status(
            PerformerMessage(action="status", session_id=perf.session_id),
            perf,
        )
        assert wire.status == "assessment_complete"
        assert commit.await_count == (0 if mode == "workflow" else 1)
    state["performer_services"] = {"assessing": Service(json.loads(wire.model_dump_json()))}
    state = await monitor_performer(state)
    assert state["performer_stage"] == "implementing" and state["phase"] == "dispatching"
    impl_ctx = await context(state, "implementing")
    score = Score.model_validate(
        {**impl_ctx, "repo_url": "https://github.com/example/sample", "branch": "synthetic/held"},
    )
    prompt = _build_task_prompt(score)
    evidence = {
        "case": mode,
        "native_wire_status": wire.status,
        "original_review_in_assessor_payload": True,
        "original_review_in_implementer_payload": MARKER in json.dumps(impl_ctx),
        "original_review_in_implementer_prompt": MARKER in prompt,
        "prior_answer_in_prompt": OLD in prompt,
        "batch_after_assessor": state["dispatched_feedback"],
        "relay_after_assessor": state["relay_feedback"],
        "ledger_open": sum(x.get("disposition") == "open" for x in state["feedback_ledger"]),
        "review_findings": state.get("review_findings"),
        "pr_head_unchanged": state["current_card"].get("head_after") == "synthetic-current-head",
        "pr_branch_unchanged": state["current_card"].get("pushed_branch") == "synthetic/held",
        "assessment_echoes_review": MARKER in json.dumps(state.get("assessment")),
    }
    assert evidence["prior_answer_in_prompt"] and evidence["ledger_open"] == 2
    assert evidence["pr_head_unchanged"] and evidence["pr_branch_unchanged"]
    print(json.dumps(evidence), flush=True)
    assert MARKER in prompt, (
        "Actionable original reviewer feedback must reach the implementer after a successful assessor detour"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["handoff", "restore", "workflow", "direct", "neighbors"])
async def test_reviewer_feedback_reaches_implementer_after_reassessment(mode):
    await chain(mode)


@pytest.mark.asyncio
async def test_correction_survives_intermediate_role_and_is_consumed_by_implementer():
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    state = await routed()
    state["lifecycle_sequence"] = ["assessing", "architecting", "implementing", "reviewing"]
    original = copy.deepcopy(state["relay_feedback"])
    for stage, following in [("assessing", "architecting"), ("architecting", "implementing")]:
        ctx = await context(state, stage)
        await _finalise_success(
            state,
            {"session_id": f"{stage}-session", "performer_id": stage},
            {"card_context": ctx, "performer_stage": stage, "card": state["current_card"]},
        )
        state.update(_advance_stage(state, {"status": "complete"}))
        assert state["performer_stage"] == following
        assert _feedback_for_dispatch(state, following) == original
    ctx = await context(state, "implementing")
    await _finalise_success(
        state,
        {"session_id": "impl-session", "performer_id": "implementing"},
        {"card_context": ctx, "performer_stage": "implementing", "card": state["current_card"]},
    )
    state.update(_advance_stage(state, {"status": "complete"}))
    assert state["performer_stage"] == "reviewing"
    assert not _feedback_for_dispatch(state, "reviewing")


@pytest.mark.parametrize("owning_stage,marked", [("assessing", False), ("reviewing", True)])
def test_success_does_not_forward_unmarked_or_wrong_owner_feedback(owning_stage, marked):
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    item = {"id": "fb-1", "raiser": "reviewing", "body": "Scoped feedback"}
    if marked:
        item["delivery_stage"] = "implementing"
    state = initial_state()
    state.update(
        performer_stage="assessing",
        lifecycle_sequence=["assessing", "implementing"],
        dispatched_feedback={"stage": owning_stage, "items": [item]},
        relay_feedback=[],
    )
    state.update(_advance_stage(state, {"status": "assessment_complete"}))
    assert not _feedback_for_dispatch(state, "implementing")


@pytest.mark.asyncio
async def test_handoff_preserves_queued_feedback_and_copies_correction():
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    state = await routed()
    ctx = await context(state, "assessing")
    await _finalise_success(
        state,
        {"session_id": "assessor", "performer_id": "assessing"},
        {"card_context": ctx, "performer_stage": "assessing", "card": state["current_card"]},
    )
    accepted = copy.deepcopy(state["dispatched_feedback"])
    queued = {"body": "New queued clarification"}
    state["relay_feedback"] = [queued]
    state.update(_advance_stage(state, {"status": "assessment_complete"}))
    feedback = _feedback_for_dispatch(state, "implementing")
    assert queued in feedback
    assert all(item in feedback for item in accepted["items"])
    feedback[1]["body"] = "Mutated payload"
    assert accepted["items"][0]["body"] != "Mutated payload"
    assert state["relay_feedback"][1]["body"] != "Mutated payload"


@pytest.mark.asyncio
async def test_correction_restores_after_handoff_and_replays_only_to_implementer():
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    state = await routed()
    context_assessing = await context(state, "assessing")
    original = copy.deepcopy(context_assessing["relay_feedback"])
    ledger = copy.deepcopy(state["feedback_ledger"])
    await _finalise_success(
        state,
        {"session_id": "assessor", "performer_id": "assessing"},
        {
            "card_context": context_assessing,
            "performer_stage": "assessing",
            "card": state["current_card"],
        },
    )
    state.update(_advance_stage(state, {"status": "assessment_complete"}))
    persisted = PersistedSession.model_validate_json(
        _persist_one_session("SYNTHETIC_CARD", state).model_dump_json(),
    )
    snapshot = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="dispatching",
        active_card_id="SYNTHETIC_CARD",
        lifecycle_sequence=state["lifecycle_sequence"],
    )
    state.update(
        _restored_session_dict(
            "SYNTHETIC_CARD",
            persisted,
            snapshot,
            copy.deepcopy(state["current_card"]),
        ),
    )
    assert _feedback_for_dispatch(state, "implementing") == original
    context_implementing = await context(state, "implementing")
    await _finalise_success(
        state,
        {"session_id": "impl", "performer_id": "implementing"},
        {
            "card_context": context_implementing,
            "performer_stage": "implementing",
            "card": state["current_card"],
        },
    )
    assert _feedback_for_dispatch(state, "implementing") == original
    assert not _feedback_for_dispatch(state, "reviewing")
    assert state["feedback_ledger"] == ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["skip", "restart"])
async def test_assessor_control_preserves_correction_for_implementer(action):
    from coordinare.graph.nodes.monitor.verdict import _apply_pending_override

    state = await routed()
    assessor_context = await context(state, "assessing")
    original = copy.deepcopy(assessor_context["relay_feedback"])
    await _finalise_success(
        state,
        {"session_id": "assessor", "performer_id": "assessing"},
        {
            "card_context": assessor_context,
            "performer_stage": "assessing",
            "card": state["current_card"],
        },
    )
    state["pending_override"] = {
        "action": action,
        "target_stage": "implementing",
        "control_id": "human-control",
    }
    assert _apply_pending_override(state) is state
    assert state["performer_stage"] == "implementing"
    assert state["consumed_control_id"] == "human-control"
    implementer_context = await context(state, "implementing")
    assert implementer_context.get("relay_feedback") == original
    prompt = _build_task_prompt(
        Score.model_validate(
            {
                **implementer_context,
                "repo_url": "https://github.com/example/sample",
                "branch": "synthetic/held",
            },
        ),
    )
    assert MARKER in prompt
    assert OLD in prompt
    if action == "restart":
        assert state["pending_override"]["applied"] is True
        assert state["override_forced_dispatch"] == "implementing"
    else:
        assert state["pending_override"] is None
