from __future__ import annotations

import pytest

from coordinare.graph.nodes.notify import _persona_scope_signature, notify
from coordinare.graph.state import initial_state
from coordinare.models.notification import EventType, NotificationEvent
from tests.utils.fake_notification import FakeNotificationService


@pytest.mark.asyncio
async def test_notify_dispatches_event() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "title": "Card",
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "dispatching"

    result = await notify(state)

    assert result["current_card"]["title"] == "Card"
    assert len(fake.dispatched) == 1
    assert fake.dispatched[0].event_type == EventType.card_dispatched
    assert fake.dispatched[0].dedup_key == "card_dispatched:Card:IN_PROGRESS:implementing"


@pytest.mark.asyncio
async def test_notify_uses_card_id_for_dedup_key_when_available() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "PROJ-42",
        "title": "Card",
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "dispatching"

    await notify(state)

    assert fake.dispatched[0].dedup_key == "card_dispatched:PROJ-42:IN_PROGRESS:implementing"


@pytest.mark.asyncio
async def test_notify_returns_state_when_missing_services() -> None:
    state = initial_state()
    state["current_card"] = {"title": "Card", "status": "TODO"}

    result = await notify(state)

    assert result["current_card"]["title"] == "Card"


@pytest.mark.asyncio
async def test_notify_returns_state_when_no_card() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["notification_service"] = fake

    await notify(state)

    assert len(fake.dispatched) == 0


@pytest.mark.asyncio
async def test_notify_includes_commit_summary_in_payload() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "title": "Card",
        "status": "DONE",
        "previous_status": "IN_REVIEW",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "merging"
    state["commit_summary"] = "abc1234 Fix bug"

    await notify(state)

    assert len(fake.dispatched) == 1
    assert fake.dispatched[0].payload["commit_summary"] == "abc1234 Fix bug"
    assert fake.dispatched[0].event_type == EventType.card_merged


@pytest.mark.asyncio
async def test_notify_includes_open_questions() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "title": "Card",
        "status": "BLOCKED",
        "previous_status": "TODO",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["What API?", "Which provider?"]

    await notify(state)

    assert len(fake.dispatched) == 1
    assert "What API?" in fake.dispatched[0].payload["open_questions"]


@pytest.mark.asyncio
async def test_notify_survives_dispatch_failure() -> None:
    """Dispatch errors should be caught and not raised."""

    class _FailingNotification:
        async def dispatch(self, event: NotificationEvent) -> None:
            raise ConnectionError("boom")

    state = initial_state()
    state["current_card"] = {
        "title": "Card",
        "status": "TODO",
        "description": "Task",
    }
    state["notification_service"] = _FailingNotification()

    result = await notify(state)
    assert result is not None


# ---------------------------------------------------------------------------
# 042 — false-merge notification regression: merging-phase retries must NOT
# fire ``card_merged`` until the merge actually succeeds (commit_summary set
# or card.status=DONE).  Previously, every cycle that merge_pr looped back
# to phase=merging produced a fake "✅ merged!" Slack message even though
# the squash_merge mutation was being rejected by the GitHub API.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_merging_phase_without_commit_summary_does_not_fire() -> None:
    """Regression: merge_pr loops back to phase=merging on retry without
    setting commit_summary.  notify must not emit any event in that case —
    not card_merged (false success), not card_transition (noise) — because
    nothing actually changed since the last cycle."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "title": "Favicon",
        "status": "IN_REVIEW",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "merging"
    # No commit_summary, status not DONE — merge attempt failed and retrying

    await notify(state)

    assert fake.dispatched == []


@pytest.mark.asyncio
async def test_notify_fires_card_merged_when_commit_summary_set_even_in_idle() -> None:
    """042: After a successful merge, merge_pr sets phase=idle AND
    commit_summary AND card.status=DONE.  The card_merged event must
    still fire even though phase isn't ``merging`` — success is detected
    via commit_summary, not the phase."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "title": "Favicon",
        "status": "DONE",
        "previous_status": "IN_REVIEW",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "idle"
    state["commit_summary"] = "abc1234 Add favicon"

    await notify(state)

    assert len(fake.dispatched) == 1
    assert fake.dispatched[0].event_type == EventType.card_merged
    assert "merged!" in fake.dispatched[0].payload["summary"]


@pytest.mark.asyncio
async def test_notify_fires_card_merged_when_status_done_without_commit_summary() -> None:
    """042: card.status=DONE alone is enough to detect merge success
    (defensive — covers any path where commit_summary might be unset
    but the card was moved to DONE by merge_pr)."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "title": "Favicon",
        "status": "DONE",
        "previous_status": "IN_REVIEW",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "idle"
    # No commit_summary

    await notify(state)

    assert len(fake.dispatched) == 1
    assert fake.dispatched[0].event_type == EventType.card_merged


@pytest.mark.asyncio
async def test_notify_merging_phase_with_status_done_fires_card_merged() -> None:
    """042: When merge_pr succeeds it sets card.status=DONE — even if it
    hasn't yet transitioned phase off ``merging`` in the snapshot we see,
    we recognise the success."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "title": "Favicon",
        "status": "DONE",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "merging"
    state["commit_summary"] = "abc1234 done"

    await notify(state)

    assert len(fake.dispatched) == 1
    assert fake.dispatched[0].event_type == EventType.card_merged


@pytest.mark.asyncio
async def test_notify_performer_stage_none_does_not_leak_string_none() -> None:
    """Copilot review: if performer_stage is explicitly None in state,
    ``str(state.get("performer_stage", ""))`` returns the literal "None"
    (the default "" only fires on missing keys), which then leaks into
    the dedup key and "dispatched to None" summary.  Treat None as an
    empty string — same bug class as the _build_snapshot fix."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "title": "Card",
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
    }
    state["notification_service"] = fake
    state["phase"] = "dispatching"
    state["performer_stage"] = None  # explicit None, not missing

    await notify(state)

    assert len(fake.dispatched) == 1
    event = fake.dispatched[0]
    # Dedup key must NOT contain the literal "None"
    assert "None" not in (event.dedup_key or "")
    # Human-readable summary must NOT say "dispatched to None"
    assert "None" not in event.payload["summary"]


# --- 046: Dependency state in blocked notifications ---


@pytest.mark.asyncio
async def test_blocked_notification_includes_dependency_context() -> None:
    """046 T028: When blocked_by_dependencies is non-empty, the Slack
    summary should show blocker issue numbers and columns instead of
    the generic open_questions preview."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "CARD_1",
        "title": "Add dark mode",
        "issue_number": 91,
        "status": "BLOCKED",
        "previous_status": "TODO",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Depends on #90"]
    state["blocked_by_dependencies"] = [
        {"issue_number": 90, "column": "IN_PROGRESS"},
    ]

    await notify(state)

    assert len(fake.dispatched) == 1
    summary = fake.dispatched[0].payload["summary"]
    assert "#90" in summary
    assert "IN_PROGRESS" in summary
    assert "waiting on" in summary


@pytest.mark.asyncio
async def test_blocked_notification_without_dependencies_uses_questions() -> None:
    """046: When blocked_by_dependencies is empty, fall back to the
    standard open_questions preview in the summary."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "CARD_2",
        "title": "Some feature",
        "status": "BLOCKED",
        "previous_status": "TODO",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["What API should we use?"]
    state["blocked_by_dependencies"] = []

    await notify(state)

    assert len(fake.dispatched) == 1
    summary = fake.dispatched[0].payload["summary"]
    assert "What API" in summary
    assert "waiting on" not in summary


@pytest.mark.asyncio
async def test_notify_card_transition_with_status_change() -> None:
    """When prev_status and status are both present and different,
    emit a card_transition event with status change in summary."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "CARD_3",
        "title": "Feature X",
        "status": "IN_REVIEW",
        "previous_status": "IN_PROGRESS",
    }
    state["notification_service"] = fake
    state["phase"] = "monitoring_pr"

    await notify(state)

    assert len(fake.dispatched) == 1
    event = fake.dispatched[0]
    assert event.event_type == EventType.card_transition
    assert "IN_PROGRESS → IN_REVIEW" in event.payload["summary"]


# ---------------------------------------------------------------------------
# 069 — Block-notification dedup on state rehydration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppresses_card_blocked_within_reminder_cooldown() -> None:
    """069 US1 (FR-004): per-session ``last_blocked_slack_delivered_at`` set
    less than ``card_blocked_reminder_cooldown_seconds`` ago → skip the
    card_blocked dispatch.  Gates both the post-restart first-tick path AND
    every per-cycle re-emission while the cooldown has not elapsed."""
    from datetime import UTC, datetime, timedelta

    fake = FakeNotificationService(card_blocked_reminder_cooldown_seconds=3600)
    state = initial_state()
    state["current_card"] = {
        "id": "card-70",
        "title": "Card 70",
        "status": "BLOCKED",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Clarify scope"]
    state["active_sessions"] = {
        "card-70": {
            "last_blocked_slack_delivered_at": datetime.now(UTC) - timedelta(minutes=5),
        }
    }

    await notify(state)

    blocked_events = [e for e in fake.dispatched if e.event_type == EventType.card_blocked]
    assert blocked_events == []


@pytest.mark.asyncio
async def test_card_blocked_dispatches_after_reminder_cooldown_elapsed() -> None:
    """069 FR-004: once ``card_blocked_reminder_cooldown_seconds`` has elapsed
    since the last Slack delivery, the next pass re-emits and re-stamps the
    watermark."""
    from datetime import UTC, datetime, timedelta

    fake = FakeNotificationService(card_blocked_reminder_cooldown_seconds=3600)
    state = initial_state()
    state["current_card"] = {
        "id": "card-70",
        "title": "Card 70",
        "status": "BLOCKED",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Clarify scope"]
    old_watermark = datetime.now(UTC) - timedelta(hours=2)
    state["active_sessions"] = {
        "card-70": {"last_blocked_slack_delivered_at": old_watermark}
    }

    await notify(state)

    assert any(e.event_type == EventType.card_blocked for e in fake.dispatched)
    new_watermark = state["active_sessions"]["card-70"]["last_blocked_slack_delivered_at"]
    assert new_watermark > old_watermark


@pytest.mark.asyncio
async def test_card_blocked_cooldown_disabled_emits_every_pass() -> None:
    """069 FR-004: ``card_blocked_reminder_cooldown_seconds=0`` disables the
    notify-level gate (per-channel DeduplicationWindow remains the only
    rate control)."""
    from datetime import UTC, datetime, timedelta

    fake = FakeNotificationService(card_blocked_reminder_cooldown_seconds=0)
    state = initial_state()
    state["current_card"] = {
        "id": "card-70",
        "title": "Card 70",
        "status": "BLOCKED",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Clarify scope"]
    state["active_sessions"] = {
        "card-70": {
            "last_blocked_slack_delivered_at": datetime.now(UTC) - timedelta(seconds=10),
        }
    }

    await notify(state)

    assert any(e.event_type == EventType.card_blocked for e in fake.dispatched)


@pytest.mark.asyncio
async def test_card_blocked_does_not_suppress_when_only_notified_watermark_present() -> None:
    """069 FR-004 regression: ``last_blocked_notified_at`` is rewritten by
    handle_blocked on every pass (it triples as check_board cutoff + 24h
    GitHub-comment dedup) and therefore does NOT prove Slack ever fired.
    Only ``last_blocked_slack_delivered_at`` may gate FR-004 suppression."""
    from datetime import UTC, datetime, timedelta

    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-70",
        "title": "Card 70",
        "status": "BLOCKED",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Clarify scope"]
    state["active_sessions"] = {
        "card-70": {
            "phase": "blocked",
            # Set ONLY the handle_blocked watermark; Slack-delivery field absent.
            "last_blocked_notified_at": datetime.now(UTC) - timedelta(hours=1),
        }
    }

    await notify(state)

    assert any(e.event_type == EventType.card_blocked for e in fake.dispatched)


@pytest.mark.asyncio
async def test_card_blocked_dispatch_stamps_slack_delivered_watermark() -> None:
    """069 FR-004: on successful dispatch, notify must write
    ``last_blocked_slack_delivered_at`` onto the active_sessions entry so
    daemon._persist_active_sessions can snapshot it for the next restart."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-70",
        "title": "Card 70",
        "status": "BLOCKED",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Clarify scope"]
    state["active_sessions"] = {"card-70": {"phase": "blocked"}}

    await notify(state)

    sess = state["active_sessions"]["card-70"]
    assert sess.get("last_blocked_slack_delivered_at") is not None


@pytest.mark.asyncio
async def test_suppresses_card_blocked_when_active_session_dispatching() -> None:
    """069 US2 (FR-005): fresh performer running for the card → suppress
    the stale card_blocked from a rehydrated top-level phase."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-70",
        "title": "Card 70",
        "status": "IN_PROGRESS",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Clarify scope"]
    state["active_sessions"] = {"card-70": {"phase": "dispatching"}}

    await notify(state)

    assert not any(e.event_type == EventType.card_blocked for e in fake.dispatched)


@pytest.mark.asyncio
async def test_suppresses_card_blocked_when_active_session_monitoring_performer() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-70",
        "title": "Card 70",
        "status": "IN_PROGRESS",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Clarify scope"]
    state["active_sessions"] = {"card-70": {"phase": "monitoring_performer"}}

    await notify(state)

    assert not any(e.event_type == EventType.card_blocked for e in fake.dispatched)


@pytest.mark.asyncio
async def test_suppresses_card_blocked_when_active_session_monitoring_agent() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-70",
        "title": "Card 70",
        "status": "IN_PROGRESS",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Clarify scope"]
    state["active_sessions"] = {"card-70": {"phase": "monitoring_agent"}}

    await notify(state)

    assert not any(e.event_type == EventType.card_blocked for e in fake.dispatched)


@pytest.mark.asyncio
async def test_emits_card_blocked_when_active_session_phase_is_blocked() -> None:
    """069 US2 guardrail: active-session phase == 'blocked' must NOT suppress
    — that's the legitimate path where notify should still emit."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-71",
        "title": "Card 71",
        "status": "BLOCKED",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["Clarify scope"]
    # active_sessions present but no watermark, phase=blocked → emit normally
    state["active_sessions"] = {"card-71": {"phase": "blocked"}}

    await notify(state)

    assert any(e.event_type == EventType.card_blocked for e in fake.dispatched)


@pytest.mark.asyncio
async def test_replay_card70_restart_does_not_emit_card_blocked() -> None:
    """069 US2 — direct replay of the 2026-05-22 12:02 incident.  Top-level
    phase rehydrated to 'blocked' while a fresh performer started; the
    active session reflects the fresh dispatch.  No card_blocked must fire."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "PVT_70",
        "title": "Add deployment notes",
        "issue_number": 70,
        "status": "IN_PROGRESS",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"  # stale from rehydration
    state["open_questions"] = ["Which environment first?"]
    state["active_sessions"] = {
        "PVT_70": {
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
        }
    }

    await notify(state)

    assert not any(e.event_type == EventType.card_blocked for e in fake.dispatched)


@pytest.mark.asyncio
async def test_suppresses_card_blocked_when_open_questions_empty() -> None:
    """069 US3 (FR-003): empty open_questions → no card_blocked emission
    (no more content-free 'needs input' Slack posts)."""
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "title": "Card 1",
        "status": "BLOCKED",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = []

    await notify(state)

    assert not any(e.event_type == EventType.card_blocked for e in fake.dispatched)


@pytest.mark.asyncio
async def test_dedup_key_includes_open_questions_hash() -> None:
    """069 US3 (FR-006): differing open_questions content yields distinct
    dedup keys for card_blocked; identical content collapses."""
    fake = FakeNotificationService()
    base_state = {
        "id": "card-1",
        "title": "Card 1",
        "status": "BLOCKED",
    }

    state_a = initial_state()
    state_a["current_card"] = dict(base_state)
    state_a["notification_service"] = fake
    state_a["phase"] = "blocked"
    state_a["open_questions"] = ["Which API?"]
    await notify(state_a)

    state_b = initial_state()
    state_b["current_card"] = dict(base_state)
    state_b["notification_service"] = fake
    state_b["phase"] = "blocked"
    state_b["open_questions"] = ["Which database?"]
    await notify(state_b)

    state_c = initial_state()
    state_c["current_card"] = dict(base_state)
    state_c["notification_service"] = fake
    state_c["phase"] = "blocked"
    state_c["open_questions"] = ["Which API?"]
    await notify(state_c)

    blocked = [e for e in fake.dispatched if e.event_type == EventType.card_blocked]
    keys = [e.dedup_key for e in blocked]
    # Assert on key cardinality, not dispatch count: distinct questions get
    # distinct keys; identical questions collapse to the same key.  Robust
    # to whether the underlying service applies dedup at dispatch time.
    assert len(set(keys)) == 2
    assert keys[0] != keys[1]
    assert keys[0] == keys[2]


@pytest.mark.asyncio
async def test_no_path_emits_card_blocked_with_needs_input_summary() -> None:
    """069 US3 (FR-003): the literal 'needs input' fallback must be gone
    from any card_blocked dispatch payload."""
    fake = FakeNotificationService()
    # Drive several blocked emissions with non-empty questions
    for q in [["Clarify scope"], ["What API?", "What DB?"], ["Where?"]]:
        state = initial_state()
        state["current_card"] = {
            "id": "card-x",
            "title": "Card X",
            "status": "BLOCKED",
        }
        state["notification_service"] = fake
        state["phase"] = "blocked"
        state["open_questions"] = q
        await notify(state)

    blocked = [e for e in fake.dispatched if e.event_type == EventType.card_blocked]
    assert blocked  # sanity: we did emit some
    for e in blocked:
        assert "needs input" not in e.payload["summary"].lower()


def _scope(personas: dict[str, dict]) -> dict:
    return {
        "computed_at": "2026-05-27T00:00:00Z",
        "cycle_index": 1,
        "classifier_model": "test",
        "head_sha": "abc",
        "files_summary": [],
        "personas": personas,
    }


def test_persona_scope_signature_invariant_to_focus() -> None:
    # FR-001: focus prose varies across cycles for identical structural
    # classifications; signature must dedup them so we don't re-post the
    # rollup comment on every poll.
    a = _scope(
        {
            "implementer": {"depth": "normal", "focus": "look at auth", "overrides": []},
            "reviewer": {"depth": "skim", "focus": "skim tests", "overrides": ["api"]},
        }
    )
    b = _scope(
        {
            "implementer": {"depth": "normal", "focus": "TOTALLY different prose", "overrides": []},
            "reviewer": {"depth": "skim", "focus": "and here too", "overrides": ["api"]},
        }
    )
    assert _persona_scope_signature(a) == _persona_scope_signature(b)


def test_persona_scope_signature_changes_on_structural_change() -> None:
    base = _scope({"implementer": {"depth": "normal", "focus": "x", "overrides": []}})
    diff_depth = _scope({"implementer": {"depth": "full", "focus": "x", "overrides": []}})
    diff_name = _scope({"reviewer": {"depth": "normal", "focus": "x", "overrides": []}})
    diff_overrides = _scope(
        {"implementer": {"depth": "normal", "focus": "x", "overrides": ["api"]}}
    )
    sig = _persona_scope_signature(base)
    assert sig != _persona_scope_signature(diff_depth)
    assert sig != _persona_scope_signature(diff_name)
    assert sig != _persona_scope_signature(diff_overrides)


def test_persona_scope_signature_overrides_order_invariant() -> None:
    a = _scope({"impl": {"depth": "normal", "focus": "x", "overrides": ["a", "b"]}})
    b = _scope({"impl": {"depth": "normal", "focus": "x", "overrides": ["b", "a"]}})
    assert _persona_scope_signature(a) == _persona_scope_signature(b)
