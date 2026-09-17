"""Unit tests for the spec-093 dispatch-time toolchain-readiness gate.

These exercise the gate decision in ``dispatch_performer`` that runs AFTER the
existing "cache current + verified" guard passes: a real, re-run-each-dispatch
``verify.sh`` check via the ``verify_env_cache_clean`` tri-state seam.

Contract (specs/093-env-cache-readiness-gate/contracts/readiness-gate.md):
  * ``True``  (exit 0)  ⇒ proceed with dispatch.
  * ``False`` (nonzero) ⇒ trigger re-bootstrap, withhold dispatch, release slot,
    log ``dispatch_performer.env_cache_not_current``.
  * ``None``  (verify.sh absent / docker error) ⇒ MUST NOT block — degraded
    passthrough on legacy ``last_bootstrap_succeeded``.

The gate is re-evaluated on EVERY code-running dispatch (no caching, FR-006) and
``env_bootstrap`` is exempt (it is what builds the cache).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.lifecycle import ROLE_TO_STAGE

# Reuse the established harness helpers from the sibling suite.
from tests.unit.graph.nodes.test_dispatch_performer import (
    _base_state,
    _make_http_service,
    _ready_env_cache,
)

# Every code-running stage the gate applies to (all ROLE_TO_STAGE values;
# env_bootstrap is NOT among them and is exempt).
CODE_RUNNING_STAGES = sorted(set(ROLE_TO_STAGE.values()))


def _readiness_state(
    tmp_path: Path,
    *,
    stage: str,
    svc: Any,
    symphony_name: str = "my-project",
) -> dict[str, Any]:
    """Build a dispatch state whose env-cache passes the current+verified guard
    so the NEW readiness gate is the deciding factor."""
    env_cache = _ready_env_cache(tmp_path, symphony_name)
    return _base_state(
        performer_services={stage: svc},
        performer_stage=stage,
        lifecycle_sequence=[stage],
        current_symphony=symphony_name,
        env_cache=env_cache,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", CODE_RUNNING_STAGES)
async def test_readiness_fail_withholds_dispatch(
    tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T005: readiness ``False`` ⇒ dispatch withheld + slot released, for every
    code-running stage."""
    svc = _make_http_service(mode="ephemeral")
    state = _readiness_state(tmp_path, stage=stage, svc=svc)

    fake_verify = AsyncMock(return_value=(False, "FAIL: ruby 3.3.0 not resolvable"))
    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.verify_env_cache_clean",
        fake_verify,
    )

    result = await dispatch_performer(state)

    # Readiness was actually consulted...
    assert fake_verify.await_count == 1
    # ...and the dispatch was held (performer never invoked).
    assert not svc.dispatch_card.called
    # The node returns state (deferred), not a monitoring phase.
    assert result is state or result.get("phase") != "monitoring_performer"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", CODE_RUNNING_STAGES)
async def test_readiness_pass_proceeds_with_dispatch(
    tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T006: readiness ``True`` ⇒ dispatch proceeds, for every code-running stage."""
    svc = _make_http_service(mode="ephemeral")
    state = _readiness_state(tmp_path, stage=stage, svc=svc)

    fake_verify = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.verify_env_cache_clean",
        fake_verify,
    )

    await dispatch_performer(state)

    assert fake_verify.await_count == 1
    assert svc.dispatch_card.called


@pytest.mark.asyncio
async def test_readiness_none_is_degraded_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T007: readiness ``None`` (verify.sh absent / docker error) MUST NOT block —
    proceed on the legacy last_bootstrap_succeeded path."""
    svc = _make_http_service(mode="ephemeral")
    state = _readiness_state(tmp_path, stage="qa", svc=svc)

    fake_verify = AsyncMock(return_value=(None, "no verify.sh in cache"))
    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.verify_env_cache_clean",
        fake_verify,
    )

    await dispatch_performer(state)

    assert fake_verify.await_count == 1
    # Degraded ⇒ dispatch proceeds.
    assert svc.dispatch_card.called


@pytest.mark.asyncio
async def test_readiness_gate_exempts_env_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env_bootstrap is the run that BUILDS the cache — it must never be gated on
    readiness (the check would always fail before the cache exists)."""
    svc = _make_http_service(mode="ephemeral")
    symphony_name = "my-project"
    env_cache = _ready_env_cache(tmp_path, symphony_name)
    state = _base_state(
        performer_services={"env_bootstrap": svc},
        performer_stage="env_bootstrap",
        lifecycle_sequence=["env_bootstrap"],
        current_symphony=symphony_name,
        env_cache=env_cache,
    )

    fake_verify = AsyncMock(return_value=(False, "FAIL"))
    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.verify_env_cache_clean",
        fake_verify,
    )

    await dispatch_performer(state)

    # Readiness was NOT consulted for the bootstrap dispatch...
    assert fake_verify.await_count == 0
    # ...and the bootstrap dispatch proceeded.
    assert svc.dispatch_card.called


@pytest.mark.asyncio
async def test_readiness_recomputed_each_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-006: no readiness verdict is cached across dispatches — two dispatches
    against the same cache run the check twice."""
    svc = _make_http_service(mode="ephemeral")
    # No lingering live session between dispatches — model a fresh re-pickup so
    # the in-flight guard does not short-circuit the second dispatch before it
    # reaches the readiness gate (the point under test is that readiness is
    # re-evaluated, not cached).
    svc.has_live_session = MagicMock(return_value=False)
    state = _readiness_state(tmp_path, stage="qa", svc=svc)

    fake_verify = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.verify_env_cache_clean",
        fake_verify,
    )

    await dispatch_performer(state)
    await dispatch_performer(state)

    assert fake_verify.await_count == 2


# ---------------------------------------------------------------------------
# User Story 3 — readiness failure self-heals via bootstrap, bounded by the
# EXISTING env_bootstrap_max_attempts budget (T017-T019).
#
# The dispatch node does not own the re-bootstrap or the budget: on a False
# readiness it flags the cache for forced regen through the existing
# EnvCacheService.mark_runtime_health_failed seam (the same one
# monitor_performer uses for a services-health failure), then holds + releases
# the slot. The next check_and_trigger cycle observes the flag and dispatches a
# re-bootstrap, bounded by the existing bootstrap_attempts / bootstrap_exhausted
# machinery — no readiness-specific counter is introduced.
# ---------------------------------------------------------------------------


def _readiness_state_with_ec_service(
    tmp_path: Path,
    *,
    stage: str = "qa",
    svc: Any = None,
    symphony_name: str = "my-project",
) -> tuple[dict[str, Any], Any]:
    """Build a readiness-gate state that also carries a mock EnvCacheService so
    the re-bootstrap seam can be asserted. Returns (state, env_cache_service)."""
    if svc is None:
        svc = _make_http_service(mode="ephemeral")
    state = _readiness_state(
        tmp_path, stage=stage, svc=svc, symphony_name=symphony_name,
    )
    env_cache_service = MagicMock()
    env_cache_service.mark_runtime_health_failed = MagicMock(return_value=None)
    state["env_cache_service"] = env_cache_service
    return state, env_cache_service


@pytest.mark.asyncio
async def test_readiness_fail_triggers_rebootstrap_and_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T017: a False readiness flags the cache for re-bootstrap (the existing
    forced-regen seam) for the current symphony AND holds the dispatch under the
    existing env_cache_not_current path (slot released, performer not invoked)."""
    svc = _make_http_service(mode="ephemeral")
    state, env_cache_service = _readiness_state_with_ec_service(
        tmp_path, stage="qa", svc=svc,
    )

    fake_verify = AsyncMock(return_value=(False, "FAIL: ruby 3.3.0 not resolvable"))
    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.verify_env_cache_clean",
        fake_verify,
    )

    await dispatch_performer(state)

    # Re-bootstrap was triggered through the existing seam, for THIS symphony...
    assert env_cache_service.mark_runtime_health_failed.call_count == 1
    called_args = env_cache_service.mark_runtime_health_failed.call_args
    assert called_args.args[0] == "my-project" or called_args.kwargs.get(
        "symphony_name",
    ) == "my-project"
    # ...and the dispatch was held (performer never invoked).
    assert not svc.dispatch_card.called


@pytest.mark.asyncio
async def test_readiness_fail_loop_is_bounded_by_existing_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T018: the dispatch→bootstrap→dispatch loop is bounded by the EXISTING
    budget — the node introduces NO readiness-specific counter. It only routes
    through mark_runtime_health_failed, leaving the bootstrap_attempts /
    bootstrap_exhausted accounting to env_cache (covered in test_060_env_cache).
    Persistent False simply re-flags each time; the budget lives elsewhere."""
    from coordinare.models.env_cache import EnvCacheState

    svc = _make_http_service(mode="ephemeral")
    svc.has_live_session = MagicMock(return_value=False)
    state, env_cache_service = _readiness_state_with_ec_service(
        tmp_path, stage="qa", svc=svc,
    )

    fake_verify = AsyncMock(return_value=(False, "FAIL: ruby not resolvable"))
    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.verify_env_cache_clean",
        fake_verify,
    )

    # Three persistent-False dispatches.
    await dispatch_performer(state)
    await dispatch_performer(state)
    await dispatch_performer(state)

    # Each held dispatch routes through the SAME existing seam — no direct
    # re-bootstrap dispatch, no new counter.
    assert env_cache_service.mark_runtime_health_failed.call_count == 3
    assert not svc.dispatch_card.called
    # No readiness-specific counter was bolted onto the cache state.
    cache_state = state["env_cache"]["my-project"]
    assert isinstance(cache_state, EnvCacheState)
    state_fields = set(type(cache_state).model_fields)
    assert not any(
        "readiness" in f and "count" in f.lower() for f in state_fields
    ), "no readiness-specific counter must be introduced on EnvCacheState"


@pytest.mark.asyncio
async def test_readiness_converges_after_rebootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T019: once a re-bootstrap makes readiness pass, the next dispatch
    proceeds normally (the gate self-heals)."""
    svc = _make_http_service(mode="ephemeral")
    svc.has_live_session = MagicMock(return_value=False)
    state, env_cache_service = _readiness_state_with_ec_service(
        tmp_path, stage="qa", svc=svc,
    )

    # First dispatch fails readiness (triggers re-bootstrap + hold); the second,
    # after the cache converges, passes and proceeds.
    fake_verify = AsyncMock(side_effect=[(False, "FAIL: ruby not resolvable"), (True, "")])
    monkeypatch.setattr(
        "coordinare.graph.nodes.dispatch_performer.verify_env_cache_clean",
        fake_verify,
    )

    await dispatch_performer(state)
    assert env_cache_service.mark_runtime_health_failed.call_count == 1
    assert not svc.dispatch_card.called

    await dispatch_performer(state)
    # Converged: dispatch proceeds, no further re-bootstrap flag.
    assert svc.dispatch_card.called
    assert env_cache_service.mark_runtime_health_failed.call_count == 1
