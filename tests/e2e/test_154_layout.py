"""#154: the simplified Dashboard layout in a real browser.

The status strip, the active runs, the board and the activity feed sit above the
fold at 1280x800. Each fact is shown once, the utilization table is on the
Performers page with a summary line on the Dashboard, the lower panels collapse,
and a run's menu calls the existing control endpoints.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from coordinare.dashboard import DashboardStore
from coordinare.services.card_timeline import PIPELINE_STAGES
from tests.e2e.test_dashboard_browser import (
    _WAIT_LIVE,
    _WAIT_NAV,
    _WAIT_SSE,
    _active_session,
    _full_snapshot,
)

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
RUNNING_CARDS = 2
BOARD_CARDS = 6
ACTIVITY_ROWS = 20
_BOARD = "layout-e2e"
_RUNNING_A = "PVTI_R1"
_RUNNING_B = "PVTI_R2"


def _board() -> dict:
    """One symphony with six cards on the board: two to do, two running, two in review."""
    columns = {
        "TODO": ["PVTI_T1", "PVTI_T2"],
        "BLOCKED": [],
        "IN_PROGRESS": [_RUNNING_A, _RUNNING_B],
        "IN_REVIEW": ["PVTI_V1", "PVTI_V2"],
        "DONE": ["PVTI_D1"],
        "BACKLOG": [],
    }
    titles = {
        "PVTI_T1": "Add export", "PVTI_T2": "Tidy logs", _RUNNING_A: "Fix login",
        _RUNNING_B: "Speed up search", "PVTI_V1": "Review cache", "PVTI_V2": "Pin versions", "PVTI_D1": "Ship docs",
    }
    return {
        "name": _BOARD, "priority": 0, "github_project_number": 1,
        "state": {
            "board_snapshot": columns,
            "board_titles": titles,
            "board_issue_numbers": {"PVTI_T1": 11, "PVTI_T2": 12, _RUNNING_A: 21, _RUNNING_B: 22, "PVTI_V1": 31, "PVTI_V2": 32, "PVTI_D1": 41},
            "board_issue_urls": {},
            "board_pr_urls": {},
            "todo_waits": {},
            "todo_capacity": None,
            "active_sessions": {},
        },
    }


def _ago(minutes: float) -> str:
    """An ISO timestamp this many minutes in the past, so the fixture reads as a live daemon."""
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _running(card_id: str, title: str, number: int, session: str) -> dict:
    return {
        **_active_session(card_id, title, "implementing"),
        "issue_number": number,
        "session_id": session,
        "agent_dispatch_at": _ago(5),
        "card_tokens_total": 1234,
    }


def _pipeline(card_ids: list[str]) -> dict:
    """The card_pipeline the daemon sends: each card's stages, the running one current (147)."""
    statuses_for = []
    for stage in PIPELINE_STAGES:
        status = "current" if stage == "implementing" else ("done" if PIPELINE_STAGES.index(stage) < PIPELINE_STAGES.index("implementing") else "pending")
        statuses_for.append({"stage": stage, "status": status})
    return {_BOARD: {card_id: {"current": "implementing", "statuses": statuses_for} for card_id in card_ids}}


def _layout_snapshot(**overrides: Any) -> dict:
    return _full_snapshot(
        phase="monitoring_performer",
        phase_label="Monitoring Performer",
        symphonies=[_board()],
        active_sessions=[
            _running(_RUNNING_A, "Fix login", 21, "run-aaa111"),
            _running(_RUNNING_B, "Speed up search", 22, "run-bbb222"),
        ],
        agent_dispatch_at=_ago(5),
        last_poll_at=_ago(0.5),
        poll_interval_seconds=30,
        board_summary={"TODO": 2, "IN_PROGRESS": 2, "IN_REVIEW": 2, "DONE": 1, "BLOCKED": 0, "BACKLOG": 3},
        card_pipeline=_pipeline([_RUNNING_A, _RUNNING_B]),
        **overrides,
    )


LONG_TITLE = ("Investigate the intermittent timeout when the export service retries a partially "
              "uploaded archive after a network reset")


def _scenario_snapshot(runs: int, long_title: bool, banner: bool) -> dict:
    """``runs`` running cards on the board, with long or short titles, and the attention banner on or off."""
    ids = [f"PVTI_S{i}" for i in range(runs)]
    board = _board()
    board["state"]["board_snapshot"]["IN_PROGRESS"] = ids
    base = LONG_TITLE if long_title else "Fix login"
    titles = {card: f"{base} {n}" for n, card in enumerate(ids)}
    board["state"]["board_titles"].update(titles)
    board["state"]["board_issue_numbers"].update({card: 100 + n for n, card in enumerate(ids)})
    sessions = [_running(card, titles[card], 100 + n, f"run-s{n:03d}") for n, card in enumerate(ids)]
    questions = []
    if banner:
        questions = [{"card_id": ids[0], "card_number": 100, "card_title": titles[ids[0]], "stage": "implementing",
                      "text": "Which provider?", "asked_at": _ago(3), "issue_url": None}]
    pipeline = {_BOARD: {card: _pipeline([card])[_BOARD][card] for card in ids}}
    return _full_snapshot(
        phase="monitoring_performer", phase_label="Monitoring Performer",
        symphonies=[board], active_sessions=sessions, open_questions=questions,
        agent_dispatch_at=_ago(5), last_poll_at=_ago(0.5), poll_interval_seconds=30,
        board_summary={"TODO": 2, "IN_PROGRESS": runs, "IN_REVIEW": 2, "DONE": 1, "BLOCKED": 0, "BACKLOG": 3},
        card_pipeline=pipeline,
    )


def _activity(count: int) -> list[dict[str, Any]]:
    return [
        {
            "seq": 9000 + n, "timestamp": _ago(1), "card_id": _RUNNING_A, "card_number": 21,
            "card_title": "Fix login", "stage": "implementing", "activity_type": "progress",
            "text": f"entry {n}", "summary": "", "status": "", "truncated": False,
            "session_id": "", "performer_id": "", "is_delta": False, "stream_id": "",
        }
        for n in range(count)
    ]


def _show_the_layout(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_layout_snapshot())
    store.broadcaster.broadcast({"_event": "activity_event", "entries": _activity(ACTIVITY_ROWS)})
    expect(page.locator("#active-performer-tiles .ap-tile")).to_have_count(RUNNING_CARDS, timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_at_1280_by_800_the_strip_active_runs_cards_and_activity_all_fit_above_the_fold(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _show_the_layout(page, live_server_url, store)
    expect(page.locator("#swimlane-section .swimlane-card")).to_have_count(BOARD_CARDS, timeout=_WAIT_LIVE)
    expect(page.locator("#activity-feed .af-row")).to_have_count(ACTIVITY_ROWS, timeout=_WAIT_LIVE)

    for selector in ("#status-strip", "#active-performers", "#active-work-card", "#activity-feed-card"):
        box = page.locator(selector).bounding_box()
        assert box is not None, selector
        assert box["y"] >= 0, (selector, box)
        assert box["y"] + box["height"] <= VIEWPORT_HEIGHT, f"{selector} ends below the fold: {box}"


@pytest.mark.e2e
def test_the_board_and_the_activity_feed_sit_side_by_side_on_a_desktop(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _show_the_layout(page, live_server_url, store)
    cards = page.locator("#active-work-card").bounding_box()
    feed = page.locator("#activity-feed-card").bounding_box()
    assert cards is not None and feed is not None
    assert abs(cards["y"] - feed["y"]) < 1
    assert feed["x"] > cards["x"]


@pytest.mark.e2e
def test_the_current_cards_phase_text_appears_once_on_the_dashboard(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer", phase_label="Monitoring Performer",
        symphonies=[_board()],
        active_sessions=[_running(_RUNNING_A, "Fix login", 21, "run-aaa111")],
        agent_dispatch_at=_ago(5),
    ))
    expect(page.locator("#active-performer-tiles .ap-tile")).to_have_count(1, timeout=_WAIT_LIVE)
    phase = page.evaluate("() => humanPhase('monitoring_performer')")
    # The tile shows the phase; the board card and the header do not repeat it.
    expect(page.locator("#dashboard-page").get_by_text(phase, exact=True)).to_have_count(1)


@pytest.mark.e2e
def test_the_utilization_table_is_on_the_performers_page_and_the_dashboard_keeps_a_summary(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_layout_snapshot(role_utilization=[
        {"role": "implementing", "active": 1, "max": 2, "queued": 0},
        {"role": "reviewing", "active": 0, "max": 1, "queued": 0},
    ]))

    expect(page.locator("#active-runs-summary")).to_have_text("2 agents · 1 busy · 0 queued", timeout=_WAIT_LIVE)
    expect(page.locator("#role-utilization-card")).to_have_count(0)
    expect(page.locator("#dashboard-page")).not_to_contain_text("of 2 busy")

    page.locator("#navbar a[href='/performers']").click()
    expect(page.locator("#performers-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#performers-page-tbody")).to_contain_text("1 of 2 busy", timeout=_WAIT_LIVE)
    expect(page.locator("#performers-page-tbody")).to_contain_text("0 of 1 busy")


@pytest.mark.e2e
def test_the_lower_panels_are_collapsed_and_expand_on_click(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _show_the_layout(page, live_server_url, store)
    metrics = page.locator("#metrics-card")
    assert metrics.evaluate("el => el.open") is False
    expect(page.locator("#cycles-completed")).to_be_hidden()
    metrics.locator(":scope > summary").click()
    expect(page.locator("#cycles-completed")).to_be_visible()

    flow = page.locator("#flow-chart")
    expect(flow).to_be_hidden()
    page.locator("#workflow-card summary").click()
    expect(flow).to_be_visible()


@pytest.mark.e2e
def test_a_run_menu_calls_the_existing_cancel_and_skip_endpoints(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _show_the_layout(page, live_server_url, store)
    def fulfil(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"status": "override_queued", "action": "x"}))

    page.route("**/api/cancel**", fulfil)
    page.route("**/api/skip-role**", fulfil)
    page.on("dialog", lambda dialog: dialog.accept())

    tile = page.locator(f"#active-performer-tiles .ap-tile[data-card-id='{_RUNNING_A}']")
    tile.locator(".card-actions > summary").click()
    with page.expect_request("**/api/skip-role**") as skip:
        tile.get_by_role("button", name="Skip current stage").click()
    assert skip.value.method == "POST"
    assert skip.value.url.split("/api/", 1)[1] == f"skip-role?card_id={_RUNNING_A}"
    expect(tile.locator(".card-action-msg")).to_have_text("Queued. It takes effect on the next check.", timeout=_WAIT_LIVE)

    with page.expect_request("**/api/cancel**") as cancel:
        tile.get_by_role("button", name="Cancel card").click()
    assert cancel.value.method == "POST"
    assert cancel.value.url.split("/api/", 1)[1] == f"cancel?card_id={_RUNNING_A}"


@pytest.mark.e2e
def test_a_run_menu_opens_from_the_keyboard(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _show_the_layout(page, live_server_url, store)
    tile = page.locator(f"#active-performer-tiles .ap-tile[data-card-id='{_RUNNING_B}']")
    summary = tile.locator(".card-actions > summary")
    summary.focus()
    page.keyboard.press("Enter")
    expect(tile.locator(".card-actions")).to_have_attribute("open", "")
    expect(tile.get_by_role("button", name="Cancel card")).to_be_visible()


@pytest.mark.e2e
def test_the_cycle_log_is_on_the_history_page_and_not_the_dashboard(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(cycle_history=[
        {"timestamp": "2026-10-13T10:05:00+00:00", "phase": "idle", "duration_seconds": 0.45, "outcome": "success"},
    ]))
    expect(page.locator("#recent-cycles-card")).to_have_count(0)

    page.locator("#navbar a[href='/history']").click()
    expect(page.locator("#history-page")).to_be_visible(timeout=_WAIT_NAV)
    panel = page.locator("#history-cycles-panel")
    panel.locator(":scope > summary").click()
    expect(page.locator("#history-cycles")).to_contain_text("success", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Review of #154: the fold holds for long titles, four runs and the banner.
# ---------------------------------------------------------------------------

_FOLD_SELECTORS = ("#status-strip", "#active-performers", "#active-work-card", "#activity-feed-card")
_SCENARIOS = [
    (2, False, False), (2, True, False), (4, False, False), (4, True, False),
    (2, False, True), (4, False, True), (4, True, True),
]


@pytest.mark.e2e
@pytest.mark.parametrize(("runs", "long_title", "banner"), _SCENARIOS, ids=lambda v: str(v))
def test_the_fold_holds_for_any_title_length_run_count_and_banner(
    page: Page, live_server_url: str, store: DashboardStore, runs: int, long_title: bool, banner: bool,
) -> None:
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_scenario_snapshot(runs, long_title, banner))
    store.broadcaster.broadcast({"_event": "activity_event", "entries": _activity(ACTIVITY_ROWS)})
    expect(page.locator("#active-performer-tiles .ap-tile")).to_have_count(runs, timeout=_WAIT_LIVE)
    expect(page.locator("#activity-feed .af-row")).to_have_count(ACTIVITY_ROWS, timeout=_WAIT_LIVE)
    if banner:
        expect(page.locator("#attention-panel")).to_be_visible(timeout=_WAIT_LIVE)
    for selector in _FOLD_SELECTORS:
        box = page.locator(selector).bounding_box()
        assert box is not None, selector
        assert box["y"] + box["height"] <= VIEWPORT_HEIGHT, f"{selector} ends below the fold: {box}"


@pytest.mark.e2e
def test_a_long_board_title_is_clamped_to_two_lines_with_the_full_title_on_hover(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_scenario_snapshot(2, True, False))
    title = page.locator("#swimlane-section .swimlane-card-title", has_text=f"{LONG_TITLE} 0")
    expect(title).to_have_count(1, timeout=_WAIT_LIVE)
    expect(title).to_have_attribute("title", f"{LONG_TITLE} 0", timeout=_WAIT_LIVE)
    box = title.bounding_box()
    assert box is not None
    line_height = title.evaluate("el => parseFloat(getComputedStyle(el).lineHeight)")
    assert box["height"] <= 2 * line_height + 1, box


@pytest.mark.e2e
def test_board_columns_scroll_inside_a_bounded_height(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _show_the_layout(page, live_server_url, store)
    body = page.locator("#swimlane-section .swimlane-col-body").first
    assert body.evaluate("el => getComputedStyle(el).overflowY") == "auto"
    assert body.evaluate("el => getComputedStyle(el).maxHeight") != "none"


@pytest.mark.e2e
def test_four_active_run_tiles_share_one_row_at_1280(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_scenario_snapshot(4, False, False))
    expect(page.locator("#active-performer-tiles .ap-tile")).to_have_count(4, timeout=_WAIT_LIVE)
    tops = page.locator("#active-performer-tiles .ap-tile").evaluate_all(
        "els => els.map(el => Math.round(el.getBoundingClientRect().top))",
    )
    assert len(set(tops)) == 1, tops


@pytest.mark.e2e
def test_the_attention_banner_is_one_line_until_it_is_expanded(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_scenario_snapshot(2, False, True))
    panel = page.locator("#attention-panel")
    expect(panel).to_be_visible(timeout=_WAIT_LIVE)
    box = panel.bounding_box()
    assert box is not None and box["height"] <= 48, box
    toggle = page.locator("#attention-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#attention-list")).to_be_hidden()
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#attention-list")).to_be_visible()


# --- Review: the Conductor description is visible text, not hover-only ----------

@pytest.mark.e2e
def test_the_conductor_description_is_visible_text_with_the_full_text_on_hover(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _show_the_layout(page, live_server_url, store)
    desc = page.locator("#phase-desc")
    expect(desc).to_have_text("Performer agent is actively working on the card.", timeout=_WAIT_LIVE)
    expect(desc).to_be_visible()
    assert desc.get_attribute("title") == "Performer agent is actively working on the card."


# --- Review: one stage tracker per running tile; no duplicate View run ----------

@pytest.mark.e2e
def test_a_running_tile_renders_the_stage_tracker_from_the_snapshot(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _show_the_layout(page, live_server_url, store)
    tile = page.locator(f"#active-performer-tiles .ap-tile[data-card-id='{_RUNNING_A}']")
    expect(tile.locator(".tl-dot")).to_have_count(len(PIPELINE_STAGES), timeout=_WAIT_LIVE)
    expect(tile.locator(".tl-dot.tl-current")).to_have_count(1)


@pytest.mark.e2e
def test_a_running_card_has_one_view_run_link_on_its_tile_only(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _show_the_layout(page, live_server_url, store)
    expect(page.locator(f"#active-performer-tiles .ap-tile[data-card-id='{_RUNNING_A}'] .view-run-link")).to_have_count(1)
    expect(page.locator(f"#swimlane-section .swimlane-card[data-card-id='{_RUNNING_A}'] .view-run-link")).to_have_count(0)


# --- Review: duplicate timing and the live region ----------------------------

@pytest.mark.e2e
def test_the_active_runs_header_does_not_repeat_the_freshness_timing(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _show_the_layout(page, live_server_url, store)
    expect(page.locator("#last-checked")).to_have_count(0)
    expect(page.locator("#connection-detail")).to_contain_text("next check in")


@pytest.mark.e2e
def test_only_the_state_label_is_a_live_region_not_the_ticking_countdown(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _show_the_layout(page, live_server_url, store)
    expect(page.locator("#connection-status")).not_to_have_attribute("aria-live", "polite")
    expect(page.locator("#connection-label")).to_have_attribute("role", "status")
    expect(page.locator("#connection-label")).to_have_attribute("aria-live", "polite")


@pytest.mark.e2e
def test_metrics_and_recent_checks_summaries_carry_a_heading(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _show_the_layout(page, live_server_url, store)
    expect(page.locator("#metrics-card > summary h2")).to_have_text("Metrics")
    page.locator("#navbar a[href='/history']").click()
    expect(page.locator("#history-cycles-panel > summary h2")).to_have_text("Recent checks (this daemon run)")


# --- Review: no em or en dash in what the page renders ---------------------------

@pytest.mark.e2e
def test_the_rendered_dashboard_text_has_no_em_or_en_dash(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _show_the_layout(page, live_server_url, store)
    page.evaluate("() => { document.getElementById('metrics-card').open = true; document.getElementById('health-card').open = true; }")
    text = page.evaluate("() => document.getElementById('dashboard-page').innerText")
    text += page.evaluate("() => document.getElementById('disconnected-banner').textContent")
    assert "\u2014" not in text and "\u2013" not in text  # em dash, en dash


@pytest.mark.e2e
def test_the_tab_title_uses_a_bar_not_a_dash(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _show_the_layout(page, live_server_url, store)
    expect(page).to_have_title("Dashboard | Coordinare", timeout=_WAIT_LIVE)
