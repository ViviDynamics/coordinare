from __future__ import annotations

import pytest

from coordinare.graph.nodes.notify import notify
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
