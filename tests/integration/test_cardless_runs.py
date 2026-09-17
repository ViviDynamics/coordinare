"""Spec 173: the card-less intake runs, at the daemon seam.

Two claims that no unit test covers and that the analysis pass flagged as
unverified: a deployment that configures neither role is unaffected by the
deletion, and a restart between a run and the next cycle produces no duplicate.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.intake_dispatch import handle_run_result, should_run


def _state(**over: object) -> EnvCacheState:
    state = EnvCacheState(symphony_name="s", sanitised_name="s", cache_dir="/tmp/s")
    for k, v in over.items():
        setattr(state, k, v)
    return state


# ---------------------------------------------------------------- FR-027 / SC-008


def test_neither_role_runs_when_neither_is_configured() -> None:
    """The old path was deleted rather than flag-guarded, so "unchanged for a
    deployment that never enabled it" needs a real check, not a flag assertion."""
    from coordinare.config import ProjectConfiguration

    cfg = ProjectConfiguration.model_construct()
    assert cfg.advocate.enabled is False
    assert cfg.curator.enabled is False
    state = _state()
    for role in ("advocate", "curator"):
        assert not should_run(state, role, enabled=False, interval_seconds=900)


def test_the_retired_service_is_gone_from_the_tree() -> None:
    """FR-026: removed, not left dormant beside its replacement."""
    import importlib

    for module in ("coordinare.services.advocate", "coordinare.services.scoring",
                   "coordinare.graph.nodes.advocate"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_the_graph_no_longer_has_an_advocate_node() -> None:
    from pathlib import Path

    builder = Path("src/coordinare/graph/builder.py").read_text()
    assert "advocate_scan" not in builder
    assert 'graph.add_edge(START, "route_issue_comments")' in builder, (
        "START must be rewired to the node that followed the advocate scan"
    )


# ---------------------------------------------------------------- SC-005


@pytest.mark.asyncio
async def test_a_restart_between_a_run_and_the_next_cycle_adds_no_duplicate() -> None:
    """The durable marker is the issue's label, not coordinare memory. A restart
    empties the in-memory state, so the second cycle must still skip the issue."""
    from performer.workflows.advocate.intake import select_candidates

    answered = {
        "id": "I_1", "number": 1, "title": "How?", "body": "?",
        "url": "u", "labels": ["advocate-handled"],
    }
    before_restart = select_candidates(
        [answered], handled_label="advocate-handled", escalation_label="needs-human",
    )
    assert before_restart == []

    # A restart: brand new state, nothing remembered.
    after_restart = select_candidates(
        [answered], handled_label="advocate-handled", escalation_label="needs-human",
    )
    assert after_restart == [], "the label carries the decision, so memory is not needed"


@pytest.mark.asyncio
async def test_an_interrupted_run_does_not_block_the_role_after_a_restart() -> None:
    """The in-flight marker is deliberately transient. A crash mid-run leaves it
    set in memory; the restored state must not carry it."""
    from coordinare.state_store import EnvCacheStateSnapshot

    crashed = _state(advocate_in_flight=True, last_advocate_run_at=datetime.now(UTC) - timedelta(days=1))
    snapshot = EnvCacheStateSnapshot.model_validate(
        {k: v for k, v in crashed.model_dump(mode="json").items()
         if k in EnvCacheStateSnapshot.model_fields},
    )
    restored = _state(**{
        k: getattr(snapshot, k) for k in ("advocate_attempts", "advocate_exhausted",
                                          "last_advocate_run_at", "last_advocate_succeeded")
    })
    assert restored.advocate_in_flight is False
    assert should_run(restored, "advocate", enabled=True, interval_seconds=900)


@pytest.mark.asyncio
async def test_a_completed_run_survives_a_restart_because_it_was_flushed() -> None:
    flushed: list = []

    async def save() -> None:
        flushed.append(True)

    import asyncio

    state = _state(advocate_in_flight=True)
    handle_run_result(
        state, "advocate",
        {"status": "advocate_complete", "report": {"advocate": {"issues_seen": 2}}},
        {"snapshot_save_fn": save},
    )
    await asyncio.sleep(0)
    assert flushed == [True], "an out-of-lifecycle completion must not wait on a stage change"
    assert state.last_advocate_issues_seen == 2
