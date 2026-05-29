"""Coverage tests for coordinare/graph/nodes/notify.py.

Targets line 54 — the pr_url branch:
    if card.get("pr_url"):
        payload["pr_url"] = str(card["pr_url"])
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.nodes.notify import (
    PERSONA_SCOPE_ROLLUP_MARKER,
    _emit_persona_scope_rollup,
    _format_persona_scope_rollup,
    _persona_scope_signature,
    notify,
)
from coordinare.graph.state import initial_state
from coordinare.models.notification import NotificationEvent

_SCOPE = {
    "personas": {
        "implementer": {"depth": "deep", "focus": "auth | core", "overrides": ["max_turns=5"]},
        "reviewer": {"depth": "shallow"},
    },
    "cycle_index": 3,
    "classifier_model": "qwen3-8b",
    "head_sha": "abcdef1234567890",
}


class TestPersonaScopeRollup:
    def test_format_renders_marker_table_and_meta(self) -> None:
        out = _format_persona_scope_rollup(_SCOPE)
        assert out.startswith(PERSONA_SCOPE_ROLLUP_MARKER)
        assert "| Persona | Depth | Focus | Overrides |" in out
        assert "`implementer`" in out and "`reviewer`" in out
        assert "auth \\| core" in out  # pipe escaped for markdown table
        assert "max_turns=5" in out
        assert "cycle `3`" in out and "classifier `qwen3-8b`" in out
        assert "head `abcdef1`" in out  # truncated to 7

    def test_format_handles_empty_meta_and_missing_fields(self) -> None:
        out = _format_persona_scope_rollup({"personas": {"qa": {}}})
        assert PERSONA_SCOPE_ROLLUP_MARKER in out
        assert "`qa`" in out
        assert "—" in out  # focus/overrides fall back to em-dash

    def test_signature_stable_and_differs_on_change(self) -> None:
        sig1 = _persona_scope_signature(_SCOPE)
        sig2 = _persona_scope_signature(dict(_SCOPE))
        assert sig1 == sig2
        changed = {**_SCOPE, "personas": {"implementer": {"depth": "shallow"}}}
        assert _persona_scope_signature(changed) != sig1

    @pytest.mark.asyncio
    async def test_emit_posts_comment_and_stamps_signature(self) -> None:
        github = MagicMock()
        github.add_comment = AsyncMock()
        state = {"github_service": github}
        session: dict = {"persona_scope": _SCOPE}
        card = {"id": "c1", "pr_node_id": "PR_1"}
        await _emit_persona_scope_rollup(state, session, card)
        github.add_comment.assert_awaited_once()
        assert session["persona_scope_rollup_signature"] == _persona_scope_signature(_SCOPE)
        # Idempotent: same signature => no second post.
        await _emit_persona_scope_rollup(state, session, card)
        assert github.add_comment.await_count == 1

    @pytest.mark.asyncio
    async def test_emit_noops_without_scope_github_or_pr(self) -> None:
        gh = MagicMock()
        gh.add_comment = AsyncMock()
        # no personas
        await _emit_persona_scope_rollup({"github_service": gh}, {"persona_scope": {}}, {"pr_node_id": "P"})
        # no github
        await _emit_persona_scope_rollup({"github_service": None}, {"persona_scope": _SCOPE}, {"pr_node_id": "P"})
        # no pr_node_id
        await _emit_persona_scope_rollup({"github_service": gh}, {"persona_scope": _SCOPE}, {})
        gh.add_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_emit_swallows_post_error(self) -> None:
        github = MagicMock()
        github.add_comment = AsyncMock(side_effect=RuntimeError("boom"))
        session: dict = {"persona_scope": _SCOPE}
        await _emit_persona_scope_rollup({"github_service": github}, session, {"id": "c", "pr_node_id": "P"})
        # signature NOT stamped on failure, so a later retry can re-post.
        assert "persona_scope_rollup_signature" not in session


@pytest.mark.asyncio
async def test_notify_includes_pr_url_in_payload_when_set() -> None:
    """pr_url on the card is forwarded into the notification payload.

    Uses a card_dispatched scenario rather than the merging phase because
    042 fixed the bug where merging phase fired notifications without a
    real merge — see test_notify_merging_phase_without_commit_summary_does_not_fire.
    """
    state = initial_state()
    state["current_card"] = {
        "id": "card-42",
        "title": "Add feature",
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
        "pr_url": "https://github.com/org/repo/pull/99",
    }
    # Use monitoring_performer (the real post-dispatch phase) — phase
    # "dispatching" at notify time now means the dispatch was HELD and
    # card_dispatched is intentionally suppressed (076 live QA #150).
    state["phase"] = "monitoring_performer"

    dispatched: list[NotificationEvent] = []

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock(side_effect=dispatched.append)
    state["notification_service"] = notification_service

    result = await notify(state)

    assert isinstance(result, dict)
    notification_service.dispatch.assert_awaited_once()
    event: NotificationEvent = dispatched[0]
    assert "pr_url" in event.payload
    assert event.payload["pr_url"] == "https://github.com/org/repo/pull/99"


@pytest.mark.asyncio
async def test_notify_excludes_pr_url_from_payload_when_not_set() -> None:
    """When card has no pr_url the payload key is absent."""
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "title": "Some card",
        "status": "TODO",
        "previous_status": "",
    }
    # See note above: monitoring_performer is the real card_dispatched phase.
    state["phase"] = "monitoring_performer"

    dispatched: list[NotificationEvent] = []

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock(side_effect=dispatched.append)
    state["notification_service"] = notification_service

    await notify(state)

    event: NotificationEvent = dispatched[0]
    assert "pr_url" not in event.payload


@pytest.mark.asyncio
async def test_notify_suppresses_dispatched_re_emit_for_same_stage() -> None:
    """Once card_dispatched has fired for (card, performer_stage), a later
    pass on the same active session must not re-emit even after the
    NotificationService dedup window would have expired.

    Reproduces 2026-05-26 incident where #138 produced a duplicate
    'dispatched to implementing' Slack post 11 min apart while env_bootstrap
    was still running (phase stayed in monitoring_performer; dedup TTL=600s
    elapsed and the same dedup_key re-fired).
    """
    state = initial_state()
    card = {
        "id": "card-138",
        "title": "Add robots.txt",
        "status": "IN_PROGRESS",
    }
    state["current_card"] = card
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "implementing"
    state["active_sessions"] = {"card-138": {}}

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock()
    state["notification_service"] = notification_service

    await notify(state)
    assert notification_service.dispatch.await_count == 1

    await notify(state)
    assert notification_service.dispatch.await_count == 1

    # Stage transition produces a fresh notification.
    state["performer_stage"] = "reviewing"
    await notify(state)
    assert notification_service.dispatch.await_count == 2


@pytest.mark.asyncio
async def test_notify_suppresses_card_dispatched_when_dispatch_held() -> None:
    """076 live QA #150: when dispatch_performer holds (env_bootstrap_in_flight /
    env_cache not ready / in-flight guard) it returns with phase still
    "dispatching" and NO container started.  notify must NOT emit
    card_dispatched in that case — otherwise every poll cycle re-announces and,
    once the 600s Slack dedup window lapses, leaks a duplicate "dispatched"
    post for a card that never launched a performer.
    """
    state = initial_state()
    state["current_card"] = {
        "id": "card-150",
        "title": "Feature: Time tracking schema",
        "status": "IN_PROGRESS",
    }
    state["phase"] = "dispatching"  # held: no container started this cycle
    state["performer_stage"] = "assessing"

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock()
    state["notification_service"] = notification_service

    # Re-run several "held" cycles — none may emit.
    for _ in range(3):
        await notify(state)
    assert notification_service.dispatch.await_count == 0

    # Once a container actually starts, dispatch_performer advances the phase
    # to monitoring_performer; THAT is when the operator should be told.
    state["phase"] = "monitoring_performer"
    await notify(state)
    assert notification_service.dispatch.await_count == 1


@pytest.mark.asyncio
async def test_notify_dispatched_re_emits_after_simulated_restart() -> None:
    """The dispatched-stages list is in-memory only; if a restart drops it
    the next pass MUST re-emit so operators see which cards came back.
    """
    state = initial_state()
    state["current_card"] = {"id": "card-138", "title": "Add robots.txt", "status": "IN_PROGRESS"}
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "implementing"
    state["active_sessions"] = {"card-138": {}}  # session entry present, list absent

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock()
    state["notification_service"] = notification_service

    await notify(state)
    assert notification_service.dispatch.await_count == 1
    assert state["active_sessions"]["card-138"]["dispatched_notified_stages"] == ["implementing"]


@pytest.mark.asyncio
async def test_notify_dispatched_does_not_stamp_when_session_missing() -> None:
    """When active_sessions has no entry for the card the throwaway sess
    dict cannot retain the stamp; verify we never re-emit-suppress against
    a discarded dict (gate is open until the session entry exists).
    """
    state = initial_state()
    state["current_card"] = {"id": "card-999", "title": "Orphan", "status": "IN_PROGRESS"}
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "implementing"
    state["active_sessions"] = {}  # no entry for card-999

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock()
    state["notification_service"] = notification_service

    await notify(state)
    await notify(state)
    # Both fire — the upstream DeduplicationWindow is what protects against
    # within-window repeats. The dispatched-once gate only engages once the
    # session entry is registered.
    assert notification_service.dispatch.await_count == 2
    assert "card-999" not in state["active_sessions"]


@pytest.mark.asyncio
async def test_notify_handles_dispatch_exception_gracefully() -> None:
    """If dispatch() raises, notify() must still return state without propagating."""
    state = initial_state()
    state["current_card"] = {
        "id": "card-5",
        "title": "Card",
        "status": "BLOCKED",
        "pr_url": "https://github.com/org/repo/pull/1",
    }
    state["phase"] = "blocked"

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock(side_effect=RuntimeError("downstream error"))
    state["notification_service"] = notification_service

    result = await notify(state)
    assert isinstance(result, dict)
