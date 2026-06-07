"""Tests for src/coordinare/dashboard.py (spec 010-web-dashboard)."""
from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from coordinare.dashboard import (
    _DASHBOARD_HTML,
    DashboardStore,
    SSEBroadcaster,
    _json_default,
    check_port_available,
    compute_overall_health,
    create_dashboard_app,
    format_phase_label,
    is_session_stale,
    render_performer_pool_widget,
)


def test_json_default_handles_set_path_and_datetime() -> None:
    """Regression: SSE snapshots carry PosixPath (workspace_path), sets
    (processed_issue_comment_ids), and datetimes — _json_default must encode
    all three without raising, otherwise the SSE stream dies mid-flight and
    the dashboard sticks on "Disconnected — reconnecting…"."""
    import json
    from pathlib import Path

    payload = {
        "workspace_path": Path("/tmp/coordinare/wp"),
        "processed_ids": {1, 3, 2},
        "ts": datetime(2026, 5, 12, 19, 51, tzinfo=UTC),
    }
    out = json.loads(json.dumps(payload, default=_json_default))
    assert out["workspace_path"] == "/tmp/coordinare/wp"
    assert out["processed_ids"] == [1, 2, 3]
    assert out["ts"].startswith("2026-05-12T19:51")

    with pytest.raises(TypeError):
        _json_default(object())

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_mock_daemon(
    phase: str = "idle",
    snapshot: Any = None,
    error_count: int = 0,
) -> MagicMock:
    daemon = MagicMock()
    daemon.state = {"phase": phase, "error_count": error_count}
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = snapshot
    daemon._cycle_active = False
    daemon.running = True
    return daemon


def _make_mock_metrics(cycles: int = 0, started_at: str | None = None) -> MagicMock:
    metrics = MagicMock()
    # Simulate prometheus counter _value.get()
    metrics.cycles_completed_total._value.get.return_value = cycles
    # Simulate build_info labels()._value.get()
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": started_at or "2026-03-02T09:30:00+00:00"
    }
    return metrics


def _make_mock_health(probes: list[dict] | None = None) -> MagicMock:
    health = MagicMock()
    if probes is None:
        probes = [
            {
                "name": "github",
                "status": "healthy",
                "required": True,
                "checked_at": "2026-03-02T10:00:00+00:00",
                "details": None,
            }
        ]

    mock_probe_objects = []
    for p in probes:
        probe = MagicMock()
        probe.subsystem_name = p["name"]
        probe.status.value = p["status"]
        probe.is_required = p["required"]
        probe.checked_at.isoformat.return_value = p["checked_at"]
        probe.details = p["details"]
        mock_probe_objects.append(probe)

    health.snapshot.return_value.probes = mock_probe_objects
    return health


def _make_app(
    store: DashboardStore | None = None,
    daemon: Any = None,
    metrics: Any = None,
    health: Any = None,
) -> TestClient:
    store = store or DashboardStore()
    daemon = daemon or _make_mock_daemon()
    metrics = metrics or _make_mock_metrics()
    health = health or _make_mock_health()
    app = create_dashboard_app(store, daemon, metrics, health)
    return TestClient(app)


# ---------------------------------------------------------------------------
# T036 — HTML size budget
# ---------------------------------------------------------------------------


def test_dashboard_multi_page_routes_return_html_shell() -> None:
    """049 T007: /performers, /personas, /history all return 200 with the same HTML shell as /."""
    client = _make_app()
    root_html = client.get("/").text
    for path in ["/performers", "/personas", "/history", "/config"]:
        res = client.get(path)
        assert res.status_code == 200, f"{path} returned {res.status_code}"
        assert res.headers["content-type"].startswith("text/html"), f"{path} wrong content-type"
        assert res.text == root_html, f"{path} returned different HTML than /"


def test_config_view_page_containers_present() -> None:
    """081 T018: the live-config view ships its page container and loader hook."""
    assert 'id="config-page"' in _DASHBOARD_HTML
    assert 'id="config-page-section"' in _DASHBOARD_HTML
    assert "loadConfigPage" in _DASHBOARD_HTML
    # T019: routing read-only empty state renders the section's guidance banner,
    # not an error — the renderer surfaces invalid_banner as a status region.
    assert "cfgSectionBlock" in _DASHBOARD_HTML
    # T041 (US3): routing-table CRUD wired to /api/config/routing with a
    # "applies to next performer job" label, distinct from the catalog endpoints.
    assert "cfgRoutingEntryBlock" in _DASHBOARD_HTML
    assert "loadRoutingEntries" in _DASHBOARD_HTML
    assert "/api/config/routing/entry" in _DASHBOARD_HTML
    assert "next performer job" in _DASHBOARD_HTML


def test_config_stale_reference_is_preserved_and_flagged() -> None:
    """A stored reference not in the catalog is kept as a selected, flagged option."""
    # cfgRefSelect injects the unknown current value as a selected option ...
    assert "not in catalog" in _DASHBOARD_HTML
    # ... and only when it is non-empty and unmatched (preserve, never drop).
    assert "if (cur !== '' && !known)" in _DASHBOARD_HTML


def test_cfg_opt_hint_keys_off_stable_schema_key() -> None:
    """cfgOptHint derives its lookup from the stable schema key (last dotted
    component), falling back to the user-facing label only when key is absent —
    so humanizing labels never breaks the modes/model_endpoints hints."""
    # Stable-key derivation: last dotted component of s.key.
    assert "String(s.key).split('.').pop()" in _DASHBOARD_HTML
    # Label is only a fallback when s.key is absent.
    assert "s.key ? String(s.key).split('.').pop() : s.label" in _DASHBOARD_HTML
    # The brittle label-only map is gone.
    assert "byLabel[s.label] = s.current_value" not in _DASHBOARD_HTML


def test_cfg_field_row_renders_length_constraints() -> None:
    """cfgFieldRow handles both {min,max} and {min_length,max_length} range
    shapes, plus partial bounds, so string/collection length constraints render
    real numbers instead of `undefined`."""
    # The length shape is detected and labeled "length".
    assert "min_length" in _DASHBOARD_HTML
    assert "max_length" in _DASHBOARD_HTML
    # Partial bounds render with comparison operators rather than "X to undefined".
    assert "\\u2265" in _DASHBOARD_HTML or "≥" in _DASHBOARD_HTML


def test_cfg_item_block_forces_readonly_when_section_not_editable() -> None:
    """Copilot round 26: a view-only catalog section (personas/symphonies passes
    editable=false into cfgItemBlock) must not render interactive inputs for
    individually-editable settings — there is no Save action, so an edit affordance
    is confusing and violates the read-only requirement. cfgItemBlock forces every
    field read-only (a shallow copy with editable:false) before delegating to
    cfgFieldRow when the section itself is not editable."""
    assert "Object.assign({}, s, {editable: false})" in _DASHBOARD_HTML
    # It must NOT pass cfgFieldRow the raw settings unconditionally any more.
    assert "(item.settings || []).map(cfgFieldRow)" not in _DASHBOARD_HTML


def test_dashboard_html_under_144kb() -> None:
    """T036: _DASHBOARD_HTML must not exceed the 144 KB size budget (raised to accommodate
    multi-page layout, navbar, active-performer tiles, performers/personas/history pages — 049,
    Global Config edit page — 058, CSS design tokens + phase-label + health widget — 059,
    env-bootstrap card + symphonies-list bootstrap button — 060, the 081 live-config
    edit UI: per-field inputs, save/validation feedback, catalog create/edit/delete — 081, the
    081 US3 routing-table CRUD surface: nested target editors + create/edit/delete — T041, and
    the 081 Copilot-review hardening: store-aware save payloads + stable-key field derivation)."""
    size = len(_DASHBOARD_HTML.encode())
    assert size < 144 * 1024, f"_DASHBOARD_HTML is {size} bytes (limit: {144 * 1024})"


def test_history_page_is_live_container_not_coming_soon_stub() -> None:
    """053: /history should render live history containers, not placeholder copy."""
    assert "Coming soon" not in _DASHBOARD_HTML
    assert 'id="history-page-section"' in _DASHBOARD_HTML


def test_performers_page_includes_drilldown_detail_container() -> None:
    """053: /performers must include list/detail view containers for drilldown."""
    assert 'id="performers-page-list-view"' in _DASHBOARD_HTML
    assert 'id="performers-page-detail-view"' in _DASHBOARD_HTML
    assert 'id="performers-page-back"' in _DASHBOARD_HTML


def test_dashboard_cards_use_single_column_layout_where_expected() -> None:
    """053: Key dashboard cards should occupy single grid columns on desktop."""
    assert '<div class="card full">\n  <h2>Workflow</h2>' not in _DASHBOARD_HTML
    assert 'id="performers-card" class="card full"' not in _DASHBOARD_HTML
    assert 'id="active-performers" class="card full"' not in _DASHBOARD_HTML
    assert 'id="active-performers" class="card"' in _DASHBOARD_HTML
    assert 'id="clarifications-card" class="card full"' not in _DASHBOARD_HTML
    assert 'id="clarifications-card" class="card"' in _DASHBOARD_HTML
    assert '<div class="card full">\n  <h2>Recent Cycles</h2>' not in _DASHBOARD_HTML


# ---------------------------------------------------------------------------
# T014 — format_phase_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        # Standard snake_case phases
        ("monitoring_agent", "Monitoring Agent"),
        ("relay_feedback", "Relay Feedback"),
        ("idle", "Idle"),
        ("dispatching", "Dispatching"),
        ("blocked", "Blocked"),
        ("merging", "Merging"),
        ("unknown_future_phase", "Unknown Future Phase"),
        # Edge cases
        ("", ""),
        ("singleword", "Singleword"),
        ("Already Title", "Already Title"),
        ("mixed-separator_phase", "Mixed-Separator Phase"),
    ],
)
def test_format_phase_label(phase: str, expected: str) -> None:
    assert format_phase_label(phase) == expected


def test_format_phase_label_none_returns_empty() -> None:
    # None is not a valid str; callers must coerce — but the Python helper
    # accepts only str per its signature. Verify it does not raise on "".
    assert format_phase_label("") == ""


# ---------------------------------------------------------------------------
# compute_overall_health
# ---------------------------------------------------------------------------


def _sub(name: str, status: str, required: bool = True) -> dict:
    return {"name": name, "status": status, "required": required}


def test_compute_overall_health_all_healthy() -> None:
    assert compute_overall_health([_sub("db", "healthy"), _sub("gh", "healthy")]) == "healthy"


def test_compute_overall_health_empty_list() -> None:
    assert compute_overall_health([]) == "healthy"


def test_compute_overall_health_optional_unhealthy_ignored() -> None:
    assert compute_overall_health([_sub("db", "healthy"), _sub("opt", "degraded", required=False)]) == "healthy"


def test_compute_overall_health_one_degraded() -> None:
    assert compute_overall_health([_sub("db", "healthy"), _sub("gh", "degraded")]) == "degraded"


def test_compute_overall_health_one_unavailable() -> None:
    assert compute_overall_health([_sub("db", "healthy"), _sub("gh", "unavailable")]) == "unavailable"


def test_compute_overall_health_unavailable_beats_degraded() -> None:
    subs = [_sub("db", "degraded"), _sub("gh", "unavailable"), _sub("ci", "healthy")]
    assert compute_overall_health(subs) == "unavailable"


# ---------------------------------------------------------------------------
# is_session_stale (059 Phase E)
# ---------------------------------------------------------------------------


def _iso_ago(minutes: float) -> str:
    from datetime import timedelta

    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def test_is_session_stale_none_returns_false() -> None:
    assert is_session_stale(None) is False


def test_is_session_stale_below_threshold() -> None:
    assert is_session_stale(_iso_ago(10)) is False


def test_is_session_stale_at_threshold_not_stale() -> None:
    # Just under threshold — the check is strict >, so 30 min exactly is not stale.
    # Use 29.9 min to avoid a timing race between timestamp creation and the call.
    assert is_session_stale(_iso_ago(29.9)) is False


def test_is_session_stale_above_threshold() -> None:
    assert is_session_stale(_iso_ago(31)) is True


def test_is_session_stale_custom_threshold() -> None:
    assert is_session_stale(_iso_ago(6), threshold_minutes=5) is True


def test_is_session_stale_invalid_iso_returns_false() -> None:
    assert is_session_stale("not-a-date") is False


# ---------------------------------------------------------------------------
# T015 — GET / and initial SSE event
# ---------------------------------------------------------------------------


def test_get_root_returns_200_html() -> None:
    client = _make_app()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Coordinare Dashboard" in resp.text
    assert "EventSource" in resp.text


def test_sse_initial_event_is_state_update() -> None:
    """sse_stream() must yield a state_update event with the correct shape as its first chunk."""
    import json as _json

    async def _collect() -> str:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        metrics = _make_mock_metrics()
        health = _make_mock_health()
        gen = store.sse_stream(daemon, metrics, health)
        try:
            first_chunk = await gen.__anext__()
        finally:
            await gen.aclose()
        return first_chunk

    first_chunk = asyncio.run(_collect())
    assert first_chunk.startswith("event: state_update\n"), repr(first_chunk)
    data_line = next(line for line in first_chunk.splitlines() if line.startswith("data:"))
    payload = _json.loads(data_line[len("data:"):].strip())
    for key in (
        "phase",
        "phase_label",
        "subsystems",
        "cycles_completed",
        "cycle_history",
        "board_summary",
        "last_poll_at",
    ):
        assert key in payload, f"Missing key {key!r} in SSE payload"


# ---------------------------------------------------------------------------
# T016 — check_port_available exits on conflict
# ---------------------------------------------------------------------------


def test_check_port_available_exits_on_conflict() -> None:
    """Should call sys.exit(1) when the port is already bound."""
    # Bind a port so it appears in-use
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        # Port is now bound — check_port_available should fail
        with pytest.raises(SystemExit) as exc_info:
            check_port_available("127.0.0.1", port)
    assert exc_info.value.code == 1


def test_check_port_available_succeeds_on_free_port() -> None:
    """Should not raise when port is free."""
    # Find a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # Port released — should be free now
    check_port_available("127.0.0.1", port)  # no exception


# ---------------------------------------------------------------------------
# T017 — middleware logs requests
# ---------------------------------------------------------------------------


def test_middleware_logs_http_request(caplog: pytest.LogCaptureFixture) -> None:
    """HTTP middleware must emit an http_request structlog event."""
    import structlog.testing

    client = _make_app()
    with structlog.testing.capture_logs() as cap_logs:
        client.get("/")

    http_events = [e for e in cap_logs if e.get("event") == "http_request"]
    assert http_events, f"No http_request log emitted; got: {[e.get('event') for e in cap_logs]}"
    ev = http_events[0]
    assert ev["method"] == "GET"
    assert ev["path"] == "/"
    assert isinstance(ev["status_code"], int)
    assert isinstance(ev["response_time_ms"], float)


# ---------------------------------------------------------------------------
# T021/T022 — build_snapshot subsystem and metrics fields
# ---------------------------------------------------------------------------


def test_build_snapshot_contains_subsystem_fields() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics(cycles=5)
    health = _make_mock_health([
        {"name": "github", "status": "healthy", "required": True,
         "checked_at": "2026-03-02T10:00:00+00:00", "details": None},
        {"name": "agent_ssh", "status": "degraded", "required": True,
         "checked_at": "2026-03-02T10:00:00+00:00", "details": "circuit open"},
    ])
    snap = store.build_snapshot(daemon, metrics, health)
    assert len(snap["subsystems"]) == 2
    names = {s["name"] for s in snap["subsystems"]}
    assert names == {"github", "agent_ssh"}
    degraded = next(s for s in snap["subsystems"] if s["name"] == "agent_ssh")
    assert degraded["status"] == "degraded"
    assert degraded["details"] == "circuit open"


def test_build_snapshot_metrics_fields() -> None:
    store = DashboardStore()
    store.last_cycle_duration = 1.23
    daemon = _make_mock_daemon(error_count=2)
    metrics = _make_mock_metrics(cycles=17, started_at="2026-03-02T09:30:00+00:00")
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["cycles_completed"] == 17
    assert snap["last_cycle_duration_seconds"] == 1.23
    assert snap["consecutive_error_count"] == 2
    assert snap["daemon_start_time"] == "2026-03-02T09:30:00+00:00"


def test_build_snapshot_includes_board_summary_and_last_poll() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["board_snapshot"] = {
        "TODO": ["1", "2"],
        "IN_PROGRESS": ["3"],
        "IN_REVIEW": [],
        "DONE": ["4"],
    }
    daemon.state["last_poll_at"] = "2026-04-22T22:40:00+00:00"
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["board_summary"]["TODO"] == 2
    assert snap["board_summary"]["IN_PROGRESS"] == 1
    assert snap["board_summary"]["IN_REVIEW"] == 0
    assert snap["board_summary"]["DONE"] == 1
    assert snap["last_poll_at"] == "2026-04-22T22:40:00+00:00"


def test_build_snapshot_board_summary_defaults_missing_columns_to_zero() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["board_snapshot"] = {"BLOCKED": ["x"]}
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["board_summary"]["TODO"] == 0
    assert snap["board_summary"]["IN_PROGRESS"] == 0
    assert snap["board_summary"]["IN_REVIEW"] == 0
    assert snap["board_summary"]["DONE"] == 0
    assert snap["board_summary"]["BLOCKED"] == 1


def test_build_snapshot_board_summary_aggregates_symphony_boards() -> None:
    """Symphony-mode: no top-level board_snapshot; counts must come from symphony states."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    # No top-level board — simulates symphony-only mode
    daemon.state["board_snapshot"] = None

    sym_a = MagicMock()
    sym_a.board_snapshot = {"TODO": ["1", "2"], "IN_REVIEW": ["3"], "DONE": []}
    sym_b = MagicMock()
    sym_b.board_snapshot = {"TODO": ["4"], "IN_PROGRESS": ["5"], "IN_REVIEW": ["6"]}
    daemon.state["symphony_states"] = {"website": sym_a, "other": sym_b}

    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    # Aggregated across both symphonies
    assert snap["board_summary"]["TODO"] == 3
    assert snap["board_summary"]["IN_PROGRESS"] == 1
    assert snap["board_summary"]["IN_REVIEW"] == 2
    assert snap["board_summary"]["DONE"] == 0


def test_build_snapshot_last_poll_at_falls_back_to_symphony() -> None:
    """Symphony-mode: top-level last_poll_at is None; most-recent symphony poll used."""
    from datetime import UTC, datetime

    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["last_poll_at"] = None
    daemon.state["board_snapshot"] = None

    older = datetime(2026, 5, 7, 10, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)

    sym_a = MagicMock()
    sym_a.board_snapshot = {}
    sym_a.last_poll_at = older
    sym_b = MagicMock()
    sym_b.board_snapshot = {}
    sym_b.last_poll_at = newer
    daemon.state["symphony_states"] = {"alpha": sym_a, "beta": sym_b}

    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["last_poll_at"] == newer.isoformat()


def test_build_snapshot_idle_nulls() -> None:
    """When no snapshot in state_store, card fields must be null."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state_store.last_snapshot = None
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["active_card_title"] is None
    assert snap["active_card_column"] is None
    assert snap["pr_url"] is None
    assert snap["agent_session_id"] is None
    assert snap["open_questions"] == []


def test_build_snapshot_active_card() -> None:
    store = DashboardStore()
    mock_snapshot = MagicMock()
    mock_snapshot.phase = "monitoring_agent"
    mock_snapshot.active_card_title = "Fix timeout"
    mock_snapshot.active_card_column = "In Progress"
    mock_snapshot.pr_url = "https://github.com/org/repo/pull/42"
    mock_snapshot.agent_session_id = "sess-abc"
    mock_snapshot.open_questions = []
    daemon = _make_mock_daemon(snapshot=mock_snapshot)
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["active_card_title"] == "Fix timeout"
    assert snap["pr_url"] == "https://github.com/org/repo/pull/42"
    assert snap["phase_label"] == "Monitoring Agent"


def test_build_snapshot_performer_stage_none_coerces_to_empty_string() -> None:
    """Copilot review: if daemon.state["performer_stage"] is explicitly
    None (not missing), ``str(...)`` returns the literal "None" and
    leaks into the dashboard UI as a bogus stage label.  Same bug class
    as the notify and _build_snapshot coercion fixes earlier in this PR."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    # Explicit None — the default "" in .get() does NOT fire for this.
    daemon.state["performer_stage"] = None
    daemon.state["active_sessions"] = {}
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["performer_stage"] == ""
    # And NOT the literal string "None"
    assert snap["performer_stage"] != "None"


def test_build_snapshot_session_performer_stage_none_coerces_to_empty_string() -> None:
    """042: Multi-card active_sessions each carry their own
    performer_stage.  A None value there also must coerce to "" so the
    per-session UI doesn't show "None" as an actual stage."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["active_sessions"] = {
        "CARD_A": {
            "phase": "dispatching",
            "performer_stage": None,  # explicit None
            "current_card": {"title": "Card A"},
        },
    }
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    sessions = snap.get("active_sessions", [])
    assert len(sessions) == 1
    assert sessions[0]["performer_stage"] == ""
    assert sessions[0]["performer_stage"] != "None"


def test_build_snapshot_synthesizes_active_session_in_single_session_mode() -> None:
    """When top-level phase is monitoring but active_sessions is empty,
    snapshot should include a synthetic active session row for dashboard tiles."""
    store = DashboardStore()
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state["active_sessions"] = {}
    daemon.state["current_card"] = {"id": "PVTI_123", "title": "Fix performer tile"}
    daemon.state["performer_stage"] = "implementing"
    daemon.state["agent_dispatch"] = {"session_id": "sid-123"}
    daemon.state["agent_dispatch_at"] = datetime.now(UTC)
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    sessions = snap.get("active_sessions", [])
    assert snap["active_session_count"] == 1
    assert len(sessions) == 1
    assert sessions[0]["card_id"] == "PVTI_123"
    assert sessions[0]["card_title"] == "Fix performer tile"
    assert sessions[0]["phase"] == "monitoring_performer"
    assert sessions[0]["performer_stage"] == "implementing"


# ---------------------------------------------------------------------------
# T028 — record_cycle and history
# ---------------------------------------------------------------------------


def test_record_cycle_appends_entry() -> None:
    store = DashboardStore()
    store.record_cycle(duration_seconds=1.5, phase="idle", outcome="success")
    assert len(store.history) == 1
    entry = store.history[0]
    assert entry["phase"] == "idle"
    assert entry["duration_seconds"] == 1.5
    assert entry["outcome"] == "success"
    assert "timestamp" in entry


def test_history_capped_at_20() -> None:
    store = DashboardStore()
    for i in range(25):
        store.record_cycle(duration_seconds=float(i), phase="idle", outcome="success")
    assert len(store.history) == 20


def test_history_newest_first() -> None:
    store = DashboardStore()
    for i in range(3):
        store.record_cycle(duration_seconds=float(i), phase="idle", outcome="success")
    durations = [e["duration_seconds"] for e in store.history]
    assert durations == [2.0, 1.0, 0.0]


def test_record_cycle_updates_last_duration_on_success() -> None:
    store = DashboardStore()
    store.record_cycle(duration_seconds=1.1, phase="idle", outcome="success")
    assert store.last_cycle_duration == 1.1


def test_record_cycle_does_not_update_last_duration_on_error() -> None:
    store = DashboardStore()
    store.record_cycle(duration_seconds=2.0, phase="idle", outcome="success")
    store.record_cycle(duration_seconds=0.0, phase="recovery", outcome="error")
    assert store.last_cycle_duration == 2.0


def test_history_empty_state() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["cycle_history"] == []


# ---------------------------------------------------------------------------
# T031 — SSEBroadcaster
# ---------------------------------------------------------------------------


def test_broadcaster_delivers_to_all_subscribers() -> None:
    async def _run() -> None:
        b = SSEBroadcaster()
        q1 = b.subscribe()
        q2 = b.subscribe()
        b.broadcast({"phase": "idle"})
        p1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        p2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert p1 == {"phase": "idle"}
        assert p2 == {"phase": "idle"}

    asyncio.run(_run())


def test_broadcaster_drops_for_full_queue() -> None:
    """put_nowait on a full queue must not raise — event dropped silently."""
    async def _run() -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        # Fill the queue completely
        for i in range(32):
            q.put_nowait({"i": i})
        # This broadcast should not raise even though queue is full
        b.broadcast({"overflow": True})

    asyncio.run(_run())


def test_broadcaster_unsubscribe_removes_queue() -> None:
    async def _run() -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        b.unsubscribe(q)
        b.broadcast({"phase": "idle"})
        assert q.empty()

    asyncio.run(_run())


def test_broadcaster_concurrent_disconnect_safe() -> None:
    """Modifying _queues during broadcast must not cause RuntimeError."""
    async def _run() -> None:
        b = SSEBroadcaster()
        q1 = b.subscribe()
        q2 = b.subscribe()
        # Remove q2 while we iterate (simulated by clearing set after snapshot)
        # The list() snapshot in broadcast() prevents RuntimeError
        b._queues.discard(q2)
        b.broadcast({"phase": "idle"})  # must not raise
        p1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        assert p1 == {"phase": "idle"}

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# T032 — keepalive comment on timeout
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T005 — build_snapshot() performers-card path (US1 coverage)
# ---------------------------------------------------------------------------


def _make_mock_daemon_with_events(
    phase: str = "monitoring_agent",
    performer_events: list[dict] | None = None,
) -> MagicMock:
    daemon = MagicMock()
    daemon.state = {
        "phase": phase,
        "error_count": 0,
        "performer_events": performer_events or [],
    }
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    return daemon


def test_build_snapshot_includes_performer_events_key() -> None:
    """build_snapshot() must always include performer_events in the output dict."""
    store = DashboardStore()
    daemon = _make_mock_daemon_with_events()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert "performer_events" in snap


def test_build_snapshot_performer_events_empty_when_no_events() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon_with_events(performer_events=[])
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["performer_events"] == []


def test_build_snapshot_performer_events_returned_when_present() -> None:
    events = [
        {"type": "tool_use", "text": "Read: src/app/main.py", "detail": ""},
        {"type": "progress", "text": "Running tests", "detail": ""},
    ]
    store = DashboardStore()
    daemon = _make_mock_daemon_with_events(performer_events=events)
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["performer_events"] == events
    assert len(snap["performer_events"]) == 2


# ---------------------------------------------------------------------------
# T006 — SSE /events endpoint includes performer_events (US1 coverage)
# ---------------------------------------------------------------------------


def test_sse_payload_includes_performer_events_when_state_has_events() -> None:
    """The SSE stream's initial state_update must include performer_events."""
    import json as _json

    events = [{"type": "tool_use", "text": "Edit: app.py", "detail": ""}]

    async def _collect() -> str:
        store = DashboardStore()
        daemon = _make_mock_daemon_with_events(performer_events=events)
        metrics = _make_mock_metrics()
        health = _make_mock_health()
        gen = store.sse_stream(daemon, metrics, health)
        try:
            first_chunk = await gen.__anext__()
        finally:
            await gen.aclose()
        return first_chunk

    first_chunk = asyncio.run(_collect())
    data_line = next(line for line in first_chunk.splitlines() if line.startswith("data:"))
    payload = _json.loads(data_line[len("data:"):].strip())
    assert "performer_events" in payload
    assert payload["performer_events"] == events


# ---------------------------------------------------------------------------
# T032 — keepalive comment on timeout
# ---------------------------------------------------------------------------


def test_sse_generator_yields_keepalive_on_timeout() -> None:
    """When asyncio.wait_for times out, sse_stream() must yield a keepalive comment."""

    async def _get_keepalive() -> str:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        metrics = _make_mock_metrics()
        health = _make_mock_health()

        async def _raise_timeout(coro, timeout=None):  # type: ignore[no-untyped-def]
            coro.close()  # prevent "coroutine was never awaited" warning
            raise TimeoutError

        with patch("coordinare.dashboard.asyncio.wait_for", _raise_timeout):
            gen = store.sse_stream(daemon, metrics, health)
            try:
                await gen.__anext__()  # initial state_update
                keepalive = await gen.__anext__()  # should be keepalive
            finally:
                await gen.aclose()
        return keepalive

    keepalive = asyncio.run(_get_keepalive())
    assert keepalive == ": keepalive\n\n", repr(keepalive)


# ---------------------------------------------------------------------------
# Broadcaster.shutdown() — sends None sentinel to all subscriber queues
# ---------------------------------------------------------------------------


def test_broadcaster_shutdown_sends_none_sentinel() -> None:
    async def _run() -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        b.shutdown()
        sentinel = await asyncio.wait_for(q.get(), timeout=1.0)
        assert sentinel is None

    asyncio.run(_run())


def test_broadcaster_shutdown_sends_sentinel_to_all_queues() -> None:
    async def _run() -> None:
        b = SSEBroadcaster()
        q1 = b.subscribe()
        q2 = b.subscribe()
        b.shutdown()
        s1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        s2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert s1 is None
        assert s2 is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# DashboardStore.shutdown() — delegates to broadcaster
# ---------------------------------------------------------------------------


def test_store_shutdown_delegates_to_broadcaster() -> None:
    store = DashboardStore()
    store.broadcaster = MagicMock()
    store.shutdown()
    store.broadcaster.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# sse_stream() None sentinel exits the generator cleanly
# ---------------------------------------------------------------------------


def test_sse_stream_exits_on_none_sentinel() -> None:
    async def _run() -> list[str]:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        metrics = _make_mock_metrics()
        health = _make_mock_health()
        gen = store.sse_stream(daemon, metrics, health)
        first = await gen.__anext__()  # initial state_update
        # Put the shutdown sentinel into the queue while generator is suspended
        store.broadcaster.shutdown()
        chunks = [first]
        try:
            async for chunk in gen:
                chunks.append(chunk)
        finally:
            await gen.aclose()
        return chunks

    chunks = asyncio.run(_run())
    assert len(chunks) == 1
    assert chunks[0].startswith("event: state_update")


# ---------------------------------------------------------------------------
# build_snapshot() exception fallback for metrics counter
# ---------------------------------------------------------------------------


def test_build_snapshot_cycles_exception_fallback() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    metrics.cycles_completed_total._value.get.side_effect = Exception("prometheus broken")
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["cycles_completed"] == 0


# ---------------------------------------------------------------------------
# build_snapshot() performer_logs from agent_service.get_agent_logs()
# ---------------------------------------------------------------------------


def test_build_snapshot_performer_logs_when_agent_active() -> None:
    store = DashboardStore()
    agent_service = MagicMock()
    agent_service.get_agent_logs.return_value = ["line one", "line two"]
    daemon = _make_mock_daemon()
    daemon.state = {
        "phase": "monitoring_agent",
        "error_count": 0,
        "agent_service": agent_service,
    }
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["performer_logs"] == ["line one", "line two"]


def test_build_snapshot_performer_logs_empty_when_no_agent() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state = {"phase": "idle", "error_count": 0}
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["performer_logs"] == []


# ---------------------------------------------------------------------------
# GET /events endpoint — returns text/event-stream
# ---------------------------------------------------------------------------


def test_events_endpoint_returns_text_event_stream() -> None:
    """GET /events must return text/event-stream with a state_update event.

    Patches sse_stream to a finite generator so TestClient doesn't hang on
    the 15-second asyncio.wait_for inside the real generator.
    """

    async def _finite_stream(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        yield "event: state_update\ndata: {}\n\n"

    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    app = create_dashboard_app(store, daemon, metrics, health)

    with patch.object(store, "sse_stream", _finite_stream):
        client = TestClient(app)
        resp = client.get("/events")

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "event: state_update" in resp.text


# ---------------------------------------------------------------------------
# GET /api/performer-logs — streaming log endpoint
# ---------------------------------------------------------------------------


def test_performer_logs_endpoint_no_agent_service() -> None:
    daemon = _make_mock_daemon()
    daemon.state = {"phase": "idle", "error_count": 0}
    client = _make_app(daemon=daemon)
    resp = client.get("/api/performer-logs")
    assert resp.status_code == 200
    assert "no performer active" in resp.text


def test_performer_logs_endpoint_streams_initial_logs() -> None:
    """Patch asyncio.sleep to a no-op and make getter raise on the second call
    so the infinite while-loop exits without hanging the test."""
    from unittest.mock import AsyncMock

    agent_service = MagicMock()
    agent_service.get_agent_logs.side_effect = [["alpha", "beta"], RuntimeError("stop")]
    daemon = _make_mock_daemon()
    daemon.state = {
        "phase": "monitoring_agent",
        "error_count": 0,
        "agent_service": agent_service,
    }
    store = DashboardStore()
    app = create_dashboard_app(store, daemon, _make_mock_metrics(), _make_mock_health())
    client = TestClient(app, raise_server_exceptions=False)
    with patch("coordinare.dashboard.asyncio.sleep", AsyncMock()):
        resp = client.get("/api/performer-logs")
    assert resp.status_code == 200
    assert "alpha" in resp.text
    assert "beta" in resp.text


# ---------------------------------------------------------------------------
# sse_stream line 164: broadcast a real payload → in-loop yield fires
# ---------------------------------------------------------------------------


def test_sse_stream_yields_broadcasted_payload() -> None:
    """Line 164: the in-loop yield fires when a real payload is broadcast."""

    async def _run() -> str:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        metrics = _make_mock_metrics()
        health = _make_mock_health()

        gen = store.sse_stream(daemon, metrics, health)
        # Consume the initial state_update (line 157)
        await gen.__anext__()
        # Now broadcast a real payload so the queue has a non-None item
        store.broadcaster.broadcast({"phase": "dispatching"})
        # This triggers line 164
        event = await gen.__anext__()
        await gen.aclose()
        return event

    event = asyncio.run(_run())
    assert "dispatching" in event
    assert event.startswith("event: state_update")


# ---------------------------------------------------------------------------
# build_snapshot line 213-214: metrics.build_info raises → started_at = None
# ---------------------------------------------------------------------------


def test_build_snapshot_started_at_falls_back_to_none_on_exception() -> None:
    """Lines 213-214: if metrics.build_info raises, started_at is None."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    health = _make_mock_health()

    bad_metrics = MagicMock()
    bad_metrics.cycles_completed_total._value.get.return_value = 0
    bad_metrics.build_info.labels.side_effect = RuntimeError("metrics exploded")

    snapshot = store.build_snapshot(daemon, bad_metrics, health)
    assert snapshot.get("daemon_start_time") is None


# ---------------------------------------------------------------------------
# 018 — Personas API endpoint tests (T016, T016b)
# ---------------------------------------------------------------------------


def _make_personas_app(tmp_path, config_path=None):
    """Build a TestClient for the dashboard with personas support.

    When no explicit config_path is given, writes a default config YAML with
    a custom implementer persona so GET /api/personas returns non-default data.
    """
    import yaml

    from coordinare.dashboard import DashboardStore, create_dashboard_app

    store = DashboardStore()
    daemon = _make_mock_daemon()

    _path = config_path or (tmp_path / "config.yaml")
    if config_path is None:
        _path.write_text(yaml.dump({
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 1,
            "github_token": "tok",
            "human_reviewers": ["alice"],
            "personas": {
                "implementer": {"instructions": "custom"},
            },
        }))

    health = _make_mock_health()
    metrics = _make_mock_metrics()
    app = create_dashboard_app(store, daemon, metrics, health, config_path=_path)
    return TestClient(app)


def test_get_personas_returns_list_of_nine_roles(tmp_path) -> None:
    """GET /api/personas returns 9 roles (042: added closer)."""
    client = _make_personas_app(tmp_path)
    res = client.get("/api/personas")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) == 9
    roles = {p["role"] for p in data}
    assert roles == {
        "advocate", "assessor", "architect", "implementer",
        "reviewer", "security", "qa", "tech_writer", "closer",
    }


def test_get_personas_is_default_flags_correct(tmp_path) -> None:
    """T016: implementer has custom instructions → is_default=False; others → True."""
    client = _make_personas_app(tmp_path)
    res = client.get("/api/personas")
    data = {p["role"]: p for p in res.json()}
    assert data["implementer"]["is_default"] is False
    assert data["assessor"]["is_default"] is True


def test_put_persona_valid_instructions_returns_200(tmp_path) -> None:
    """T016: PUT /api/personas/implementer with valid instructions returns 200."""
    import yaml
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "project_name": "Demo",
        "github_org": "acme",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
    }))
    client = _make_personas_app(tmp_path, config_path=config_path)
    res = client.put(
        "/api/personas/implementer",
        json={"instructions": "Write tests first."},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "implementer"
    assert data["instructions"] == "Write tests first."
    assert data["is_default"] is False


def test_put_persona_instructions_too_long_returns_400(tmp_path) -> None:
    """T016: PUT with instructions > 8000 chars returns 400."""
    from coordinare.config import PERSONA_MAX_LENGTH
    client = _make_personas_app(tmp_path)
    res = client.put(
        "/api/personas/implementer",
        json={"instructions": "x" * (PERSONA_MAX_LENGTH + 1)},
    )
    assert res.status_code == 400
    assert "exceed" in res.json()["error"].lower()


def test_put_persona_unknown_role_returns_404(tmp_path) -> None:
    """T016: PUT with unknown role returns 404."""
    client = _make_personas_app(tmp_path)
    res = client.put("/api/personas/wizard", json={"instructions": "Hello."})
    assert res.status_code == 404


def test_delete_persona_returns_204(tmp_path) -> None:
    """T016: DELETE /api/personas/assessor returns 204."""
    import yaml
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "project_name": "Demo",
        "github_org": "acme",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
    }))
    client = _make_personas_app(tmp_path, config_path=config_path)
    res = client.delete("/api/personas/assessor")
    assert res.status_code == 204


def test_delete_persona_unknown_role_returns_404(tmp_path) -> None:
    """T016: DELETE with unknown role returns 404."""
    client = _make_personas_app(tmp_path)
    res = client.delete("/api/personas/wizard")
    assert res.status_code == 404


def test_put_persona_save_oserror_returns_500(tmp_path) -> None:
    """T016: PUT when save_persona raises OSError returns HTTP 500 with error body (D2)."""
    from unittest.mock import patch

    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "project_name": "Demo", "github_org": "acme",
        "github_project_number": 1, "github_token": "tok",
        "human_reviewers": ["alice"],
    }))
    client = _make_personas_app(tmp_path, config_path=config_path)

    with patch(
        "coordinare.services.persona_service.save_persona",
        side_effect=OSError("disk full"),
    ) as mock_save:
        res = client.put(
            "/api/personas/implementer",
            json={"instructions": "Write tests."},
        )
    assert res.status_code == 500
    assert "error" in res.json()
    assert mock_save.called


def test_put_persona_responds_under_two_seconds(tmp_path) -> None:
    """T016b: PUT /api/personas/implementer responds in < 2 seconds (SC-003)."""
    import time

    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "project_name": "Demo",
        "github_org": "acme",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
    }))
    client = _make_personas_app(tmp_path, config_path=config_path)

    t0 = time.perf_counter()
    res = client.put(
        "/api/personas/implementer",
        json={"instructions": "Write tests first."},
    )
    elapsed = time.perf_counter() - t0

    assert res.status_code == 200
    assert elapsed < 2.0, f"PUT /api/personas took {elapsed:.3f}s (> 2s limit)"


# ---------------------------------------------------------------------------
# 031 — Human Override Controls API tests
# ---------------------------------------------------------------------------


def test_skip_role_active_card() -> None:
    """POST /api/skip-role queues skip override when card is active."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["implementing", "reviewing"]
    daemon.state["performer_stage"] = "implementing"
    client = _make_app(daemon=daemon)

    res = client.post("/api/skip-role")

    assert res.status_code == 200
    assert res.json()["status"] == "override_queued"
    assert res.json()["action"] == "skip"
    assert daemon.state["pending_override"] == {"action": "skip"}


def test_skip_role_no_active_card() -> None:
    """POST /api/skip-role returns 400 when no card is active."""
    daemon = _make_mock_daemon(phase="idle")
    client = _make_app(daemon=daemon)

    res = client.post("/api/skip-role")

    assert res.status_code == 400
    assert "No active card" in res.json()["error"]


def test_restart_from_valid_role() -> None:
    """POST /api/restart-from/{role} queues restart override for valid role."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/restart-from/reviewing")

    assert res.status_code == 200
    assert res.json()["action"] == "restart"
    assert res.json()["target_stage"] == "reviewing"
    assert daemon.state["pending_override"] == {"action": "restart", "target_stage": "reviewing"}


def test_restart_from_role_noun() -> None:
    """POST /api/restart-from/{role} accepts role noun and resolves to stage."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["implementing", "architecting", "reviewing"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/restart-from/architect")

    assert res.status_code == 200
    assert res.json()["target_stage"] == "architecting"
    assert daemon.state["pending_override"] == {"action": "restart", "target_stage": "architecting"}


def test_restart_from_invalid_role() -> None:
    """POST /api/restart-from/{role} returns 400 for unknown role."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["implementing", "reviewing"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/restart-from/nonexistent")

    assert res.status_code == 400
    assert "nonexistent" in res.json()["error"]


def test_restart_from_no_active_card() -> None:
    """POST /api/restart-from/{role} returns 400 when no card is active."""
    daemon = _make_mock_daemon(phase="idle")
    daemon.state["lifecycle_sequence"] = ["implementing"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/restart-from/implementing")

    assert res.status_code == 400


def test_veto_active_card() -> None:
    """POST /api/veto queues veto override when card is active."""
    daemon = _make_mock_daemon(phase="dispatching")
    client = _make_app(daemon=daemon)

    res = client.post("/api/veto")

    assert res.status_code == 200
    assert res.json()["action"] == "veto"
    assert daemon.state["pending_override"] == {"action": "veto"}


def test_veto_no_active_card() -> None:
    """POST /api/veto returns 400 when no card is active."""
    daemon = _make_mock_daemon(phase="idle")
    client = _make_app(daemon=daemon)

    res = client.post("/api/veto")

    assert res.status_code == 400


def test_skip_role_monitoring_agent_phase() -> None:
    """POST /api/skip-role works during monitoring_agent phase."""
    daemon = _make_mock_daemon(phase="monitoring_agent")
    daemon.state["lifecycle_sequence"] = ["implementing", "reviewing"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/skip-role")

    assert res.status_code == 200
    assert daemon.state["pending_override"] == {"action": "skip"}


# ---------------------------------------------------------------------------
# 034 — Cost & Token Tracking dashboard tests
# ---------------------------------------------------------------------------


def test_dashboard_html_includes_token_elements() -> None:
    """Dashboard HTML includes card-tokens-total and card-cost-estimate elements."""
    client = _make_app()
    res = client.get("/")
    assert res.status_code == 200
    assert "card-tokens-total" in res.text
    assert "card-cost-estimate" in res.text
    assert "Card Tokens" in res.text
    assert "Estimated Cost" in res.text


# --- 046: Dependency state in dashboard snapshot ---


def test_build_snapshot_contains_blocked_by_dependencies() -> None:
    """046 T027: The dashboard snapshot includes blocked_by_dependencies
    so the frontend can render blocker badges with issue links."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["blocked_by_dependencies"] = [
        {"issue_number": 42, "title": "Set up theming", "column": "IN_PROGRESS",
         "issue_url": "https://github.com/o/r/issues/42", "source": "explicit"},
    ]
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert "blocked_by_dependencies" in snap
    deps = snap["blocked_by_dependencies"]
    assert len(deps) == 1
    assert deps[0]["issue_number"] == 42
    assert deps[0]["column"] == "IN_PROGRESS"
    assert deps[0]["source"] == "explicit"


def test_build_snapshot_blocked_by_dependencies_empty_when_none() -> None:
    """046: When no dependencies are blocking, the field is an empty list."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["blocked_by_dependencies"] == []


# --- 047: Rebase status in dashboard snapshot ---


def test_build_snapshot_contains_last_rebase_round() -> None:
    """047 T032: Dashboard snapshot includes last_rebase_round."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["last_rebase_round"] = {
        "trigger_pr_number": 42,
        "trigger_sha": "abc123",
        "timestamp": "2026-04-17T12:00:00Z",
        "jobs": [
            {"card_id": "CARD_A", "branch": "coordinare/PVTI_A/feat", "outcome": "clean",
             "post_rebase_sha": "def456", "conflicted_files": [], "duration_seconds": 5.0},
        ],
    }
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert "last_rebase_round" in snap
    rr = snap["last_rebase_round"]
    assert rr["trigger_pr_number"] == 42
    assert len(rr["jobs"]) == 1
    assert rr["jobs"][0]["outcome"] == "clean"


def test_build_snapshot_last_rebase_round_none_when_absent() -> None:
    """047: When no rebase has occurred, the field is None."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["last_rebase_round"] is None


# --- 048: Role utilization in dashboard snapshot ---


def test_build_snapshot_role_utilization_with_slot_manager() -> None:
    """048: Dashboard snapshot includes role_utilization from SlotManager."""
    from unittest.mock import MagicMock

    from coordinare.services.slot_manager import SlotManager

    sm = SlotManager()
    sm.register_pool("implementing", [MagicMock(), MagicMock()], max_concurrency=2)
    sm.acquire("implementing", "CARD_A")

    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["slot_manager"] = sm
    daemon.state["active_sessions"] = {
        "CARD_B": {"phase": "dispatching", "performer_stage": "implementing"},
    }
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert "role_utilization" in snap
    util = snap["role_utilization"]
    assert len(util) == 1
    assert util[0]["role"] == "implementing"
    assert util[0]["active"] == 1
    assert util[0]["max"] == 2
    assert util[0]["queued"] == 1  # CARD_B is in dispatching phase


def test_build_snapshot_role_utilization_empty_without_slot_manager() -> None:
    """048: No SlotManager → empty utilization list."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["role_utilization"] == []


# --- 050: assignee_filter snapshot tests ---


def test_build_snapshot_assignee_filter_present() -> None:
    """050: assignee_filter is included in snapshot when configured."""
    from types import SimpleNamespace
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["config"] = SimpleNamespace(
        assignee_filter="coordinare-bot",
        project_name="Demo",
        github_org="acme",
        github_project_number=1,
    )
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["assignee_filter"] == "coordinare-bot"


def test_build_snapshot_assignee_filter_none_when_not_configured() -> None:
    """050: assignee_filter is null in snapshot when not configured."""
    from types import SimpleNamespace
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["config"] = SimpleNamespace(
        assignee_filter=None,
        project_name="Demo",
        github_org="acme",
        github_project_number=1,
    )
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["assignee_filter"] is None


# ---------------------------------------------------------------------------
# 054: session_skip_reasons in snapshot (T020)
# ---------------------------------------------------------------------------


def test_build_snapshot_session_skip_reasons_populated() -> None:
    """054 T020: session_skip_reasons dict is included in the snapshot."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["session_skip_reasons"] = {
        "card-A": {"reason": "blocked_column", "blockers": []},
        "card-B": {"reason": "dependency_blocked", "blockers": [42]},
    }
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert "session_skip_reasons" in snap
    assert snap["session_skip_reasons"]["card-A"]["reason"] == "blocked_column"
    assert snap["session_skip_reasons"]["card-B"]["blockers"] == [42]


def test_build_snapshot_session_skip_reasons_empty_when_absent() -> None:
    """054 T020: session_skip_reasons defaults to empty dict when not in state."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    # Ensure key is absent
    daemon.state.pop("session_skip_reasons", None)
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap.get("session_skip_reasons") == {}


def test_build_snapshot_session_skip_reasons_is_copy() -> None:
    """054 T020: snapshot returns a copy so mutations don't affect daemon state."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    original = {"card-X": {"reason": "blocked_column"}}
    daemon.state["session_skip_reasons"] = original
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)
    snap["session_skip_reasons"]["card-Y"] = {"reason": "extra"}

    assert "card-Y" not in original


# ---------------------------------------------------------------------------
# 057/058 — Symphony CRUD HTTP endpoint tests (FR-008 to FR-011)
# ---------------------------------------------------------------------------


def _make_symphony_app(tmp_path):
    """Build a TestClient with two pre-loaded symphonies."""
    import yaml

    from coordinare.config import SymphonyConfig
    from coordinare.dashboard import DashboardStore, create_dashboard_app

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "github_org": "acme",
        "github_token": "tok",
        "human_reviewers": ["alice"],
        "symphonies": [
            {"name": "alpha", "github_project_number": 10},
            {"name": "beta", "github_project_number": 20},
        ],
    }))

    daemon = _make_mock_daemon()
    daemon.state["symphony_configs"] = {
        "alpha": SymphonyConfig(name="alpha", github_project_number=10),
        "beta": SymphonyConfig(name="beta", github_project_number=20),
    }

    store = DashboardStore()
    health = _make_mock_health()
    metrics = _make_mock_metrics()
    app = create_dashboard_app(store, daemon, metrics, health, config_path=config_path)
    return TestClient(app), daemon


def test_symphonies_list_includes_bootstrap_status(tmp_path) -> None:
    """077: the overview list payload carries per-symphony bootstrap status so the
    dashboard shows an at-a-glance badge (and the failure reason on hover)."""
    from pathlib import Path

    from coordinare.models.env_cache import EnvCacheState

    client, daemon = _make_symphony_app(tmp_path)
    daemon.state["env_cache"] = {
        "alpha": EnvCacheState(
            symphony_name="alpha",
            sanitised_name="alpha",
            cache_dir=Path(tmp_path) / "alpha",
            cache_dir_ready=True,
            last_bootstrap_succeeded=False,
            last_bootstrap_error="FAIL: Gem rails is not installed/available",
            bootstrap_in_flight=False,
        ),
    }
    res = client.get("/api/symphonies")
    assert res.status_code == 200
    syms = {s["name"]: s for s in res.json()["symphonies"]}
    assert syms["alpha"]["last_bootstrap_succeeded"] is False
    assert syms["alpha"]["last_bootstrap_error"] == "FAIL: Gem rails is not installed/available"
    assert syms["alpha"]["cache_dir_ready"] is True
    assert syms["alpha"]["bootstrap_in_flight"] is False
    # A symphony with no env_cache entry gets safe defaults (no error).
    assert syms["beta"]["last_bootstrap_error"] is None


def test_post_symphony_creates_new_entry(tmp_path) -> None:
    """058 FR-008: POST /api/symphonies creates a new symphony and returns 201."""
    client, daemon = _make_symphony_app(tmp_path)
    res = client.post("/api/symphonies", json={"name": "gamma", "github_project_number": 30})
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "gamma"
    assert data["github_project_number"] == 30
    assert "gamma" in daemon.state["symphony_configs"]


def test_post_symphony_duplicate_name_returns_409(tmp_path) -> None:
    """058 FR-008: POST /api/symphonies with an existing name returns 409."""
    client, _ = _make_symphony_app(tmp_path)
    res = client.post("/api/symphonies", json={"name": "alpha", "github_project_number": 99})
    assert res.status_code == 409


def test_post_symphony_missing_name_returns_400(tmp_path) -> None:
    """058 FR-008: POST /api/symphonies without name returns 400."""
    client, _ = _make_symphony_app(tmp_path)
    res = client.post("/api/symphonies", json={"github_project_number": 30})
    assert res.status_code == 400
    assert "name" in res.json()["error"].lower()


def test_post_symphony_missing_project_number_returns_400(tmp_path) -> None:
    """058 FR-008: POST /api/symphonies without github_project_number returns 400."""
    client, _ = _make_symphony_app(tmp_path)
    res = client.post("/api/symphonies", json={"name": "delta"})
    assert res.status_code == 400


def test_post_symphony_cycle_active_returns_409(tmp_path) -> None:
    """058 FR-008: POST /api/symphonies returns 409 when a cycle is running."""
    client, daemon = _make_symphony_app(tmp_path)
    daemon._cycle_active = True
    res = client.post("/api/symphonies", json={"name": "new", "github_project_number": 50})
    assert res.status_code == 409


def test_put_symphony_updates_enabled_flag(tmp_path) -> None:
    """058 FR-009: PUT /api/symphonies/{name} with enabled=False disables the symphony."""
    client, daemon = _make_symphony_app(tmp_path)
    res = client.put("/api/symphonies/alpha", json={"enabled": False})
    assert res.status_code == 200
    data = res.json()
    assert data["enabled"] is False
    assert daemon.state["symphony_configs"]["alpha"].enabled is False


def test_put_symphony_updates_overrides(tmp_path) -> None:
    """058 FR-009: PUT /api/symphonies/{name} accepts and persists overrides."""
    client, _daemon = _make_symphony_app(tmp_path)
    res = client.put("/api/symphonies/beta", json={"overrides": {"poll_interval_seconds": 45}})
    assert res.status_code == 200
    data = res.json()
    assert data["overrides"]["poll_interval_seconds"] == 45


def test_put_symphony_not_found_returns_404(tmp_path) -> None:
    """058 FR-009: PUT /api/symphonies/{name} for unknown name returns 404."""
    client, _ = _make_symphony_app(tmp_path)
    res = client.put("/api/symphonies/nonexistent", json={"enabled": True})
    assert res.status_code == 404


def test_put_symphony_cycle_active_returns_409(tmp_path) -> None:
    """058 FR-009: PUT /api/symphonies/{name} returns 409 when a cycle is running."""
    client, daemon = _make_symphony_app(tmp_path)
    daemon._cycle_active = True
    res = client.put("/api/symphonies/alpha", json={"enabled": False})
    assert res.status_code == 409


# ---------------------------------------------------------------------------
# 061 — Manual env_bootstrap trigger (POST /api/symphonies/{name}/env-bootstrap)
# ---------------------------------------------------------------------------


def _attach_env_cache(daemon, symphony: str, *, in_flight: bool = False) -> None:
    """Configure the symphony for env_bootstrap and seed an EnvCacheState."""
    from pathlib import Path

    from coordinare.models.env_cache import EnvCacheState

    cfg = daemon.state["symphony_configs"][symphony]
    cfg.env_bootstrap_performer_id = "codex-ephemeral"
    daemon.state["env_cache_service"] = MagicMock()
    daemon.state["performer_services_by_id"] = {"codex-ephemeral": MagicMock()}
    daemon.state["env_cache"] = {
        symphony: EnvCacheState(
            symphony_name=symphony,
            sanitised_name=f"{symphony}-abc123",
            cache_dir=Path(f"/tmp/env-caches/{symphony}-abc123"),
            readme_sha="cafef00d",
            bootstrap_in_flight=in_flight,
        )
    }
    daemon._webhook_trigger = MagicMock()


def test_force_env_bootstrap_clears_sha_and_fires_trigger(tmp_path) -> None:
    client, daemon = _make_symphony_app(tmp_path)
    _attach_env_cache(daemon, "alpha")
    res = client.post("/api/symphonies/alpha/env-bootstrap")
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "accepted"
    assert body["symphony"] == "alpha"
    assert body["performer_id"] == "codex-ephemeral"
    assert daemon.state["env_cache"]["alpha"].readme_sha is None
    daemon._webhook_trigger.set.assert_called_once()


def test_force_env_bootstrap_unknown_symphony_returns_404(tmp_path) -> None:
    client, _ = _make_symphony_app(tmp_path)
    res = client.post("/api/symphonies/missing/env-bootstrap")
    assert res.status_code == 404


def test_force_env_bootstrap_no_performer_returns_400(tmp_path) -> None:
    """Symphonies without env_bootstrap_performer_id can't be bootstrapped."""
    client, _ = _make_symphony_app(tmp_path)
    res = client.post("/api/symphonies/alpha/env-bootstrap")
    assert res.status_code == 400


def test_force_env_bootstrap_in_flight_returns_409(tmp_path) -> None:
    client, daemon = _make_symphony_app(tmp_path)
    _attach_env_cache(daemon, "alpha", in_flight=True)
    res = client.post("/api/symphonies/alpha/env-bootstrap")
    assert res.status_code == 409
    # SHA must NOT be cleared while a bootstrap is in flight.
    assert daemon.state["env_cache"]["alpha"].readme_sha == "cafef00d"


def test_force_env_bootstrap_no_cache_state_returns_503(tmp_path) -> None:
    """If initialise() hasn't run yet, return 503 rather than guessing."""
    client, daemon = _make_symphony_app(tmp_path)
    cfg = daemon.state["symphony_configs"]["alpha"]
    cfg.env_bootstrap_performer_id = "codex-ephemeral"
    daemon.state["env_cache_service"] = MagicMock()
    daemon.state["performer_services_by_id"] = {"codex-ephemeral": MagicMock()}
    daemon.state["env_cache"] = {}
    res = client.post("/api/symphonies/alpha/env-bootstrap")
    assert res.status_code == 503


def test_force_env_bootstrap_missing_performer_service_returns_503(tmp_path) -> None:
    """If the configured bootstrap performer is not registered with the daemon
    (e.g. typo in config.yaml, half-initialised startup), surface that directly
    instead of accepting the 202 and silently failing on the next cycle."""
    client, daemon = _make_symphony_app(tmp_path)
    cfg = daemon.state["symphony_configs"]["alpha"]
    cfg.env_bootstrap_performer_id = "codex-ephemeral"
    daemon.state["env_cache_service"] = MagicMock()
    daemon.state["performer_services_by_id"] = {}
    res = client.post("/api/symphonies/alpha/env-bootstrap")
    assert res.status_code == 503
    body = res.json()
    assert "codex-ephemeral" in body["error"]
    assert body["performer_id"] == "codex-ephemeral"


def test_get_symphony_includes_env_cache_state(tmp_path) -> None:
    client, daemon = _make_symphony_app(tmp_path)
    _attach_env_cache(daemon, "alpha")
    res = client.get("/api/symphonies/alpha")
    assert res.status_code == 200
    body = res.json()
    assert body["env_bootstrap_performer_id"] == "codex-ephemeral"
    assert body["env_cache"] is not None
    assert body["env_cache"]["readme_sha"] == "cafef00d"
    assert body["env_cache"]["sanitised_name"] == "alpha-abc123"
    assert body["env_cache"]["bootstrap_in_flight"] is False


def test_list_symphonies_includes_env_bootstrap_performer_id(tmp_path) -> None:
    """060: GET /api/symphonies returns env_bootstrap_performer_id per row so the
    list page can render the per-row Bootstrap button."""
    client, daemon = _make_symphony_app(tmp_path)
    cfg = daemon.state["symphony_configs"]["alpha"]
    cfg.env_bootstrap_performer_id = "codex-ephemeral"
    res = client.get("/api/symphonies")
    assert res.status_code == 200
    syms = res.json()["symphonies"]
    alpha = next(s for s in syms if s["name"] == "alpha")
    assert alpha["env_bootstrap_performer_id"] == "codex-ephemeral"


def test_dashboard_html_renders_per_row_bootstrap_button() -> None:
    """060: the symphonies list rendering includes the sym-row-bootstrap button class
    and wires its click to the env-bootstrap endpoint."""
    assert "sym-row-bootstrap" in _DASHBOARD_HTML
    assert "/env-bootstrap" in _DASHBOARD_HTML


def test_build_snapshot_symphonies_include_env_bootstrap_performer_id() -> None:
    """060: SSE snapshot must include env_bootstrap_performer_id per symphony so
    the symphonies list page renders the per-row Bootstrap button. Without this,
    the button never appears even though the REST API returns the field."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    cfg = MagicMock()
    cfg.github_project_number = 42
    cfg.env_bootstrap_performer_id = "codex-ephemeral"
    daemon.state["symphony_configs"] = {"alpha": cfg}
    daemon.state["symphony_states"] = {}
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["symphonies"][0]["env_bootstrap_performer_id"] == "codex-ephemeral"


# ---------------------------------------------------------------------------
# GET /api/config/global
# ---------------------------------------------------------------------------


def test_get_global_config_returns_editable_fields() -> None:
    """GET /api/config/global returns the editable global config fields from state."""
    daemon = _make_mock_daemon()
    cfg = MagicMock()
    cfg.poll_interval_seconds = 30
    cfg.max_concurrent_cards = 2
    cfg.log_level = "INFO"
    for attr in ("heartbeat_interval_seconds", "max_feedback_cycles",
                 "max_closed_pr_attempts_per_issue", "output_mode",
                 "conducting_backend", "assignee_filter",
                 "human_reviewers", "trusted_bot_reviewers",
                 "env_cache_root"):
        setattr(cfg, attr, None)
    daemon.state["config"] = cfg
    client = _make_app(daemon=daemon)
    resp = client.get("/api/config/global")
    assert resp.status_code == 200
    data = resp.json()
    assert data["poll_interval_seconds"] == 30
    assert data["max_concurrent_cards"] == 2
    assert data["log_level"] == "INFO"


def test_get_global_config_no_config_returns_500() -> None:
    """GET /api/config/global returns 500 when config is unavailable."""
    daemon = _make_mock_daemon()
    daemon.state.pop("config", None)
    client = _make_app(daemon=daemon)
    resp = client.get("/api/config/global")
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# PUT /api/config/global
# ---------------------------------------------------------------------------


def test_put_global_config_no_config_path_returns_503() -> None:
    """PUT /api/config/global returns 503 when no config file is configured."""
    client = _make_app()
    resp = client.put("/api/config/global", json={"max_concurrent_cards": 3})
    assert resp.status_code == 503


def test_put_global_config_unknown_field_returns_400(tmp_path) -> None:
    """PUT /api/config/global rejects unknown (non-editable) fields."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("github_org: testorg\ngithub_project_number: 1\n")
    store = DashboardStore()
    daemon = _make_mock_daemon()
    app = create_dashboard_app(store, daemon, _make_mock_metrics(), _make_mock_health(), config_path=config_file)
    resp = TestClient(app).put("/api/config/global", json={"not_a_real_field": "value"})
    assert resp.status_code == 400
    assert "not_a_real_field" in resp.json()["error"]


def test_put_global_config_persists_valid_field(tmp_path) -> None:
    """PUT /api/config/global writes updated fields to config.yaml."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "github_org: testorg\ngithub_project_number: 1\n"
        "poll_interval_seconds: 30\nhuman_reviewers:\n  - reviewer1\n"
        "github_token: dummy_token\n"
    )
    store = DashboardStore()
    daemon = _make_mock_daemon()
    app = create_dashboard_app(store, daemon, _make_mock_metrics(), _make_mock_health(), config_path=config_file)
    resp = TestClient(app).put("/api/config/global", json={"poll_interval_seconds": 60})
    assert resp.status_code == 200
    assert resp.json()["status"] == "saved"
    saved = yaml.safe_load(config_file.read_text())
    assert saved["poll_interval_seconds"] == 60


# ---------------------------------------------------------------------------
# render_performer_pool_widget
# ---------------------------------------------------------------------------


class _FakeState:
    def __init__(self, id: str, availability: str, excluded: bool = False) -> None:
        self.id = id
        self.mode = "local"
        self.availability = availability
        self.endpoint = None
        self.current_job_id = None
        self.capabilities = None
        self.consecutive_failures = 0
        self.excluded_until_recovery = excluded
        self.last_status_at = None


class _FakePool:
    def __init__(self, states: list) -> None:
        self._states = states

    def list_all(self) -> list:
        return self._states


def test_render_performer_pool_widget_none_pool() -> None:
    result = render_performer_pool_widget(None)
    assert result["total_registered"] == 0
    assert result["total_idle"] == 0
    assert result["total_busy"] == 0
    assert result["total_excluded"] == 0
    assert result["performers"] == []


def test_render_performer_pool_widget_idle() -> None:
    pool = _FakePool([_FakeState("p1", "idle")])
    result = render_performer_pool_widget(pool)
    assert result["total_registered"] == 1
    assert result["total_idle"] == 1
    assert result["total_busy"] == 0
    assert result["total_excluded"] == 0


def test_render_performer_pool_widget_busy() -> None:
    pool = _FakePool([_FakeState("p1", "busy")])
    result = render_performer_pool_widget(pool)
    assert result["total_busy"] == 1
    assert result["total_idle"] == 0


def test_render_performer_pool_widget_excluded() -> None:
    pool = _FakePool([_FakeState("p1", "idle", excluded=True)])
    result = render_performer_pool_widget(pool)
    assert result["total_excluded"] == 1
    assert result["performers"][0]["excluded_until_recovery"] is True


def test_render_performer_pool_widget_mixed() -> None:
    pool = _FakePool([
        _FakeState("p1", "idle"),
        _FakeState("p2", "busy"),
        _FakeState("p3", "idle", excluded=True),
    ])
    result = render_performer_pool_widget(pool)
    assert result["total_registered"] == 3
    assert result["total_idle"] == 2
    assert result["total_busy"] == 1
    assert result["total_excluded"] == 1


# ---------------------------------------------------------------------------
# 066 T012 — SSE payload preservation across FR-010 current_card demotion.
#
# After FR-010, top-level ``current_card`` is a *derived* mirror of
# ``active_sessions[active_card_id]["current_card"]``.  The dashboard SSE
# payload is the most visible read-site, so this regression guards the fields
# the dashboard reads from per-session state (feedback_cycle_count,
# performer_stage on the session row, card_title) AND the top-level
# performer_stage which build_snapshot still reads off daemon.state.
# ---------------------------------------------------------------------------


def test_build_snapshot_preserves_fields_under_session_mirror() -> None:
    """066 T012 / FR-010: build_snapshot must surface per-session
    feedback_cycle_count, performer_stage, and card title from the
    active_sessions entry — not from a separate top-level current_card —
    so the multi-card unified path remains the source of truth."""
    store = DashboardStore()
    daemon = _make_mock_daemon(phase="monitoring_performer")

    card = {
        "id": "card-066",
        "title": "Unify pickup",
        "issue_number": 66,
        "issue_url": "https://github.com/x/y/issues/66",
    }
    daemon.state["active_card_id"] = "card-066"
    daemon.state["active_sessions"] = {
        "card-066": {
            "current_card": card,
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "feedback_cycle_count": 2,
            "total_feedback_cycles": 5,
            "triage_blocks": 0,
            "card_tokens_total": 1234,
            "card_cost_estimate": 0.42,
            "agent_dispatch": {"container_id": "abc123"},
        }
    }
    # FR-010 mirror: top-level current_card matches the session.
    daemon.state["current_card"] = card
    # Top-level performer_stage is still read by build_snapshot directly.
    daemon.state["performer_stage"] = "implementing"

    # Stub last_snapshot so active_card_title / column come through.
    persisted = MagicMock()
    persisted.phase = "monitoring_performer"
    persisted.active_card_title = "Unify pickup"
    persisted.active_card_column = "IN_PROGRESS"
    persisted.pr_url = None
    persisted.agent_session_id = None
    persisted.open_questions = []
    persisted.card_clarifications = []
    daemon.state_store.last_snapshot = persisted

    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    # Top-level fields survive the demotion.
    assert snap["performer_stage"] == "implementing"
    assert snap["active_card_title"] == "Unify pickup"
    assert snap["active_card_column"] == "IN_PROGRESS"
    assert snap["active_card_issue_url"] == "https://github.com/x/y/issues/66"

    # Per-session row reflects the authoritative session dict.
    assert snap["active_session_count"] == 1
    row = snap["active_sessions"][0]
    assert row["card_id"] == "card-066"
    assert row["card_title"] == "Unify pickup"
    assert row["performer_stage"] == "implementing"
    assert row["feedback_cycle_count"] == 2
    assert row["total_feedback_cycles"] == 5
    assert row["container_id"] == "abc123"


# --- 081 T001: Config reference fields render as dropdowns ---


def test_config_reference_fields_render_as_dropdowns() -> None:
    """The five catalog-reference fields are wired to render as <select> dropdowns."""
    # Static label -> catalog map drives which fields become dropdowns.
    assert "_CFG_REFS" in _DASHBOARD_HTML
    assert "cfgRefSelect" in _DASHBOARD_HTML
    assert "_cfgOptions" in _DASHBOARD_HTML
    # Every reference field label and its target catalog is declared.
    for pair in (
        "endpoint:", "tool:", "thinking:", "classifier:", "mode:",
    ):
        assert pair in _DASHBOARD_HTML, f"missing _CFG_REFS entry {pair}"
    # Nullable references offer an explicit none option.
    assert "\\u2014 none \\u2014" in _DASHBOARD_HTML or "— none —" in _DASHBOARD_HTML
    # Selects carry data-ref so coercion and option-source are discoverable in the DOM.
    assert 'data-ref="' in _DASHBOARD_HTML


def test_config_options_index_built_from_payload() -> None:
    """loadConfigPage rebuilds the dropdown option index from the fetched sections."""
    assert "cfgBuildOptions" in _DASHBOARD_HTML
    # The builder is invoked during load (before sections are rendered).
    assert "_cfgOptions = cfgBuildOptions(" in _DASHBOARD_HTML
    # Options carry both the stored value and a display hint.
    assert "cfgOptHint(" in _DASHBOARD_HTML


def test_config_empty_reference_coerces_to_null() -> None:
    """An empty reference <select> (— none —) coerces to null, not an empty string."""
    # cfgCoerce special-cases controls carrying data-ref with an empty value.
    assert "getAttribute('data-ref')" in _DASHBOARD_HTML
    # The null branch appears in the coercion path.
    assert "return null" in _DASHBOARD_HTML


def test_config_page_renders_tabbed_layout() -> None:
    """The config page renders a tablist + one tabpanel per section, not a long scroll."""
    assert "cfgTabBar" in _DASHBOARD_HTML
    assert "cfgSelectTab" in _DASHBOARD_HTML
    assert 'role="tablist"' in _DASHBOARD_HTML
    assert 'role="tab"' in _DASHBOARD_HTML
    assert 'role="tabpanel"' in _DASHBOARD_HTML
    # Tabs reuse the existing swimlane tab styling — no new visual language.
    assert "swimlane-tabs" in _DASHBOARD_HTML
    # Each section is wrapped in a panel and the active tab is hash-derived.
    assert "cfgPanelId(" in _DASHBOARD_HTML
    assert "location.hash" in _DASHBOARD_HTML


def test_config_tabs_support_keyboard_navigation() -> None:
    """Tab strip supports Arrow/Home/End keyboard navigation (WAI-ARIA tabs)."""
    assert "function cfgTabKey(" in _DASHBOARD_HTML
    assert "ArrowRight" in _DASHBOARD_HTML
    assert "ArrowLeft" in _DASHBOARD_HTML
    assert "Home" in _DASHBOARD_HTML
    assert "End" in _DASHBOARD_HTML
