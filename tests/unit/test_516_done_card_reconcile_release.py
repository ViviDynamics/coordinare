"""Issue #516 — the DONE-card wedge across named-symphony cycles.

reconcile_board_state retires a session whose card the board moved to
DONE, but for named symphonies the retirement must happen *inside* the
symphony cycle (before _update_symphony_state mirrors active_sessions
back onto sym_state) or the zombie reloads next cycle and still holds
the pickup slot.  The retirement must also best-effort release the
session's performer resources before the record is dropped.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.graph.state import SymphonyRuntimeState


def _make_daemon() -> CoordinareDaemon:
    graph = AsyncMock()
    graph.ainvoke = AsyncMock(return_value={})
    return CoordinareDaemon(graph, poll_interval_seconds=1, max_cycles=1)


def _zombie_state() -> tuple[SymphonyRuntimeState, dict[str, dict[str, str]]]:
    zombie = {
        "card_id": "PVTI_z",
        "phase": "monitor",
        "performer_stage": "implement",
    }
    sym_state = SymphonyRuntimeState(name="alpha")
    sym_state.active_sessions = {"PVTI_z": dict(zombie)}
    return sym_state, {"PVTI_z": zombie}


@pytest.mark.asyncio
async def test_done_retire_releases_resources_and_survives_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coordinare.daemon as daemon_module

    daemon = _make_daemon()
    sym_state, zombie_map = _zombie_state()
    daemon.state["symphony_states"] = {"alpha": sym_state}
    daemon.state["active_sessions"] = dict(zombie_map)
    daemon.state["active_card"] = {"id": "PVTI_z", "status": "IN_PROGRESS"}
    daemon.state["board_snapshot"] = {"DONE": ["PVTI_z"]}

    released: list[str] = []

    async def fake_release(state, sess):
        released.append(sess["card_id"])

    monkeypatch.setattr(daemon_module, "_release_session_resources", fake_release)

    await daemon._reconcile_board_state_with_release()
    assert released == ["PVTI_z"]
    assert "PVTI_z" not in daemon.state["active_sessions"]
    assert daemon.state["active_card"] is None

    # _update_symphony_state mirrors the CLEANED dict onto sym_state.
    daemon._update_symphony_state("alpha", sym_state)

    # Next cycle: _resolve_symphony_effective_state re-seeds the global
    # active_sessions from sym_state — the zombie must not come back.
    _cfg, sym_sessions = await daemon._resolve_symphony_effective_state(
        "alpha", None, sym_state,
    )
    assert "PVTI_z" not in sym_sessions
    assert "PVTI_z" not in daemon.state["active_sessions"]


@pytest.mark.asyncio
async def test_done_retire_without_session_skips_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coordinare.daemon as daemon_module

    daemon = _make_daemon()
    daemon.state["symphony_states"] = {"alpha": SymphonyRuntimeState(name="alpha")}
    daemon.state["active_sessions"] = {}
    daemon.state["active_card"] = {"id": "PVTI_z", "status": "IN_PROGRESS"}
    daemon.state["board_snapshot"] = {"DONE": ["PVTI_z"]}

    released: list[str] = []

    async def fake_release(state, sess):
        released.append(sess["card_id"])

    monkeypatch.setattr(daemon_module, "_release_session_resources", fake_release)

    await daemon._reconcile_board_state_with_release()
    assert released == []
    assert daemon.state["active_card"] is None


@pytest.mark.asyncio
async def test_reconcile_crash_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coordinare.services.reconciliation as reconciliation_module

    daemon = _make_daemon()
    daemon.state["symphony_states"] = {"alpha": SymphonyRuntimeState(name="alpha")}

    async def boom(state, board):
        raise RuntimeError("reconcile blew up")

    monkeypatch.setattr(reconciliation_module, "reconcile_board_state", boom)
    await daemon._reconcile_board_state_with_release()


@pytest.mark.asyncio
async def test_symphony_cycle_reconciles_before_mirroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _make_daemon()
    calls: list[str] = []

    async def fake_reconcile() -> None:
        calls.append("reconcile")

    monkeypatch.setattr(daemon, "_reconcile_board_state_with_release", fake_reconcile)
    real_update = daemon._update_symphony_state

    def record_update(symphony_name: str, sym_state: SymphonyRuntimeState) -> None:
        calls.append("update")
        real_update(symphony_name, sym_state)

    monkeypatch.setattr(daemon, "_update_symphony_state", record_update)

    sym_state = SymphonyRuntimeState(name="alpha")
    daemon.state["symphony_states"] = {"alpha": sym_state}
    await daemon._conduct_single_symphony("alpha", None)
    assert calls == ["reconcile", "update"]
