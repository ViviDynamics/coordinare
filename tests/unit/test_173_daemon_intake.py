"""Spec 173: the daemon seam for the two card-less intake runs.

These exercise `_maybe_dispatch_intake` and `_poll_intake_completion` directly.
The seam matters because a card-less run gets none of the graph node's guards:
no per-card mutex, no slot accounting, no monitor. Its only concurrency safety
is the in-flight marker, and its ordering rule is the one the existing
card-less paths had to learn twice.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.config import AdvocateConfig, CuratorConfig
from coordinare.daemon import CoordinareDaemon
from coordinare.models.env_cache import EnvCacheState


class _Service:
    def __init__(self, *, raise_on_dispatch: bool = False, session_id: str | None = "s1") -> None:
        self._raise = raise_on_dispatch
        self._session_id = session_id
        self.dispatched: list[dict] = []
        self.statuses: list[dict] = []

    async def dispatch_card(self, card_context: dict, workspace_info: Any = None) -> dict:
        self.dispatched.append(card_context)
        if self._raise:
            raise RuntimeError("no capacity")
        return {"session_id": self._session_id} if self._session_id else {}

    async def check_status(self, session_id: str) -> dict:
        return self.statuses.pop(0) if self.statuses else {"status": "working"}


def _daemon(state: dict) -> CoordinareDaemon:
    daemon = CoordinareDaemon.__new__(CoordinareDaemon)
    daemon._state = state  # type: ignore[attr-defined]
    daemon._intake_poll_tasks = set()  # type: ignore[attr-defined]
    return daemon


def _state(svc: _Service, *, advocate: AdvocateConfig | None = None,
           curator: CuratorConfig | None = None, ec: EnvCacheState | None = None) -> dict:
    cfg = SimpleNamespace(
        advocate=advocate or AdvocateConfig(enabled=True, github_repo="r"),
        curator=curator or CuratorConfig(),
        github_org="o",
        personas=SimpleNamespace(),
        performers=SimpleNamespace(resolved_role=lambda _n: None),
        resolve_performer_dispatch_model=lambda _r: {},
    )
    cache = ec or EnvCacheState(symphony_name="s", sanitised_name="s", cache_dir="/tmp/s")
    return {
        "config": cfg,
        "env_cache": {"s": cache},
        "performer_services": {"assessing": svc},
        "symphony_workspace_managers": {},
    }


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")


@pytest.mark.asyncio
async def test_an_enabled_role_is_dispatched_with_a_cardless_context() -> None:
    svc = _Service()
    state = _state(svc)
    await _daemon(state)._maybe_dispatch_intake("advocate", "s", SimpleNamespace(project_id=""))
    assert len(svc.dispatched) == 1
    ctx = svc.dispatched[0]
    assert ctx["role"] == "advocate" and ctx["workflow"] == "advocate"
    assert ctx["id"] == "advocate-s" and "card_id" not in ctx
    assert state["env_cache"]["s"].advocate_in_flight is True


@pytest.mark.asyncio
async def test_a_disabled_role_is_not_dispatched() -> None:
    svc = _Service()
    state = _state(svc, advocate=AdvocateConfig(enabled=False))
    await _daemon(state)._maybe_dispatch_intake("advocate", "s", SimpleNamespace())
    assert svc.dispatched == []


@pytest.mark.asyncio
async def test_a_role_already_in_flight_is_not_dispatched_again() -> None:
    svc = _Service()
    ec = EnvCacheState(symphony_name="s", sanitised_name="s", cache_dir="/tmp/s")
    ec.advocate_in_flight = True
    await _daemon(_state(svc, ec=ec))._maybe_dispatch_intake("advocate", "s", SimpleNamespace())
    assert svc.dispatched == []


@pytest.mark.asyncio
async def test_a_role_inside_its_cooldown_is_not_dispatched() -> None:
    svc = _Service()
    ec = EnvCacheState(symphony_name="s", sanitised_name="s", cache_dir="/tmp/s")
    ec.last_advocate_run_at = datetime.now(UTC) - timedelta(seconds=10)
    await _daemon(_state(svc, ec=ec))._maybe_dispatch_intake("advocate", "s", SimpleNamespace())
    assert svc.dispatched == []


@pytest.mark.asyncio
async def test_a_synchronous_dispatch_failure_rolls_the_marker_back() -> None:
    """The bug the existing card-less paths hit twice: a marker set and never
    cleared wedges the role until the process restarts."""
    svc = _Service(raise_on_dispatch=True)
    state = _state(svc)
    await _daemon(state)._maybe_dispatch_intake("advocate", "s", SimpleNamespace())
    ec = state["env_cache"]["s"]
    assert ec.advocate_in_flight is False, "a failed dispatch must not leave the role blocked"
    assert ec.advocate_attempts == 1 and "no capacity" in (ec.last_advocate_error or "")


@pytest.mark.asyncio
async def test_a_dispatch_returning_no_session_rolls_the_marker_back() -> None:
    svc = _Service(session_id=None)
    state = _state(svc)
    await _daemon(state)._maybe_dispatch_intake("advocate", "s", SimpleNamespace())
    assert state["env_cache"]["s"].advocate_in_flight is False


@pytest.mark.asyncio
async def test_no_performer_service_means_no_dispatch_and_no_marker() -> None:
    state = _state(_Service())
    state["performer_services"] = {}
    await _daemon(state)._maybe_dispatch_intake("advocate", "s", SimpleNamespace())
    assert state["env_cache"]["s"].advocate_in_flight is False


@pytest.mark.asyncio
async def test_a_missing_repo_is_not_dispatched() -> None:
    svc = _Service()
    state = _state(svc, advocate=AdvocateConfig(enabled=False))
    state["config"].advocate = SimpleNamespace(
        enabled=True, github_repo="", scan_interval_seconds=900,
    )
    await _daemon(state)._maybe_dispatch_intake("advocate", "s", SimpleNamespace())
    assert svc.dispatched == []


@pytest.mark.asyncio
async def test_the_curator_carries_the_board_id_and_the_advocate_does_not() -> None:
    svc = _Service()
    state = _state(svc, curator=CuratorConfig(enabled=True, github_repo="r"))
    github = SimpleNamespace(project_id="PVT_9")
    daemon = _daemon(state)
    await daemon._maybe_dispatch_intake("curator", "s", github)
    await daemon._maybe_dispatch_intake("advocate", "s", github)
    curator_ctx = next(c for c in svc.dispatched if c["role"] == "curator")
    advocate_ctx = next(c for c in svc.dispatched if c["role"] == "advocate")
    assert curator_ctx["project_id"] == "PVT_9"
    assert "project_id" not in advocate_ctx


@pytest.mark.asyncio
async def test_a_completed_poll_records_the_outcome_and_clears_the_marker() -> None:
    svc = _Service()
    svc.statuses = [{"status": "advocate_complete",
                     "report": {"advocate": {"issues_seen": 4}}}]
    state = _state(svc)
    ec = state["env_cache"]["s"]
    ec.advocate_in_flight = True

    import coordinare.daemon as daemon_module

    async def _no_sleep(_seconds: float) -> None:
        return None

    original = daemon_module.asyncio.sleep
    daemon_module.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await _daemon(state)._poll_intake_completion("advocate", "s", svc, "s1")
    finally:
        daemon_module.asyncio.sleep = original  # type: ignore[assignment]

    assert ec.advocate_in_flight is False
    assert ec.last_advocate_succeeded is True and ec.last_advocate_issues_seen == 4
