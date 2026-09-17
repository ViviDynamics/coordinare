"""094: restart-time board reconciliation for restored per-card sessions.

`_reconcile_with_board` historically reconciled only the top-level
`active_card_id` focus. In multi-card mode the authoritative state lives in
`active_sessions`, whose `phase` is restored verbatim — so a session restored
BLOCKED/idle for a card the board has moved to TODO stayed wedged (the #158
incident). These tests pin the per-session reconciliation: board wins,
in-flight sessions are preserved, truly-blocked stays blocked, corrections are
observable and convergent, and a failed board read changes nothing.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from coordinare.daemon import CoordinareDaemon
from coordinare.state_store import WorkflowSnapshot


async def _no_sleep(_: int) -> None:
    return None


def _make_daemon() -> CoordinareDaemon:
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})
    return CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
    )


def _github_with_board(board_snapshot: dict) -> MagicMock:
    gh = MagicMock()
    gh.project_id = 123  # truthy → used directly (no per-symphony fallback)
    gh.poll_board = AsyncMock(return_value={"snapshot": board_snapshot})
    return gh


def _session(card_id: str, *, phase: str, stage: str = "implementing", **extra) -> dict:
    sess = {
        "current_card": {"id": card_id, "content_id": card_id},
        "performer_stage": stage,
        "phase": phase,
    }
    sess.update(extra)
    return sess


def _snapshot(
    active_card_id: str | None = None,
    phase: str = "idle",
    active_card_column: str | None = None,
) -> WorkflowSnapshot:
    return WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase=phase,
        active_card_id=active_card_id,
        active_card_column=active_card_column,
    )


def _install(daemon: CoordinareDaemon, sessions: dict, board: dict, active_card_id=None) -> None:
    daemon._state["github_service"] = _github_with_board(board)
    daemon._state["active_sessions"] = sessions
    if active_card_id is not None:
        daemon._state["active_card_id"] = active_card_id
        daemon._state["current_card"] = sessions[active_card_id]["current_card"]


# --- US1: a re-opened card resumes instead of wedging -----------------------

@pytest.mark.asyncio
async def test_blocked_session_for_todo_card_becomes_dispatch_eligible() -> None:
    """#158 repro: session restored BLOCKED, board now TODO → phase corrected
    so the card is no longer wedged (resolves to the board-inferred resting
    phase, not 'blocked')."""
    daemon = _make_daemon()
    sessions = {"CARD158": _session("CARD158", phase="blocked")}
    board = {"TODO": ["CARD158"], "BLOCKED": []}
    _install(daemon, sessions, board, active_card_id="CARD158")

    await daemon._reconcile_with_board(_snapshot(active_card_id="CARD158", phase="blocked"))

    assert daemon._state["active_sessions"]["CARD158"]["phase"] != "blocked"
    assert daemon._state["active_sessions"]["CARD158"]["phase"] == "idle"


@pytest.mark.asyncio
async def test_idle_wedged_session_for_todo_card_is_unwedged() -> None:
    """The idle variant of the wedge: a non-focus session stuck idle for a
    TODO card is reconciled (still idle == board-inferred, but the path runs
    without error and leaves it dispatch-eligible)."""
    daemon = _make_daemon()
    sessions = {
        "FOCUS": _session("FOCUS", phase="monitoring_pr"),
        "CARD158": _session("CARD158", phase="blocked"),
    }
    board = {"TODO": ["CARD158"], "IN_REVIEW": ["FOCUS"], "BLOCKED": []}
    _install(daemon, sessions, board, active_card_id="FOCUS")

    await daemon._reconcile_with_board(_snapshot(active_card_id="FOCUS", phase="monitoring_pr"))

    assert daemon._state["active_sessions"]["CARD158"]["phase"] == "idle"


@pytest.mark.asyncio
async def test_truly_blocked_card_stays_blocked() -> None:
    """FR-005/SC-005: board genuinely BLOCKED → session stays blocked, never
    force-dispatched."""
    daemon = _make_daemon()
    sessions = {"CARD": _session("CARD", phase="blocked")}
    board = {"BLOCKED": ["CARD"], "TODO": []}
    _install(daemon, sessions, board, active_card_id="CARD")

    await daemon._reconcile_with_board(_snapshot(active_card_id="CARD", phase="blocked"))

    assert daemon._state["active_sessions"]["CARD"]["phase"] == "blocked"


# --- US2: in-flight work survives reconciliation ----------------------------

@pytest.mark.asyncio
async def test_monitoring_pr_session_preserved() -> None:
    """FR-004/SC-003: a monitoring_pr session whose board column is consistent
    keeps its PR context and phase."""
    daemon = _make_daemon()
    sessions = {
        "PR": _session("PR", phase="monitoring_pr", pr_url="https://x/pull/7", pr_node_id="PR_node"),
    }
    board = {"IN_REVIEW": ["PR"]}
    _install(daemon, sessions, board, active_card_id="PR")

    await daemon._reconcile_with_board(_snapshot(active_card_id="PR", phase="monitoring_pr"))

    sess = daemon._state["active_sessions"]["PR"]
    assert sess["phase"] == "monitoring_pr"
    assert sess["pr_url"] == "https://x/pull/7"
    assert sess["pr_node_id"] == "PR_node"


@pytest.mark.asyncio
async def test_monitoring_performer_session_preserved() -> None:
    """FR-004/SC-003: a live-performer session is not demoted to an earlier
    stage and keeps its session linkage."""
    daemon = _make_daemon()
    sessions = {
        "PERF": _session(
            "PERF", phase="monitoring_performer", stage="implementing",
            agent_dispatch={"session_id": "sess-xyz"},
        ),
    }
    board = {"IN_PROGRESS": ["PERF"]}
    _install(daemon, sessions, board, active_card_id="PERF")

    await daemon._reconcile_with_board(_snapshot(active_card_id="PERF", phase="monitoring_performer"))

    sess = daemon._state["active_sessions"]["PERF"]
    assert sess["phase"] == "monitoring_performer"
    assert sess["performer_stage"] == "implementing"
    assert sess["agent_dispatch"]["session_id"] == "sess-xyz"


# --- US3: corrections are diagnosable ---------------------------------------

@pytest.mark.asyncio
async def test_correction_emits_secretfree_event() -> None:
    """FR-006/FR-007/SC-004: each correction emits one structured event whose
    fields are ids/columns/phases only — no secret values."""
    daemon = _make_daemon()
    sessions = {"CARD": _session("CARD", phase="blocked")}
    board = {"TODO": ["CARD"]}
    _install(daemon, sessions, board, active_card_id="CARD")

    with structlog.testing.capture_logs() as logs:
        await daemon._reconcile_with_board(
            _snapshot(active_card_id="CARD", phase="blocked", active_card_column="BLOCKED"),
        )

    events = [e for e in logs if e.get("event") == "restart_reconcile.session_corrected"]
    assert len(events) == 1
    ev = events[0]
    assert ev["card_id"] == "CARD"
    assert ev["prior_phase"] == "blocked"
    # The focus card's persisted column must appear (not None) — FR-006/SC-004.
    assert ev["prior_column"] == "BLOCKED"
    assert ev["board_column"] == "TODO"
    assert ev["corrected_phase"] == "idle"
    # Only ids/columns/phases/symphony allowed — no secret-shaped keys/values.
    allowed = {"event", "log_level", "card_id", "prior_phase", "prior_column",
               "board_column", "corrected_phase", "symphony"}
    assert set(ev) <= allowed, f"unexpected fields: {set(ev) - allowed}"
    blob = repr(ev).lower()
    for needle in ("token", "secret", "password", "api_key", "ghp_"):
        assert needle not in blob


# --- Polish / edge cases ----------------------------------------------------

@pytest.mark.asyncio
async def test_done_or_absent_card_session_retired() -> None:
    """FR-010: card DONE or absent from a successful board read → session
    retired; top-level focus cleared if it pointed there."""
    daemon = _make_daemon()
    sessions = {"GONE": _session("GONE", phase="monitoring_performer")}
    board = {"TODO": ["OTHER"]}  # GONE absent
    _install(daemon, sessions, board, active_card_id="GONE")

    await daemon._reconcile_with_board(_snapshot(active_card_id="GONE", phase="monitoring_performer"))

    assert "GONE" not in daemon._state.get("active_sessions", {})
    assert daemon._state.get("active_card_id") != "GONE"


@pytest.mark.asyncio
async def test_board_poll_failure_preserves_all_sessions() -> None:
    """FR-009: a failed board read modifies/deletes nothing and does not raise."""
    daemon = _make_daemon()
    sessions = {"A": _session("A", phase="blocked"), "B": _session("B", phase="monitoring_pr")}
    gh = MagicMock()
    gh.project_id = 123
    gh.poll_board = AsyncMock(side_effect=RuntimeError("github down"))
    daemon._state["github_service"] = gh
    daemon._state["active_sessions"] = sessions
    daemon._state["active_card_id"] = "A"

    await daemon._reconcile_with_board(_snapshot(active_card_id="A", phase="blocked"))

    assert daemon._state["active_sessions"]["A"]["phase"] == "blocked"
    assert daemon._state["active_sessions"]["B"]["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_reconcile_is_convergent_noop_on_consistent_state() -> None:
    """FR-008/SC-006: reconciling already-consistent state emits no corrections."""
    daemon = _make_daemon()
    sessions = {"CARD": _session("CARD", phase="blocked")}
    board = {"TODO": ["CARD"]}
    _install(daemon, sessions, board, active_card_id="CARD")

    # First pass corrects blocked → idle.
    await daemon._reconcile_with_board(_snapshot(active_card_id="CARD", phase="blocked"))
    assert daemon._state["active_sessions"]["CARD"]["phase"] == "idle"

    # Second pass on the corrected state: idempotent, zero correction events.
    with structlog.testing.capture_logs() as logs:
        await daemon._reconcile_with_board(_snapshot(active_card_id="CARD", phase="idle"))
    corrections = [e for e in logs if e.get("event") == "restart_reconcile.session_corrected"]
    assert corrections == []
    assert daemon._state["active_sessions"]["CARD"]["phase"] == "idle"


@pytest.mark.asyncio
async def test_multiple_diverging_sessions_corrected_independently() -> None:
    """Edge: more than one session diverges → each corrected in one pass."""
    daemon = _make_daemon()
    sessions = {
        "A": _session("A", phase="blocked"),
        "B": _session("B", phase="blocked"),
    }
    board = {"TODO": ["A"], "IN_REVIEW": ["B"]}
    _install(daemon, sessions, board, active_card_id="A")

    await daemon._reconcile_with_board(_snapshot(active_card_id="A", phase="blocked"))

    assert daemon._state["active_sessions"]["A"]["phase"] == "idle"
    assert daemon._state["active_sessions"]["B"]["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_card_advanced_past_persisted_stage_moves_forward() -> None:
    """Edge: board IN_REVIEW vs snapshot implementing → moved forward, not replayed."""
    daemon = _make_daemon()
    sessions = {"CARD": _session("CARD", phase="monitoring_performer", stage="implementing")}
    board = {"IN_REVIEW": ["CARD"]}
    _install(daemon, sessions, board, active_card_id="CARD")

    await daemon._reconcile_with_board(_snapshot(active_card_id="CARD", phase="monitoring_performer"))

    # IN_REVIEW infers monitoring_pr; a non-in-flight-consistent advance moves forward.
    assert daemon._state["active_sessions"]["CARD"]["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_top_level_phase_consistent_with_preserved_inflight_focus() -> None:
    """FR-010 follow-up: when the focus card is a preserved in-flight session,
    self._state['phase'] is re-derived to match it immediately (not left as the
    top-level block's board-inferred value), so the focus is consistent at
    reconcile time rather than one cycle late."""
    daemon = _make_daemon()
    sessions = {"PERF": _session("PERF", phase="monitoring_performer")}
    board = {"IN_PROGRESS": ["PERF"]}  # top-level block infers monitoring_agent
    _install(daemon, sessions, board, active_card_id="PERF")

    await daemon._reconcile_with_board(_snapshot(active_card_id="PERF", phase="monitoring_performer"))

    # Session preserved as monitoring_performer, and the global phase mirror
    # reflects it (not the stale monitoring_agent from the top-level block).
    assert daemon._state["active_sessions"]["PERF"]["phase"] == "monitoring_performer"
    assert daemon._state["phase"] == "monitoring_performer"


@pytest.mark.asyncio
async def test_focus_only_path_unchanged_when_no_active_sessions() -> None:
    """Guard: with no active_sessions the re-derivation is skipped, so the
    top-level block's phase result is preserved (regression guard for existing
    focus-only reconciliation behavior)."""
    daemon = _make_daemon()
    daemon._state["github_service"] = _github_with_board({"In Review": ["card-1"]})
    daemon._state["active_sessions"] = {}

    await daemon._reconcile_with_board(_snapshot(active_card_id="card-1", phase="monitoring_agent"))

    assert daemon._state["phase"] == "monitoring_pr"
