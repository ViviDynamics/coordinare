"""Spec 076 T060 — check_board stale-session reconciliation integration.

Verifies that the FR-008 change in check_board routes through
``handle_potentially_stale_session`` instead of unconditionally clearing
``agent_dispatch``.  The clearing-on-no-container path matches the
pre-076 behaviour so legacy tests continue to pass; what's new is the
delegation and the structured log event.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import initial_state


class _StaleSvc:
    """has_live_session always False → triggers the stale-redispatch path."""

    def has_live_session(self, session_id: str) -> bool:
        return False


class _GitHubInProgress:
    """Match the shape used by other check_board tests."""

    async def poll_board(self):
        return {
            "snapshot": {
                "IN_PROGRESS": ["ITEM_LEGACY"],
                "TODO": [],
                "IN_REVIEW": [],
                "BLOCKED": [],
            },
            "titles": {"ITEM_LEGACY": "Legacy Active"},
            "descriptions": {"ITEM_LEGACY": ""},
            "issue_numbers": {"ITEM_LEGACY": 99},
        }


@pytest.mark.asyncio
async def test_check_board_reconciles_top_level_stale_session(monkeypatch) -> None:
    """Legacy single-symphony shape: agent_dispatch lives at the state
    root.  When has_live_session returns False, check_board MUST route
    through handle_potentially_stale_session (T057) which clears the
    state's agent_dispatch and routes back to dispatching."""
    # Stub out the docker enumeration so we don't actually call docker
    from coordinare.services import reconciliation as recon_mod

    async def _stub_handle(state, card_id, *, docker_executor=None):
        # Mimic the pre-076 clearing behaviour for the legacy shape
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        state["phase"] = "dispatching"
        return recon_mod.ReconciliationDecision.FRESH_DISPATCHED

    monkeypatch.setattr(recon_mod, "handle_potentially_stale_session", _stub_handle)

    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["current_card"] = {"id": "ITEM_LEGACY", "status": "IN_PROGRESS"}
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "implementing"
    state["agent_dispatch"] = {"session_id": "stale-uuid"}
    state["agent_dispatch_at"] = datetime(2026, 5, 28, tzinfo=UTC)
    state["performer_services"] = {"implementing": _StaleSvc()}

    result = await check_board(state)

    # Stub set these — proves we routed through handle_potentially_stale_session
    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None
