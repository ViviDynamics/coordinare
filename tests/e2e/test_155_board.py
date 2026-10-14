"""#155: the board's Done lane, PR checks icons, inactive symphony and card labels, in a real browser."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_SYMPHONY = "conductor-e2e"


def _now_minus(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def _strip(*, checks: str | None = None, merged_hours_ago: float | None = None) -> dict:
    return {
        "current": None,
        "statuses": [],
        "checks": checks,
        "merged_at": _now_minus(merged_hours_ago) if merged_hours_ago is not None else None,
    }


def _symphony(*, done: list[str] = (), in_review: list[str] = (), in_progress: list[str] = (),
              todo: list[str] = (), waits: dict | None = None, capacity: dict | None = None,
              titles: dict | None = None, pr_urls: dict | None = None, polled: bool = True) -> dict:
    return {
        "name": _SYMPHONY, "priority": 0, "github_project_number": 1, "github_org": "acme", "repo": "sample",
        "cycle_count": 3 if polled else 0, "error_count": 0,
        "last_poll_at": _now_minus(0.01) if polled else None,
        "state": {
            "board_snapshot": {"TODO": list(todo), "BLOCKED": [], "IN_PROGRESS": list(in_progress),
                               "IN_REVIEW": list(in_review), "DONE": list(done), "BACKLOG": []},
            "board_titles": titles or {},
            "board_issue_numbers": {},
            "board_issue_urls": {},
            "board_pr_urls": pr_urls or {},
            "todo_waits": waits or {},
            "todo_capacity": capacity,
            "active_sessions": {},
        },
    }


def _broadcast(store: DashboardStore, *, symphonies: list[dict], **overrides) -> None:
    store.broadcaster.broadcast(_full_snapshot(
        phase="idle", phase_label="Idle", symphonies=symphonies, **overrides,
    ))


@pytest.mark.e2e
def test_the_done_lane_is_collapsed_and_its_summary_carries_the_count(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        symphonies=[_symphony(
            done=["PVTI_1", "PVTI_2", "PVTI_3"],
            titles={"PVTI_1": "Ship the export", "PVTI_2": "Fix the import", "PVTI_3": "Old merge"},
        )],
        card_pipeline={_SYMPHONY: {
            "PVTI_1": _strip(checks="pass", merged_hours_ago=1),
            "PVTI_2": _strip(checks="pass", merged_hours_ago=5),
            "PVTI_3": _strip(checks="pass", merged_hours_ago=30),
        }},
    )

    lane = page.locator("#done-lane")
    expect(lane.locator("summary")).to_contain_text("Done (last 24h)", timeout=_WAIT_LIVE)
    expect(lane.locator("summary .swimlane-col-count")).to_have_text("2", timeout=_WAIT_LIVE)
    assert lane.evaluate("node => node.open") is False
    expect(lane.locator(".done-lane-list")).to_be_hidden()


@pytest.mark.e2e
def test_the_done_lane_opens_to_the_cards_it_counts(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        symphonies=[_symphony(
            done=["PVTI_1", "PVTI_3"],
            titles={"PVTI_1": "Ship the export", "PVTI_3": "Old merge"},
        )],
        card_pipeline={_SYMPHONY: {
            "PVTI_1": _strip(checks="pass", merged_hours_ago=1),
            "PVTI_3": _strip(checks="pass", merged_hours_ago=30),
        }},
    )

    lane = page.locator("#done-lane")
    expect(lane.locator("summary .swimlane-col-count")).to_have_text("1", timeout=_WAIT_LIVE)
    lane.locator("summary").click()
    expect(lane).to_have_attribute("open", "", timeout=_WAIT_LIVE)
    expect(lane.locator(".done-lane-list li")).to_have_count(1)
    expect(lane.locator(".done-lane-list")).to_contain_text("Ship the export")
    expect(lane.locator(".done-lane-list")).not_to_contain_text("Old merge")
    expect(lane.locator(".done-lane-note")).to_contain_text("Earlier merges are not counted.")


@pytest.mark.e2e
def test_each_pr_checks_state_has_its_own_icon_and_an_unknown_one(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    ids = ["PVTI_11", "PVTI_12", "PVTI_13", "PVTI_14"]
    titles = {"PVTI_11": "Passing card", "PVTI_12": "Failing card", "PVTI_13": "Running card",
              "PVTI_14": "Unjudged card"}
    _broadcast(
        store,
        symphonies=[_symphony(in_review=ids, titles=titles,
                              pr_urls={card: f"https://github.com/acme/sample/pull/{n}" for n, card in enumerate(ids, 1)})],
        card_pipeline={_SYMPHONY: {
            "PVTI_11": _strip(checks="pass"),
            "PVTI_12": _strip(checks="fail"),
            "PVTI_13": _strip(checks="running"),
            "PVTI_14": _strip(checks=None),
        }},
    )

    card_passing = page.locator("#swimlane-section .swimlane-card").filter(has_text="Passing card")
    expect(card_passing.locator(".checks-icon.checks-pass")).to_have_attribute(
        "title", "PR checks: passing", timeout=_WAIT_LIVE,
    )
    expect(page.locator("#swimlane-section .swimlane-card").filter(has_text="Failing card")
           .locator(".checks-icon.checks-fail")).to_have_attribute("title", "PR checks: failing")
    expect(page.locator("#swimlane-section .swimlane-card").filter(has_text="Running card")
           .locator(".checks-icon.checks-running")).to_have_attribute("title", "PR checks: running")
    expect(page.locator("#swimlane-section .swimlane-card").filter(has_text="Unjudged card")
           .locator(".checks-icon.checks-unknown")).to_have_attribute(
        "title", "PR checks: unknown, no CI gate has judged this card yet",
    )


@pytest.mark.e2e
def test_a_card_without_a_pr_shows_no_checks_icon(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        symphonies=[_symphony(in_review=["PVTI_21"], titles={"PVTI_21": "No pull request yet"})],
        card_pipeline={_SYMPHONY: {"PVTI_21": _strip(checks="pass")}},
    )
    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="No pull request yet")
    expect(card).to_contain_text("No pull request yet", timeout=_WAIT_LIVE)
    expect(card.locator(".checks-icon")).to_have_count(0)


@pytest.mark.e2e
def test_a_never_checked_symphony_is_labelled_in_the_switcher_and_says_so_in_the_cards(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, symphonies=[
        _symphony(todo=["PVTI_31"], titles={"PVTI_31": "Live card"}),
        {"name": "placeholder", "priority": 1, "github_project_number": None, "github_org": "",
         "repo": "", "cycle_count": 0, "error_count": 0, "last_poll_at": None, "state": None},
    ])

    switcher = page.locator("#symphony-select")
    expect(switcher.locator("option", has_text="placeholder")).to_contain_text(
        "placeholder (Never checked: check config)", timeout=_WAIT_LIVE,
    )
    expect(switcher.locator("option", has_text=_SYMPHONY)).not_to_contain_text("Never checked")

    switcher.select_option("placeholder")
    expect(page.locator("#swimlane-section")).to_contain_text("Never checked: check config", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_symphonies_page_greys_out_a_never_checked_symphony(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    # The broadcaster only pushes to connected clients, so the page is opened before the snapshot.
    page.goto(live_server_url + "/symphonies")
    expect(page.locator("#symphonies-page")).to_be_visible(timeout=_WAIT_SSE)
    _broadcast(store, symphonies=[
        {"name": "placeholder", "priority": 0, "github_project_number": None, "github_org": "",
         "repo": "", "cycle_count": 0, "error_count": 0, "last_poll_at": None, "state": None},
    ])
    row = page.locator("tr[data-inactive='true']")
    expect(row).to_contain_text("Never checked: check config", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_header_has_no_stray_dash(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, symphonies=[_symphony(todo=["PVTI_41"], titles={"PVTI_41": "Any card"})])

    header = page.locator("header.workspace-header")
    expect(page.locator("#workspace-title")).to_have_text("Dashboard", timeout=_WAIT_LIVE)
    expect(header).not_to_contain_text("\u2014")
    expect(header).not_to_contain_text("\u2013")
    expect(page).to_have_title("Dashboard | Coordinare", timeout=_WAIT_SSE)


@pytest.mark.e2e
def test_a_card_shows_its_stage_pair_and_its_waiting_reason(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        symphonies=[_symphony(
            in_progress=["PVTI_51"], todo=["PVTI_52"],
            titles={"PVTI_51": "Wire the export", "PVTI_52": "Queued work"},
            waits={"PVTI_52": {"reason": "queued", "blockers": []}},
            capacity={"in_use": 1, "limit": 1},
        )],
        active_sessions=[_active_session(card_id="PVTI_51", card_title="Wire the export", stage="implementing",
                                         phase="monitoring_agent")],
    )

    working = page.locator("#swimlane-section .swimlane-card").filter(has_text="Wire the export")
    expect(working.locator("code")).to_have_text("Implement · Implementer", timeout=_WAIT_LIVE)
    queued = page.locator("#swimlane-section .swimlane-card").filter(has_text="Queued work")
    expect(queued.locator(".swimlane-wait")).to_have_text("Queued: 1 of 1 card slots in use", timeout=_WAIT_LIVE)
