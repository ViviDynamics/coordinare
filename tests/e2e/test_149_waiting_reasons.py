"""#149: each To Do and Blocked card says why it is waiting, in a real browser."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_BOARD = "conductor-e2e"


def _board(*, todo: list[str], blocked: list[str] = (), waits: dict, capacity: dict | None,
           titles: dict | None = None) -> dict:
    return {
        "name": _BOARD, "priority": 0, "github_project_number": 1,
        "state": {
            "board_snapshot": {"TODO": todo, "BLOCKED": list(blocked), "IN_PROGRESS": [], "IN_REVIEW": []},
            "board_titles": titles or {},
            "board_issue_numbers": {},
            "board_issue_urls": {},
            "board_pr_urls": {},
            "todo_waits": waits,
            "todo_capacity": capacity,
            "active_sessions": {},
        },
    }


def _broadcast(store: DashboardStore, symphony: dict, **overrides) -> None:
    store.broadcaster.broadcast(_full_snapshot(
        phase="idle", phase_label="Idle", symphonies=[symphony], **overrides,
    ))


@pytest.mark.e2e
def test_a_queued_card_says_the_slots_are_full(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, _board(
        todo=["PVTI_65"], titles={"PVTI_65": "Add the export"},
        waits={"PVTI_65": {"reason": "queued", "blockers": []}},
        capacity={"in_use": 1, "limit": 1},
    ))

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Add the export")
    expect(card.locator(".swimlane-wait")).to_have_text(
        "Queued: 1 of 1 card slots in use", timeout=_WAIT_LIVE,
    )
    expect(card.locator(".swimlane-wait")).to_have_attribute("data-wait", "queued")


@pytest.mark.e2e
def test_the_to_do_header_summarises_capacity_and_the_queue(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, _board(
        todo=["PVTI_65", "PVTI_66", "PVTI_67"],
        waits={
            "PVTI_65": {"reason": "queued", "blockers": []},
            "PVTI_66": {"reason": "queued", "blockers": []},
            "PVTI_67": {"reason": "queued", "blockers": []},
        },
        capacity={"in_use": 1, "limit": 1},
    ))

    header = page.locator("#swimlane-section .swimlane-col-header").first
    expect(header).to_contain_text("To Do", timeout=_WAIT_LIVE)
    expect(header).to_have_attribute("title", "1 of 1 card slots in use · 3 queued", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_blocked_card_with_a_question_says_how_long_ago_it_was_asked(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        _board(todo=[], blocked=["PVTI_3"], titles={"PVTI_3": "Migrate auth"},
               waits={}, capacity={"in_use": 0, "limit": 1}),
        open_questions=[{
            "card_id": "PVTI_3", "card_number": 3, "card_title": "Migrate auth", "stage": "implementing",
            "text": "Which provider?", "asked_at": "2026-10-13T10:00:00+00:00", "issue_url": None,
        }],
    )

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Migrate auth")
    expect(card.locator(".swimlane-wait")).to_have_text(
        "Needs human: Which provider?", timeout=_WAIT_LIVE,
    )
    expect(card.locator(".swimlane-wait")).to_have_attribute("data-wait", "needs_human")


@pytest.mark.e2e
def test_a_card_held_by_someone_else_is_filtered_and_says_so(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, _board(
        todo=["PVTI_9"], titles={"PVTI_9": "Someone else's work"},
        waits={"PVTI_9": {"reason": "assigned_elsewhere", "blockers": []}},
        capacity={"in_use": 0, "limit": 1},
    ))

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Someone else's work")
    expect(card.locator(".swimlane-wait")).to_have_text(
        "Filtered out: assigned to someone else", timeout=_WAIT_LIVE,
    )


@pytest.mark.e2e
def test_a_card_title_from_github_cannot_inject_markup_into_the_reason(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    dialogs: list[str] = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    _broadcast(store, _board(
        todo=["PVTI_X"], titles={"PVTI_X": "<img src=x onerror=alert(1)>Hostile"},
        waits={"PVTI_X": {"reason": "waiting_on", "blockers": ["<img src=x onerror=alert(2)>#1"]}},
        capacity={"in_use": 0, "limit": 1},
    ))

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Hostile")
    expect(card.locator(".swimlane-wait")).to_contain_text("Waiting on", timeout=_WAIT_LIVE)
    assert card.locator(".swimlane-wait img").count() == 0
    assert dialogs == []


@pytest.mark.e2e
def test_a_card_with_no_known_reason_shows_no_line(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, _board(
        todo=["PVTI_11"], titles={"PVTI_11": "No reason recorded"}, waits={}, capacity=None,
    ))

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="No reason recorded")
    expect(card).to_be_visible(timeout=_WAIT_LIVE)
    expect(card.locator(".swimlane-wait")).to_have_count(0)


@pytest.mark.e2e
def test_a_session_held_by_the_review_cap_says_so_and_the_header_counts_it(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    board = _board(
        todo=["PVTI_70"], titles={"PVTI_70": "Next up"}, waits={},
        capacity={"in_use": 0, "limit": 2, "waiting_on_review": 2, "review_limit": 2, "hold": None},
    )
    board["state"]["session_skip_reasons"] = {"PVTI_70": {
        "reason": "review_capacity", "blockers": [], "waiting_on_review": 2, "review_limit": 2,
        "capacity": {"in_use": 0, "limit": 2}, "detail": "max_cards_in_review is 2: 2 waiting on review",
    }}
    _broadcast(store, board, active_sessions=[_active_session("PVTI_70", "Next up", "assessing", phase="dispatching")])

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Next up")
    expect(card.locator(".swimlane-wait")).to_have_text("Queued: 2 cards waiting on review", timeout=_WAIT_LIVE)
    expect(card.locator(".swimlane-wait")).to_have_attribute(
        "title", "max_cards_in_review is 2: 2 waiting on review",
    )
    header = page.locator("#swimlane-section .swimlane-col-header").first
    expect(header).to_have_attribute(
        "title", "0 of 2 card slots in use · 1 queued · 2 of 2 cards waiting on review", timeout=_WAIT_LIVE,
    )


@pytest.mark.e2e
def test_in_symphony_mode_a_queued_session_card_still_says_why(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    """Review of #149: the top-level skip map is restored after a symphony runs, so the
    reason must come from the board's own state."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    board = _board(todo=["PVTI_71"], titles={"PVTI_71": "Queued in symphony"}, waits={},
                   capacity={"in_use": 1, "limit": 1, "waiting_on_review": 0, "review_limit": None, "hold": None})
    board["state"]["session_skip_reasons"] = {"PVTI_71": {
        "reason": "pipeline_capacity", "blockers": [], "capacity": {"in_use": 1, "limit": 1},
    }}
    _broadcast(store, board, session_skip_reasons={},
               active_sessions=[_active_session("PVTI_71", "Queued in symphony", "implementing", phase="dispatching")])

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Queued in symphony")
    expect(card.locator(".swimlane-wait")).to_have_text("Queued: 1 of 1 card slots in use", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_card_moved_to_blocked_by_a_dependency_says_what_it_waits_on(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, _board(
        todo=[], blocked=["PVTI_77"], titles={"PVTI_77": "Waits on an off-board issue"},
        waits={"PVTI_77": {"reason": "waiting_on", "blockers": ["#77"], "verdict": "dependency"}},
        capacity={"in_use": 0, "limit": 1, "waiting_on_review": 0, "review_limit": None, "hold": None},
    ))
    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Waits on an off-board issue")
    expect(card.locator(".swimlane-wait")).to_have_text("Waiting on #77", timeout=_WAIT_LIVE)
    expect(card.locator(".swimlane-wait")).to_have_attribute("data-wait", "waiting_on")
