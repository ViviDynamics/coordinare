"""077 — snapshot persistence must follow lifecycle (performer_stage) progress,
not only daemon `phase` transitions.

Regression for the restart-rewind bug: the daemon's `phase` stays
`monitoring_performer` across an entire card lifecycle (assess→architect→…→qa),
so a save trigger keyed on `phase` alone fired exactly once (at startup,
capturing `assessing`) and every restart rewound the in-flight card back to
`assessing`. The fix keys persistence on a lifecycle signature that includes the
per-session `performer_stage`. These tests pin that signature's sensitivity.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon


def _daemon() -> CoordinareDaemon:
    return CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())


def test_signature_changes_when_stage_advances_with_phase_constant() -> None:
    """The exact bug scenario: phase pinned at monitoring_performer, the card's
    performer_stage advances → signature MUST change (so a save fires)."""
    daemon = _daemon()
    daemon._state["phase"] = "monitoring_performer"
    daemon._state["current_card"] = {"id": "PVTI_X", "title": "Card"}

    daemon._state["active_sessions"] = {"PVTI_X": {"performer_stage": "assessing"}}
    sig_assessing = daemon._lifecycle_signature()

    # phase is unchanged — only the lifecycle stage advances
    daemon._state["active_sessions"] = {"PVTI_X": {"performer_stage": "implementing"}}
    sig_implementing = daemon._lifecycle_signature()

    assert daemon._state["phase"] == "monitoring_performer"
    assert sig_assessing != sig_implementing


def test_signature_tracks_top_level_performer_stage() -> None:
    """Single-session mode keeps stage at top level; signature must track it."""
    daemon = _daemon()
    daemon._state["phase"] = "monitoring_performer"
    daemon._state["performer_stage"] = "reviewing"
    sig_reviewing = daemon._lifecycle_signature()

    daemon._state["performer_stage"] = "qa"
    assert sig_reviewing != daemon._lifecycle_signature()


def test_signature_stable_when_nothing_relevant_changes() -> None:
    """No needless saves: identical lifecycle state → identical signature."""
    daemon = _daemon()
    daemon._state["phase"] = "monitoring_performer"
    daemon._state["current_card"] = {"id": "PVTI_X", "title": "Card"}
    daemon._state["active_sessions"] = {"PVTI_X": {"performer_stage": "qa"}}

    first = daemon._lifecycle_signature()
    # An unrelated field churns; lifecycle signature must not move.
    daemon._state["last_poll_at"] = "2026-06-01T00:00:00Z"
    assert daemon._lifecycle_signature() == first


def test_signature_does_not_raise_on_malformed_state() -> None:
    """Must be cheap and defensive — never raise mid-cycle."""
    daemon = _daemon()
    daemon._state["active_sessions"] = {"PVTI_X": None, "PVTI_Y": {"performer_stage": None}}
    daemon._state["current_card"] = None
    # Should produce a tuple, not raise.
    assert isinstance(daemon._lifecycle_signature(), tuple)


def test_snapshot_top_level_stage_tracks_active_session() -> None:
    """077: in shared-pool mode the live stage lives in the active session and
    top-level current_card/performer_stage stay stale. The built snapshot's
    top-level performer_stage must reflect the active session so blocked/
    system_error resume (which read top-level) don't rewind to the stale value.
    """
    daemon = _daemon()
    # Reproduce the shared-pool shape: no top-level card pointer, stale top stage.
    daemon._state["current_card"] = None
    daemon._state["performer_stage"] = "assessing"  # stale top-level
    daemon._state["active_sessions"] = {
        "PVTI_X": {"performer_stage": "implementing", "picked_up_at": "2026-06-01T00:00:00Z"},
    }

    snap = daemon._build_snapshot()

    # top-level snapshot stage now reflects the active session, not the stale value
    assert snap.performer_stage == "implementing"
    assert snap.performer_stage != "assessing"


# ---------------------------------------------------------------------------
# 077: clean-context env-cache verify gate
# ---------------------------------------------------------------------------
def _ec_state(cache_dir):
    from coordinare.models.env_cache import EnvCacheState
    return EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=cache_dir)


class _FakeProc:
    def __init__(self, rc: int) -> None:
        self.returncode = rc

    async def communicate(self):
        return (b"verify output", None)


@pytest.mark.asyncio
async def test_clean_verify_returns_none_when_verify_script_absent(tmp_path) -> None:
    """No verify.sh in the cache → degraded (None), must NOT downgrade success."""
    daemon = _daemon()
    cache = tmp_path / "sym"
    cache.mkdir()
    daemon._state["env_cache"] = {"sym": _ec_state(cache)}
    svc = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img"))
    _res, _detail = await daemon._verify_env_cache_clean("sym", svc)
    assert _res is None


@pytest.mark.asyncio
async def test_clean_verify_passes_on_zero_exit(tmp_path, monkeypatch) -> None:
    daemon = _daemon()
    cache = tmp_path / "sym"
    cache.mkdir()
    (cache / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
    daemon._state["env_cache"] = {"sym": _ec_state(cache)}
    svc = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img"))
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeProc(0)))
    _res, _detail = await daemon._verify_env_cache_clean("sym", svc)
    assert _res is True


@pytest.mark.asyncio
async def test_clean_verify_fails_on_nonzero_exit(tmp_path, monkeypatch) -> None:
    """A clean-context verify failure (e.g. chromium not usable from the cache
    alone) returns False so the caller downgrades the bootstrap → retry."""
    daemon = _daemon()
    cache = tmp_path / "sym"
    cache.mkdir()
    (cache / "verify.sh").write_text("#!/bin/sh\nexit 1\n")
    daemon._state["env_cache"] = {"sym": _ec_state(cache)}
    svc = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img"))
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeProc(1)))
    _res, _detail = await daemon._verify_env_cache_clean("sym", svc)
    assert _res is False
    assert isinstance(_detail, str)


# ---------------------------------------------------------------------------
# 088 US5 (T027): bootstrap completion must flush the snapshot to disk
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bootstrap_completion_flushes_snapshot_to_disk(tmp_path) -> None:
    """088 US5: the daemon only saves when `_lifecycle_signature()` changes, and
    a bootstrap completing changes no phase/card/stage — so a success recorded
    in memory never reached disk and a restart loaded
    last_bootstrap_succeeded=False (observed: success 16:16:52, restart 17:55).
    `on_bootstrap_complete` must trigger an immediate state-store save."""
    import asyncio
    from unittest.mock import MagicMock

    from coordinare.services.env_cache import EnvCacheService
    from coordinare.state_store import StateStore

    store = StateStore(tmp_path / "state.json", MagicMock())
    daemon = CoordinareDaemon(
        AsyncMock(), max_cycles=1, sleep_func=AsyncMock(), state_store=store,
    )
    cache = tmp_path / "sym"
    cache.mkdir()
    # 092: on_bootstrap_complete downgrades success to failure when the mount
    # holds only coordinare-seeded files; seed toolchain content for the success.
    (cache / "toolchain").mkdir()
    daemon._state["env_cache"] = {"sym": _ec_state(cache)}

    svc = EnvCacheService(
        SimpleNamespace(global_config=SimpleNamespace(env_bootstrap_max_attempts=3)),
    )
    svc.on_bootstrap_complete("sym", success=True, state=daemon._state)
    # Drain the flush task scheduled by the completion handler.
    pending = set(svc._notify_tasks)
    if pending:
        await asyncio.gather(*pending)

    snap = await store.load()
    assert snap is not None, "completion handler never flushed a snapshot to disk"
    assert snap.env_cache["sym"].last_bootstrap_succeeded is True
