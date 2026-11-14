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
from tests.unit.env_manifest_fixture import (
    record_verified_cache_fixture,
    reviewed_cache,
    verification_output,
)
from tests.unit.env_mount_stub import mount_config


@pytest.fixture(autouse=True)
def _accepted_verification_sources(monkeypatch, tmp_path):
    """These tests cover verifier outcomes and persistence, with pinned inputs supplied."""
    from tests.unit.env_manifest_fixture import patch_clean_source_transport
    patch_clean_source_transport(monkeypatch, tmp_path)
    monkeypatch.setattr("coordinare.services.environment_verification.verification_sources",
        AsyncMock(return_value={"tools.plan": "baseline"}))
    monkeypatch.setattr("coordinare.services.environment_verification.source_setup", lambda files, **kwargs: "")
    monkeypatch.setattr("coordinare.services.environment_capabilities._docker_identity",
        AsyncMock(return_value=("trusted/global@sha256:" + "a" * 64, "linux/x86_64")))


def _daemon() -> CoordinareDaemon:
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state["snapshot_checkpoint_fn"] = AsyncMock()
    daemon._state["coordinare_config"] = mount_config(performer_image="trusted/global@sha256:" + "a" * 64)
    return daemon


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


def _ready_state(cache_dir, verify_text):
    """218: a state whose daemon-rendered verify.sh and activate.sh are on record, and a cache
    whose activate.sh is the rendered text."""
    from coordinare.models.env_cache import EnvCacheState
    activate = "# daemon-rendered activate.sh\n"
    (cache_dir / "activate.sh").write_text(activate)
    return reviewed_cache(EnvCacheState(
        symphony_name="sym", sanitised_name="sym", cache_dir=cache_dir,
        rendered_verify_sh=verify_text, rendered_activate_sh=activate,
    ))


def _docker_answers(run_rc: int, manifest):
    """218: the daemon probe (docker version) answers 0; the verify run answers ``run_rc``."""
    async def _fake(*argv, **_kwargs):
        return _FakeProc(0 if "version" in argv or "rm" in argv else run_rc, manifest)

    return _fake


class _FakeProc:
    def __init__(self, rc: int, manifest) -> None:
        self.returncode = rc
        self.manifest = manifest

    async def wait(self):
        return self.returncode

    async def communicate(self, input: bytes | None = None):
        return (verification_output(self.manifest), None)


@pytest.mark.asyncio
async def test_clean_verify_refuses_without_daemon_rendered_script(tmp_path) -> None:
    """218: no daemon-rendered verify.sh on record → False. A missing record never passes."""
    daemon = _daemon()
    cache = tmp_path / "sym"
    cache.mkdir()
    daemon._state["env_cache"] = {"sym": _ec_state(cache)}
    svc = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img@sha256:" + "a" * 64))
    _res, _detail = await daemon._verify_env_cache_clean("sym", svc)
    assert _res is False


@pytest.mark.asyncio
async def test_clean_verify_passes_on_zero_exit(tmp_path, monkeypatch) -> None:
    daemon = _daemon()
    cache = tmp_path / "sym"
    cache.mkdir()
    daemon._state["env_cache"] = {"sym": _ready_state(cache, "#!/bin/sh\nexit 0\n")}
    svc = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img@sha256:" + "a" * 64))
    monkeypatch.setattr("asyncio.create_subprocess_exec", _docker_answers(0, daemon._state["env_cache"]["sym"].environment_manifest))
    _res, _detail = await daemon._verify_env_cache_clean("sym", svc)
    assert _res is True


@pytest.mark.asyncio
async def test_clean_verify_fails_on_nonzero_exit(tmp_path, monkeypatch) -> None:
    """A clean-context verify failure (e.g. chromium not usable from the cache
    alone) returns False so the caller downgrades the bootstrap → retry."""
    daemon = _daemon()
    cache = tmp_path / "sym"
    cache.mkdir()
    daemon._state["env_cache"] = {"sym": _ready_state(cache, "#!/bin/sh\nexit 1\n")}
    svc = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img@sha256:" + "a" * 64))
    monkeypatch.setattr("asyncio.create_subprocess_exec", _docker_answers(1, daemon._state["env_cache"]["sym"].environment_manifest))
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
    (cache / "toolchain" / "ruby").write_text("bin")
    daemon._state["env_cache"] = {"sym": reviewed_cache(_ec_state(cache))}

    svc = EnvCacheService(
        mount_config(env_bootstrap_max_attempts=3),
    )
    record_verified_cache_fixture(daemon._state["env_cache"]["sym"])
    svc.on_bootstrap_complete("sym", success=True, state=daemon._state)
    # Drain the flush task scheduled by the completion handler.
    pending = set(svc._notify_tasks)
    if pending:
        await asyncio.gather(*pending)

    snap = await store.load()
    assert snap is not None, "completion handler never flushed a snapshot to disk"
    assert snap.env_cache["sym"].last_bootstrap_succeeded is True
