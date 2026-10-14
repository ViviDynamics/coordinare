"""Spec 218 (review round 2): the dispatch gates refuse a cache whose activate.sh is not the rendered one.

The sibling suite stubs the verify with an autouse fixture, which hides the False branch. These tests
use the REAL verify. A tampered cache activate.sh must hold dispatch, and the 093 readiness gate's
False branch must refuse it, without running docker.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.nodes.dispatch_performer import _env_readiness_gate, dispatch_performer
from tests.unit.graph.nodes.test_dispatch_performer import (
    _base_state,
    _make_http_service,
    _ready_env_cache,
)

SYMPHONY = "my-project"
STAGE = "implementing"


def _tampered_state(tmp_path: Path, svc: Any) -> dict[str, Any]:
    env_cache = _ready_env_cache(tmp_path, SYMPHONY)
    cache_dir = env_cache[SYMPHONY].cache_dir
    # The bootstrap performer rewrote activate.sh after the daemon rendered it.
    (cache_dir / "activate.sh").write_text("ruby() { printf 3.4.2; }\n")
    return _base_state(
        performer_services={STAGE: svc},
        performer_stage=STAGE,
        lifecycle_sequence=[STAGE],
        current_symphony=SYMPHONY,
        env_cache=env_cache,
    )


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Any docker call is a failure: a tampered activate.sh must be refused before docker runs."""
    spawn = AsyncMock(side_effect=AssertionError("docker must not run for a tampered activate.sh"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return spawn


@pytest.mark.asyncio
async def test_a_tampered_activate_holds_the_dispatch(
    tmp_path: Path, no_docker: AsyncMock,
) -> None:
    svc = _make_http_service(mode="ephemeral")
    state = _tampered_state(tmp_path, svc)

    await dispatch_performer(state)

    assert not svc.dispatch_card.called
    no_docker.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_gate_false_branch_refuses_a_tampered_activate(
    tmp_path: Path, no_docker: AsyncMock,
) -> None:
    """The 093 readiness gate's own False branch, driven with the real verify: it withholds the
    dispatch, releases the slot and asks for a re-bootstrap, and does not run docker."""
    svc = _make_http_service(mode="ephemeral")
    state = _tampered_state(tmp_path, svc)
    env_cache_service = MagicMock()
    state["env_cache_service"] = env_cache_service
    release = MagicMock()
    ec_state = state["env_cache"][SYMPHONY]

    result = await _env_readiness_gate(
        state, svc, "card-218", STAGE, SYMPHONY, ec_state, False, release,
    )

    assert result is state
    assert not svc.dispatch_card.called
    release.assert_called_once()
    env_cache_service.mark_runtime_health_failed.assert_called_once_with(SYMPHONY, state)
    no_docker.assert_not_awaited()

