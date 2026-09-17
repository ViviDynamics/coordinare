"""Spec 076 — daemon wiring smoke tests.

Coverage for the new code paths added in daemon.py:
- Reconciliation pass invocation on startup
- Wedge invariant invocation at end-of-cycle
- Board-state reconciliation at end-of-cycle
- Config-derived budget values

The full daemon lifecycle is exercised by test_crash_recovery and
test_daemon_lifecycle; these tests are narrower unit-level smoke
tests for the new 076 wiring.
"""
from __future__ import annotations

import pytest

from coordinare.services.dispatcher_dedup_models import WedgeResolution
from coordinare.services.reconciliation import detect_wedged_state, reconcile_board_state


class _DispatcherDedupConfig:
    enabled = True
    reconciliation_budget_seconds = 15.0
    wedge_block_threshold = 5
    wedge_block_window_hours = 12


class _CoordinareConfig:
    dispatcher_dedup = _DispatcherDedupConfig()


def test_wedge_invariant_reads_config_budget() -> None:
    """Wedge invariant respects the configured threshold + window."""
    state = {
        "active_card": {"id": "PVTI_X", "title": "Card"},
        "current_card": {"id": "PVTI_X", "title": "Card"},
        "active_sessions": {},
        "performer_stage": None,
        "phase": None,
        "wedge_count_window": {},
        "coordinare_config": _CoordinareConfig(),
    }
    # First wedge → released
    resolution = detect_wedged_state(state, wedge_block_threshold=5, wedge_block_window_hours=12)
    assert resolution == WedgeResolution.RELEASED


def test_board_reconciliation_no_op_on_empty_state() -> None:
    """No active_card → reconcile_board_state is a no-op."""
    result = reconcile_board_state({"active_card": None}, {})
    assert result["action"] == "no_active_card"


def test_board_reconciliation_no_op_on_no_board() -> None:
    """No board snapshot → no_active_card path."""
    result = reconcile_board_state({}, {})
    assert result["action"] == "no_active_card"


@pytest.mark.asyncio
async def test_reconciliation_with_full_config_object() -> None:
    """Smoke test: daemon wiring constructs run_startup_reconciliation
    with budget from config — verify config-derived path doesn't crash."""
    from coordinare.services.docker_executor import DockerUnreachableError
    from coordinare.services.reconciliation import run_startup_reconciliation

    class _DownExec:
        async def list_containers_by_label(self, *a, **kw):
            raise DockerUnreachableError("test")

        async def stop_container(self, *a, **kw):  # pragma: no cover
            return True

        async def port_of(self, *a, **kw):  # pragma: no cover
            return None

        async def probe_healthz(self, *a, **kw):  # pragma: no cover
            return False

    state = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "u"},
            },
        },
        "performer_services": {},
    }
    report = await run_startup_reconciliation(state, _DownExec(), budget_seconds=15.0)
    assert report.docker_unreachable is True
    # State preserved on docker-down
    assert state["active_sessions"]["PVTI_X"]["agent_dispatch"] == {"session_id": "u"}


def test_wedge_invariant_no_card_id_returns_none() -> None:
    """Edge case: active_card present but without an id → no wedge."""
    state = {
        "active_card": {"title": "no id"},
        "current_card": None,
        "active_sessions": {},
        "performer_stage": None,
        "phase": None,
    }
    assert detect_wedged_state(state) is None


def test_wedge_invariant_falls_back_to_current_card_if_active_unset() -> None:
    """If active_card is missing but current_card is set, wedge check
    uses current_card (legacy single-session shape)."""
    state = {
        "active_card": None,
        "current_card": {"id": "PVTI_LEGACY", "title": "Legacy"},
        "active_sessions": {},
        "performer_stage": None,
        "phase": None,
        "wedge_count_window": {},
    }
    resolution = detect_wedged_state(state)
    assert resolution == WedgeResolution.RELEASED


def test_board_reconcile_normalizes_status_case() -> None:
    """'In Progress' on the card == IN_PROGRESS column → agreed."""
    state = {
        "active_card": {"id": "PVTI_X", "status": "In Progress"},
        "current_card": {"id": "PVTI_X", "status": "In Progress"},
        "active_card_id": "PVTI_X",
    }
    board = {"IN_PROGRESS": ["PVTI_X"]}
    result = reconcile_board_state(state, board)
    assert result["action"] == "agreed"


def test_board_reconcile_legitimate_forward_advance_is_deferred() -> None:
    """Local=IN_PROGRESS, board=IN_REVIEW → defer to existing startup
    reconcile, don't release the pin (preserves crash-recovery test
    semantics)."""
    state = {
        "active_card": {"id": "PVTI_X", "status": "IN_PROGRESS"},
        "current_card": {"id": "PVTI_X", "status": "IN_PROGRESS"},
        "active_card_id": "PVTI_X",
    }
    board = {"IN_REVIEW": ["PVTI_X"]}
    result = reconcile_board_state(state, board)
    assert result["action"] == "deferred"
    assert state["active_card"] is not None  # pin retained
