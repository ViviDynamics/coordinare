"""#142: every card in the Blocked column says which kind of hold it is, and why, in a real browser.

The kinds are Needs human (with the reason the conductor holds), Waiting on #N
(a dependency) and Queued (capacity, which only To Do cards can hit). The
conductor decides each one; the browser only names it.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_BOARD = "conductor-e2e"
_HOSTILE = '<img src=x onerror="alert(1)">Which provider?'


def _board(*, todo: list[str], blocked: list[str] = (), waits: dict | None = None,
           capacity: dict | None = None, titles: dict | None = None) -> dict:
    return {
        "name": _BOARD, "priority": 0, "github_project_number": 1,
        "state": {
            "board_snapshot": {"TODO": todo, "BLOCKED": list(blocked), "IN_PROGRESS": [], "IN_REVIEW": []},
            "board_titles": titles or {},
            "board_issue_numbers": {},
            "board_issue_urls": {},
            "board_pr_urls": {},
            "todo_waits": waits or {},
            "todo_capacity": capacity,
            "active_sessions": {},
        },
    }


def _broadcast(store: DashboardStore, symphony: dict, **overrides) -> None:
    store.broadcaster.broadcast(_full_snapshot(
        phase="idle", phase_label="Idle", symphonies=[symphony], **overrides,
    ))


def _stopped_session(card_id: str, title: str, cause: str | None, phase: str = "idle") -> dict:
    session = _active_session(card_id, title, "implementing", phase=phase)
    session["block_cause"] = cause
    return session


@pytest.mark.e2e
def test_a_card_blocked_on_a_question_says_needs_human_and_the_question(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        _board(todo=[], blocked=["PVTI_3"], titles={"PVTI_3": "Migrate auth"}, capacity={"in_use": 0, "limit": 1}),
        open_questions=[{
            "card_id": "PVTI_3", "card_number": 3, "card_title": "Migrate auth", "stage": "implementing",
            "text": "Which provider?", "asked_at": "2026-10-13T10:00:00+00:00", "issue_url": None,
        }],
    )

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Migrate auth")
    expect(card.locator(".swimlane-wait")).to_have_text("Needs human: Which provider?", timeout=_WAIT_LIVE)
    expect(card.locator(".swimlane-wait")).to_have_attribute("data-wait", "needs_human")
    # The age depends on the clock, so the title is checked for the question and the word "Asked".
    expect(card.locator(".swimlane-wait")).to_have_attribute(
        "title", re.compile(r"^Asked .*Which provider\?$"), timeout=_WAIT_LIVE,
    )


@pytest.mark.e2e
def test_a_card_blocked_on_a_retries_exhausted_format_failure_names_the_class(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    board = _board(todo=[], blocked=["PVTI_12"], titles={"PVTI_12": "Flaky output"}, capacity={"in_use": 0, "limit": 1})
    _broadcast(store, board, active_sessions=[
        _stopped_session("PVTI_12", "Flaky output", "format_exhausted"),
    ])

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Flaky output")
    expect(card.locator(".swimlane-wait")).to_have_text(
        "Needs human: retries exhausted on a format failure", timeout=_WAIT_LIVE,
    )
    expect(card.locator(".swimlane-wait")).to_have_attribute("data-wait", "needs_human")


@pytest.mark.e2e
def test_a_card_blocked_on_a_closed_pull_request_names_the_closure(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    board = _board(todo=[], blocked=["PVTI_13"], titles={"PVTI_13": "Closed PR"}, capacity={"in_use": 0, "limit": 1})
    _broadcast(store, board, active_sessions=[_stopped_session("PVTI_13", "Closed PR", "closed_pr")])

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Closed PR")
    expect(card.locator(".swimlane-wait")).to_have_text(
        "Needs human: the pull request was closed without merging", timeout=_WAIT_LIVE,
    )


@pytest.mark.e2e
def test_a_card_blocked_on_a_dependency_says_waiting_on_the_issue(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    board = _board(todo=[], blocked=["PVTI_77"], titles={"PVTI_77": "Waits on #9"}, capacity={"in_use": 0, "limit": 1})
    board["state"]["session_skip_reasons"] = {"PVTI_77": {
        "reason": "dependency_blocked", "blockers": [9],
    }}
    _broadcast(store, board, active_sessions=[_stopped_session("PVTI_77", "Waits on #9", None, phase="blocked")])

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Waits on #9")
    expect(card.locator(".swimlane-wait")).to_have_text("Waiting on #9", timeout=_WAIT_LIVE)
    expect(card.locator(".swimlane-wait")).to_have_attribute("data-wait", "waiting_on")
    expect(card).not_to_contain_text("Needs human")


@pytest.mark.e2e
def test_a_card_held_for_capacity_says_queued(page: Page, live_server_url: str, store: DashboardStore) -> None:
    """Capacity holds To Do cards, not Blocked ones, so the Queued kind is checked on To Do."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, _board(
        todo=["PVTI_65"], titles={"PVTI_65": "Waiting for a slot"},
        waits={"PVTI_65": {"reason": "queued", "blockers": []}},
        capacity={"in_use": 1, "limit": 1},
    ))

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Waiting for a slot")
    expect(card.locator(".swimlane-wait")).to_have_text("Queued: 1 of 1 card slots in use", timeout=_WAIT_LIVE)
    expect(card.locator(".swimlane-wait")).to_have_attribute("data-wait", "queued")


@pytest.mark.e2e
def test_a_card_blocked_for_an_unknown_reason_says_needs_human_and_nothing_more(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, _board(
        todo=[], blocked=["PVTI_20"], titles={"PVTI_20": "Moved by hand"}, capacity={"in_use": 0, "limit": 1},
    ))

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Moved by hand")
    expect(card.locator(".swimlane-wait")).to_have_text("Needs human", timeout=_WAIT_LIVE)
    expect(card.locator(".swimlane-wait")).to_have_attribute("data-wait", "needs_human")


@pytest.mark.e2e
def test_hostile_reason_text_is_shown_as_text_and_never_run(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    dialogs: list[str] = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    _broadcast(
        store,
        _board(todo=[], blocked=["PVTI_HOSTILE"], titles={"PVTI_HOSTILE": "Hostile reason"},
               capacity={"in_use": 0, "limit": 1}),
        open_questions=[{
            "card_id": "PVTI_HOSTILE", "card_number": 1, "card_title": "Hostile reason", "stage": "implementing",
            "text": _HOSTILE, "asked_at": None, "issue_url": None,
        }],
    )

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Hostile reason")
    expect(card.locator(".swimlane-wait")).to_contain_text("Needs human: <img", timeout=_WAIT_LIVE)
    assert card.locator(".swimlane-wait img").count() == 0
    assert dialogs == []


@pytest.mark.e2e
def test_the_word_blocked_appears_only_as_the_github_column_name(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, _board(
        todo=[], blocked=["PVTI_40", "PVTI_41"], titles={"PVTI_40": "Needs an answer", "PVTI_41": "Stopped"},
        capacity={"in_use": 0, "limit": 1},
    ), active_sessions=[
        _stopped_session("PVTI_40", "Needs an answer", "system_error"),
        _stopped_session("PVTI_41", "Stopped", None),
    ])

    expect(page.locator("#swimlane-section .swimlane-card").filter(has_text="Stopped")).to_be_visible(
        timeout=_WAIT_LIVE,
    )
    swimlane = page.locator("#swimlane-section").inner_text()
    # The one "Blocked" is the column header, which is the GitHub column's name.
    assert swimlane.lower().count("blocked") == 1, swimlane
    assert swimlane.count("Blocked") == 1, swimlane
    assert "Needs human" in swimlane


@pytest.mark.e2e
def test_the_needs_attention_row_names_the_reason_for_a_stop_with_a_known_cause(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, _board(
        todo=[], blocked=["PVTI_50"], titles={"PVTI_50": "Attention row"}, capacity={"in_use": 0, "limit": 1},
    ), active_sessions=[_stopped_session("PVTI_50", "Attention row", "format_exhausted", phase="blocked")])

    attention = page.locator("#attention-list")
    expect(attention).to_contain_text("Needs human", timeout=_WAIT_LIVE)
    expect(attention).to_contain_text("retries exhausted on a format failure", timeout=_WAIT_LIVE)
