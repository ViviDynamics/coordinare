from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.daemon import _persist_one_session, _restored_session_dict
from coordinare.graph.nodes.dispatch_performer import _base_card_context, _finalise_success
from coordinare.graph.state import initial_state
from coordinare.session import session_to_state, state_to_session
from coordinare.state_store import PersistedSession, WorkflowSnapshot


@pytest.mark.asyncio
async def test_dispatched_feedback_reaches_replacement_after_snapshot_restore() -> None:
    state = initial_state()
    card = {"id": "card19", "status": "IN_REVIEW"}
    feedback = [{"id": "review1", "body": "Add the single-word regression.",
                 "comments": [{"body": "Preserve case", "path": "tests/test_sample.py", "line": 128}]}]
    state.update(current_card=card, performer_stage="implementing", relay_feedback=feedback)
    payload, _ = _base_card_context(state, card, "card19", "implementing")
    await _finalise_success(state, {"session_id": "original"}, {
        "card_context": payload, "performer_stage": "implementing", "card": card,
    })
    assert state["relay_feedback"] == []
    persisted = _persist_one_session("card19", state_to_session(state))
    persisted = PersistedSession.model_validate_json(persisted.model_dump_json())
    restored = _restored_session_dict("card19", persisted, WorkflowSnapshot(
        snapshot_at=datetime.now(UTC), phase="monitoring_performer",
    ), card)
    replacement = initial_state()
    session_to_state(restored, replacement)
    replacement["phase"] = "dispatching"
    payload, _ = _base_card_context(replacement, card, "card19", "implementing")
    assert payload.get("relay_feedback") == feedback


@pytest.mark.asyncio
async def test_replacement_preserves_original_batch_and_new_queued_feedback() -> None:
    state = initial_state()
    original = {"id": "old", "body": "Original request"}
    queued = {"id": "new", "body": "Followup request"}
    state.update(performer_stage="implementing", dispatched_feedback={
        "stage": "implementing", "items": [original],
    }, relay_feedback=[queued])
    payload, _ = _base_card_context(state, {}, "card", "implementing")
    assert payload["relay_feedback"] == [original, queued]
    await _finalise_success(state, {"session_id": "replacement"}, {
        "card_context": payload, "performer_stage": "implementing", "card": {},
    })
    assert state["dispatched_feedback"]["items"] == [original, queued]
    assert state["relay_feedback"] == []


def test_completed_feedback_does_not_leak_to_next_stage_or_sibling() -> None:
    from coordinare.graph.nodes.monitor.verdict import _advance_stage
    from coordinare.graph.state import _retire_active_session

    state = initial_state()
    state.update(performer_stage="implementing", lifecycle_sequence=["implementing", "reviewing"],
                 dispatched_feedback={"stage": "implementing", "items": [{"body": "Fix it"}]})
    payload, _ = _base_card_context(state, {}, "card", "reviewing")
    assert "relay_feedback" not in payload
    state.update(_advance_stage(state, {"status": "done"}))
    assert not state.get("dispatched_feedback")
    state["dispatched_feedback"] = {"stage": "reviewing", "items": [{"body": "Review it"}]}
    session_to_state({"current_card": {"id": "legacy"}}, state)
    assert not state.get("dispatched_feedback")
    state["dispatched_feedback"] = {"stage": "implementing", "items": [{"body": "Old"}]}
    _retire_active_session(state)
    assert not state.get("dispatched_feedback")
    assert not PersistedSession(card_id="legacy").dispatched_feedback


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["reviewing", "documenting"])
async def test_unfinished_feedback_bypasses_cached_stage_skip(stage, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.dispatch_performer import (
        _documenting_gate_skip,
        _verdict_cache_check,
    )

    state = initial_state()
    state.update(performer_stage=stage, dispatched_feedback={"stage": stage, "items": [{"body": "Do this"}]})
    card = {"id": "card"}
    if stage == "reviewing":
        state["stage_verdicts"] = {stage: {"head_sha": "head", "verdict": "approved"}}
        state["github_service"] = AsyncMock()
        state["github_service"].check_mergeability.return_value = {"head_ref_oid": "head"}
        card["pr_node_id"] = "PR1"
        skipped, _ = await _verdict_cache_check(state, card, stage)
        assert not skipped
    else:
        monkeypatch.setattr("coordinare.graph.nodes.dispatch_performer._documenting_sha_gate", AsyncMock(return_value=(True, "no changes", 0)))
        assert await _documenting_gate_skip(state, {"performer_stage": stage, "card_id": "card", "card": card, "live_head": "head"}) is None


@pytest.mark.asyncio
async def test_replacement_delivers_feedback_to_service_without_aliasing_state() -> None:
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.dispatch_performer import _execute_dispatch

    state = initial_state()
    batch = {"stage": "implementing", "items": [{"id": "review", "body": "Fix", "comments": [{"path": "test.py", "line": 3, "body": "Cover this"}]}]}
    state.update(performer_stage="implementing", dispatched_feedback=batch)
    payload, _ = _base_card_context(state, {}, "card", "implementing")
    service = AsyncMock()
    service.dispatch_card.return_value = {"session_id": "replacement"}
    await _execute_dispatch(state, {"card_context": payload, "card_id": "card", "card": {},
                                   "board_provider": AsyncMock(), "service": service,
                                   "performer_stage": "implementing"}, None, lambda: None)
    delivered = service.dispatch_card.call_args.args[0]["relay_feedback"]
    assert delivered == batch["items"]
    delivered[0]["comments"][0]["body"] = "Mutated by service"
    assert state["dispatched_feedback"]["items"][0]["comments"][0]["body"] == "Cover this"


def test_skipped_stage_does_not_acknowledge_unfinished_feedback() -> None:
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    state = initial_state()
    state.update(performer_stage="implementing", dispatched_feedback={"stage": "implementing", "items": [{"body": "Fix"}]})
    _advance_stage(state, None)
    assert state["dispatched_feedback"]["items"] == [{"body": "Fix"}]


@pytest.mark.asyncio
async def test_ci_hold_then_pass_acknowledges_completed_feedback(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.monitor.body import _build_monitor_ctx, _phase_ephemeral_gate

    state = initial_state()
    state.update(current_card={"id": "card", "pr_url": "https://github.com/example/sample/pull/1", "pr_node_id": "PR1"},
                 performer_stage="implementing", lifecycle_sequence=["implementing"],
                 phase="monitoring_performer", dispatched_feedback={"stage": "implementing", "items": [{"body": "Already done"}]})
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._implementer_session_gone", lambda _: True)
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._get_ci_gate_config", lambda _: SimpleNamespace(enabled=True))
    gate = AsyncMock(side_effect=[({}, True), ({}, False)])
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_ci_gate", gate)
    await _phase_ephemeral_gate(state, _build_monitor_ctx(state))
    assert state["dispatched_feedback"]
    await _phase_ephemeral_gate(state, _build_monitor_ctx(state))
    assert state["phase"] == "monitoring_pr"
    assert not state.get("dispatched_feedback")


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.asyncio
async def test_lint_bounce_retains_feedback_for_replacement(deferred, monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.monitor.body import _build_monitor_ctx, _phase_ephemeral_gate
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    state = initial_state()
    batch = {"stage": "implementing", "items": [{"body": "Keep this request"}]}
    state.update(current_card={"id": "card", "pr_url": "https://github.com/example/sample/pull/1", "pr_node_id": "PR1"},
                 performer_stage="implementing", lifecycle_sequence=["implementing"],
                 phase="monitoring_performer", dispatched_feedback=batch)
    monkeypatch.setattr("coordinare.graph.nodes.monitor.verdict._ci_lint_gate", lambda *_: {
        "phase": "dispatching", "performer_stage": "implementing",
        "relay_feedback": [{"body": "Lint failed"}],
    })
    if deferred:
        monkeypatch.setattr("coordinare.graph.nodes.monitor.body._implementer_session_gone", lambda _: True)
        monkeypatch.setattr("coordinare.graph.nodes.monitor.body._get_ci_gate_config", lambda _: SimpleNamespace(enabled=True))
        monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_ci_gate", AsyncMock(return_value=({}, False)))
        await _phase_ephemeral_gate(state, _build_monitor_ctx(state))
    else:
        state.update(_advance_stage(state, {"status": "done"}))
    assert state["phase"] == "dispatching"
    payload, _ = _base_card_context(state, state["current_card"], "card", "implementing")
    assert batch["items"][0] in payload["relay_feedback"]


def test_final_reviewer_lint_bounce_relays_original_request_to_implementer(monkeypatch) -> None:
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    state = initial_state()
    request = {"id": "style-request", "body": "Fix naming in the new helper"}
    state.update(performer_stage="reviewing", lifecycle_sequence=["implementing", "reviewing"],
                 dispatched_feedback={"stage": "reviewing", "items": [request]})
    monkeypatch.setattr("coordinare.graph.nodes.monitor.verdict._ci_lint_gate", lambda *_: {
        "phase": "dispatching", "performer_stage": "implementing",
        "relay_feedback": [{"body": "Lint failed"}],
    })
    state.update(_advance_stage(state, {"status": "done"}))
    payload, _ = _base_card_context(state, {}, "card", "implementing")
    assert request in payload["relay_feedback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["implementing", "reviewing"])
@pytest.mark.parametrize("verdict", ["hold", "bounce", "pass", "missing_pr"])
async def test_final_stage_feedback_waits_for_pr_checks_gate(stage, verdict, monkeypatch):
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.monitor.body import (
        _build_monitor_ctx,
        _phase_terminal_success_s3,
        _phase_terminal_success_s4_s1,
    )

    state = initial_state()
    request = {"id": "original", "body": "Preserve this request"}
    card = {"id": "card", "pr_url": "https://github.com/o/r/pull/1", "pr_node_id": "PR1"}
    if verdict == "missing_pr":
        card.pop("pr_node_id")
    state.update(current_card=card, performer_stage=stage, lifecycle_sequence=[stage],
                 phase="monitoring_performer", dispatched_feedback={"stage": stage, "items": [request]})
    ctx = _build_monitor_ctx(state)
    ctx.status = {"status": "done", "marker": "review_approved", **card}
    ctx.marker = "review_approved"
    gate_updates = {
        "hold": {"phase": "monitoring_performer"},
        "bounce": {"phase": "dispatching", "performer_stage": "implementing", "relay_feedback": [{"body": "CI failed"}]},
        "pass": {}, "missing_pr": {},
    }[verdict]
    monkeypatch.setattr("coordinare.graph.nodes.monitor.verdict._ci_lint_gate", lambda *_: None)
    gate = AsyncMock(return_value=(gate_updates, verdict in {"hold", "bounce"}))
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_pr_checks_gate", gate)
    await _phase_terminal_success_s3(state, ctx)
    assert state["dispatched_feedback"]["items"] == [request]
    await _phase_terminal_success_s4_s1(state, ctx)
    if verdict == "pass":
        assert not state["dispatched_feedback"]
    elif verdict == "bounce":
        payload, _ = _base_card_context(state, card, "card", "implementing")
        assert request in payload["relay_feedback"]
        assert {"body": "CI failed"} in payload["relay_feedback"]
        from coordinare.graph.nodes.dispatch_performer import _execute_dispatch

        service = AsyncMock()
        service.dispatch_card.return_value = {"session_id": "replacement"}
        await _execute_dispatch(state, {
            "card_context": payload, "card_id": "card", "card": card,
            "board_provider": AsyncMock(), "service": service,
            "performer_stage": "implementing",
        }, None, lambda: None)
        assert request in service.dispatch_card.call_args.args[0]["relay_feedback"]
    else:
        assert state["dispatched_feedback"]["items"] == [request]
    if verdict != "pass":
        persisted = _persist_one_session("card", state_to_session(state))
        restored = _restored_session_dict("card", persisted, WorkflowSnapshot(
            snapshot_at=datetime.now(UTC), phase=state["phase"],
        ), card)
        restarted = initial_state()
        session_to_state(restored, restarted)
        replacement_stage = "implementing" if verdict == "bounce" else stage
        replay, _ = _base_card_context(restarted, card, "card", replacement_stage)
        assert request in replay["relay_feedback"]
    if verdict == "missing_pr":
        assert state["phase"] == "system_error"
        gate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["hold", "bounce", "pass"])
async def test_deferred_ephemeral_final_feedback_waits_for_pr_checks(verdict, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.monitor.body import _build_monitor_ctx, _phase_ephemeral_gate

    state = initial_state()
    request = {"id": "original", "body": "Preserve this request"}
    state.update(current_card={"id": "card", "pr_url": "https://github.com/o/r/pull/1", "pr_node_id": "PR1"},
                 performer_stage="implementing", lifecycle_sequence=["implementing"],
                 phase="monitoring_performer", dispatched_feedback={"stage": "implementing", "items": [request]})
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._implementer_session_gone", lambda _: True)
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._get_ci_gate_config", lambda _: SimpleNamespace(enabled=True))
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_ci_gate", AsyncMock(return_value=({}, False)))
    monkeypatch.setattr("coordinare.graph.nodes.monitor.verdict._ci_lint_gate", lambda *_: None)
    gate_updates = ({"phase": "dispatching", "performer_stage": "implementing", "relay_feedback": [{"body": "CI failed"}]}
                    if verdict == "bounce" else {"phase": "monitoring_performer"} if verdict == "hold" else {})
    gate = AsyncMock(return_value=(gate_updates, verdict != "pass"))
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_pr_checks_gate", gate)
    await _phase_ephemeral_gate(state, _build_monitor_ctx(state))
    gate.assert_awaited_once()
    if verdict == "pass":
        assert state["phase"] == "monitoring_pr"
        assert not state["dispatched_feedback"]
    else:
        assert state["dispatched_feedback"]["items"] == [request]
        assert state["phase"] == ("dispatching" if verdict == "bounce" else "monitoring_performer")


@pytest.mark.asyncio
@pytest.mark.parametrize('feedback_stage', ['implementing', 'reviewing'])
async def test_restored_feedback_bypasses_thrash_guard_only_for_own_stage(feedback_stage):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.dispatch_performer import _pre_dispatch_rebase_guard

    state = initial_state()
    card = {'id': 'card', 'pr_url': 'https://github.com/acme/repo/pull/1', 'pr_node_id': 'PR1'}
    state.update(current_card=card, phase='dispatching', performer_stage='implementing',
                 dispatched_feedback={'stage': feedback_stage, 'items': [{'body': 'Resolve this requested conflict'}]},
                 last_rebase_attempt={'main_sha': 'main', 'head_sha': 'head', 'outcome': 'blocked'})
    record = _persist_one_session('card', state_to_session(state))
    record = PersistedSession.model_validate_json(record.model_dump_json())
    restored = _restored_session_dict('card', record, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase='dispatching'), card)
    session_to_state(restored, state)
    github = SimpleNamespace(_current_token=AsyncMock(return_value='token'),
                             check_mergeability=AsyncMock(return_value={'mergeable_raw': 'CONFLICTING', 'merge_state_status': 'DIRTY', 'head_ref_oid': 'head'}))
    state.update(current_card=card, github_service=github, config=SimpleNamespace(github_org='acme', project_name='repo'), last_known_main_sha='main')
    assert await _pre_dispatch_rebase_guard(state, 'card') is (feedback_stage == 'implementing')
    assert github.check_mergeability.await_count == int(feedback_stage != 'implementing')
    assert state['dispatched_feedback']['items'] == [{'body': 'Resolve this requested conflict'}]
    # Completion acknowledgement restores the ordinary anti-thrash hold.
    state['dispatched_feedback'] = {}
    assert await _pre_dispatch_rebase_guard(state, 'card') is False


@pytest.mark.asyncio
@pytest.mark.parametrize('feedback_stage', ['architecting', 'reviewing'])
async def test_unfinished_architect_feedback_cannot_be_skipped_by_blueprint_reuse(feedback_stage):
    from coordinare.graph.nodes.dispatch_performer import (
        _stage_advance_gates,
        stamp_blueprint_signature,
    )

    state = initial_state()
    card = {'id': 'card', 'title': 'Existing requirements'}
    state.update(current_card=card, performer_stage='architecting', lifecycle_sequence=['architecting', 'implementing'],
                 blueprint={'milestones': [{'title': 'Original plan'}]},
                 dispatched_feedback={'stage': feedback_stage, 'items': [{'body': 'Amend the architecture plan'}]})
    stamp_blueprint_signature(state)
    record = _persist_one_session('card', state_to_session(state))
    record = PersistedSession.model_validate_json(record.model_dump_json())
    restored = _restored_session_dict('card', record, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase='dispatching'), card)
    session_to_state(restored, state)
    state['current_card'] = card
    result = await _stage_advance_gates(state, {'performer_stage': 'architecting', 'card_id': 'card', 'card': card})
    assert (result is None) is (feedback_stage == 'architecting')
    assert state['performer_stage'] == ('architecting' if feedback_stage == 'architecting' else 'implementing')
