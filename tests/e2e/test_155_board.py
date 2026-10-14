"""#155: the board's Done lane, PR checks icons, unchecked symphony and card labels, in a real browser."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_SYMPHONY = "conductor-e2e"
_HEAD = "a" * 40
EM_DASH = "\u2014"
EN_DASH = "\u2013"


def aged_title(word: str) -> re.Pattern[str]:
    return re.compile(rf"^PR checks at review hand-off: {re.escape(word)} \(\d+m \d+s ago\)$")


def _now_minus(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def _checks(state: str | None, *, stale: bool = False, minutes_ago: float = 12) -> dict | None:
    """A card's checks view as the strip carries it, or None when no gate has judged the card."""
    if state is None:
        return None
    return {"state": state, "head": _HEAD[:12], "stale": stale,
            "at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()}


def _strip(*, checks: dict | None = None, merged_hours_ago: float | None = None) -> dict:
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
        "poll_interval_seconds": 30, "daemon_cycles_completed": 3,
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
            "PVTI_1": _strip(checks=_checks("pass"), merged_hours_ago=1),
            "PVTI_2": _strip(checks=_checks("pass"), merged_hours_ago=5),
            "PVTI_3": _strip(checks=_checks("pass"), merged_hours_ago=30),
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
            "PVTI_1": _strip(checks=_checks("pass"), merged_hours_ago=1),
            "PVTI_3": _strip(checks=_checks("pass"), merged_hours_ago=30),
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
def test_the_done_lane_reads_a_microsecond_merge_time_in_this_browser(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    # The daemon writes microseconds. The age text must come out as a real age, in every browser.
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        symphonies=[_symphony(done=["PVTI_7"], titles={"PVTI_7": "Microsecond merge"})],
        card_pipeline={_SYMPHONY: {"PVTI_7": _strip(checks=_checks("pass"), merged_hours_ago=2)}},
    )
    lane = page.locator("#done-lane")
    expect(lane.locator("summary .swimlane-col-count")).to_have_text("1", timeout=_WAIT_LIVE)
    lane.locator("summary").click()
    row = lane.locator(".done-lane-list li")
    expect(row).to_contain_text("merged 2h 0m", timeout=_WAIT_LIVE)
    expect(row).to_contain_text("ago")
    expect(row).not_to_contain_text("NaN")


@pytest.mark.e2e
def test_the_done_lane_cut_uses_the_server_snapshot_time(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    # The snapshot was taken 30 hours ago and the merge is 31 hours before the browser's clock. Against
    # the snapshot time the merge is an hour old, so it is in the lane.
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    snapshot_at = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    merged_at = (datetime.now(UTC) - timedelta(hours=31)).isoformat()
    _broadcast(
        store,
        symphonies=[_symphony(done=["PVTI_8"], titles={"PVTI_8": "Server clock merge"})],
        card_pipeline={_SYMPHONY: {"PVTI_8": {"current": None, "statuses": [], "checks": None,
                                              "merged_at": merged_at}}},
        snapshot_at=snapshot_at,
    )
    lane = page.locator("#done-lane")
    expect(lane.locator("summary .swimlane-col-count")).to_have_text("1", timeout=_WAIT_LIVE)


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
            "PVTI_11": _strip(checks=_checks("pass")),
            "PVTI_12": _strip(checks=_checks("fail")),
            "PVTI_13": _strip(checks=_checks("running")),
            "PVTI_14": _strip(checks=None),
        }},
    )

    cards = page.locator("#swimlane-section .swimlane-card")
    expect(cards.filter(has_text="Passing card").locator(".checks-icon.checks-pass")).to_have_attribute(
        "title", aged_title("passing"), timeout=_WAIT_LIVE,
    )
    expect(cards.filter(has_text="Failing card").locator(".checks-icon.checks-fail")).to_have_attribute(
        "title", aged_title("failing"),
    )
    expect(cards.filter(has_text="Running card").locator(".checks-icon.checks-running")).to_have_attribute(
        "title", aged_title("running"),
    )
    expect(cards.filter(has_text="Unjudged card").locator(".checks-icon.checks-unknown")).to_have_attribute(
        "title", "PR checks: unknown, no CI gate has judged this card yet",
    )


@pytest.mark.e2e
def test_a_stale_result_is_neutral_and_says_it_is_an_earlier_commit(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        symphonies=[_symphony(in_review=["PVTI_61"], titles={"PVTI_61": "Pushed again"},
                              pr_urls={"PVTI_61": "https://github.com/acme/sample/pull/61"})],
        card_pipeline={_SYMPHONY: {"PVTI_61": _strip(checks=_checks("fail", stale=True))}},
    )
    icon = page.locator("#swimlane-section .swimlane-card").filter(has_text="Pushed again").locator(".checks-icon")
    expect(icon).to_have_class("checks-icon checks-fail checks-stale", timeout=_WAIT_LIVE)
    expect(icon).to_have_attribute("title", re.compile(
        r"^PR checks at review hand-off: failing \(\d+m \d+s ago\), checks as of an earlier commit$"))


@pytest.mark.e2e
def test_an_environment_block_is_neutral_and_not_failing(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        symphonies=[_symphony(in_review=["PVTI_71"], titles={"PVTI_71": "Quota hit"},
                              pr_urls={"PVTI_71": "https://github.com/acme/sample/pull/71"})],
        card_pipeline={_SYMPHONY: {"PVTI_71": _strip(checks=_checks("blocked"))}},
    )
    icon = page.locator("#swimlane-section .swimlane-card").filter(has_text="Quota hit").locator(".checks-icon")
    expect(icon).to_have_attribute("title", aged_title("blocked by environment"), timeout=_WAIT_LIVE)
    expect(icon).not_to_have_class("checks-icon checks-fail")


@pytest.mark.e2e
def test_a_card_without_a_pr_shows_no_checks_icon(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(
        store,
        symphonies=[_symphony(in_review=["PVTI_21"], titles={"PVTI_21": "No pull request yet"})],
        card_pipeline={_SYMPHONY: {"PVTI_21": _strip(checks=_checks("pass"))}},
    )
    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="No pull request yet")
    expect(card).to_contain_text("No pull request yet", timeout=_WAIT_LIVE)
    expect(card.locator(".checks-icon")).to_have_count(0)


def _unchecked(name: str, **overrides: object) -> dict:
    entry = {"name": name, "priority": 1, "github_project_number": None, "github_org": "",
             "repo": "", "cycle_count": 0, "error_count": 0, "last_poll_at": None,
             "poll_interval_seconds": 30, "daemon_cycles_completed": 3, "state": None}
    entry.update(overrides)
    return entry


@pytest.mark.e2e
def test_a_never_checked_symphony_is_labelled_in_the_switcher_and_says_so_in_the_cards(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, symphonies=[
        _symphony(todo=["PVTI_31"], titles={"PVTI_31": "Live card"}),
        _unchecked("placeholder", priority=1),
    ])

    switcher = page.locator("#symphony-select")
    expect(switcher.locator("option", has_text="placeholder")).to_contain_text(
        "placeholder (Never checked: check config)", timeout=_WAIT_LIVE,
    )
    expect(switcher.locator("option", has_text=_SYMPHONY)).not_to_contain_text("Never checked")

    switcher.select_option("placeholder")
    expect(page.locator("#swimlane-section")).to_contain_text("Never checked: check config", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_symphony_whose_first_cycle_failed_shows_the_error_not_never_checked(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, symphonies=[
        _symphony(todo=["PVTI_41"], titles={"PVTI_41": "Live card"}),
        _unchecked("broken", error_count=3, daemon_cycles_completed=1,
                   state={"last_error": "GitHub 401 Bad credentials", "board_snapshot": None}),
    ])

    switcher = page.locator("#symphony-select")
    expect(switcher.locator("option", has_text="broken")).to_contain_text(
        "broken (Last check failed: GitHub 401 Bad credentials)", timeout=_WAIT_LIVE,
    )
    switcher.select_option("broken")
    cards = page.locator("#swimlane-section")
    expect(cards).to_contain_text("Last check failed: GitHub 401 Bad credentials", timeout=_WAIT_LIVE)
    expect(cards).not_to_contain_text("Never checked")
    expect(cards).not_to_contain_text("Waiting for a webhook")


@pytest.mark.e2e
def test_a_fresh_boot_says_not_checked_yet_and_a_webhook_only_daemon_says_waiting(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, symphonies=[
        _symphony(todo=["PVTI_51"], titles={"PVTI_51": "Live card"}),
        _unchecked("booting", daemon_cycles_completed=0),
        _unchecked("hooked", poll_interval_seconds=None, daemon_cycles_completed=0),
    ])
    switcher = page.locator("#symphony-select")
    expect(switcher.locator("option", has_text="booting")).to_contain_text("booting (Not checked yet)", timeout=_WAIT_LIVE)
    expect(switcher.locator("option", has_text="hooked")).to_contain_text("hooked (Waiting for a webhook)")


@pytest.mark.e2e
def test_the_symphonies_page_greys_out_a_never_checked_symphony(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    # The broadcaster only pushes to connected clients, so the page is opened before the snapshot.
    page.goto(live_server_url + "/symphonies")
    expect(page.locator("#symphonies-page")).to_be_visible(timeout=_WAIT_SSE)
    _broadcast(store, symphonies=[_unchecked("placeholder")])
    row = page.locator("tr[data-inactive='true']")
    expect(row).to_contain_text("Never checked: check config", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_header_has_no_stray_dash(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, symphonies=[_symphony(todo=["PVTI_41"], titles={"PVTI_41": "Any card"})])

    header = page.locator("header.workspace-header")
    expect(page.locator("#workspace-title")).to_have_text("Dashboard", timeout=_WAIT_LIVE)
    expect(header).not_to_contain_text(EM_DASH)
    expect(header).not_to_contain_text(EN_DASH)
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
