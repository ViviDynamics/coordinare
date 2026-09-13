"""#399: an ENV_BLOCKED card must be able to come back.

Three cards were stranded in BLOCKED overnight with ``blockers=[]`` and
``COORDINARE_BLOCKED_RECOVERY=1`` live in the daemon. Every recovery oracle said
"recovered". Recovery never fired, because it classifies a card as env-blocked
with ``if sess.get("env_blocked")`` and the local-test-gate branch of
``monitor_performer`` (the one every card took) never wrote that marker. Only
the CI-infrastructure branch did. Per recovery's own contract, "a block for an
undetected reason never auto-recovers" -- safe, and terminal.

The second defect, visible on the same cards: recovery resumes to IN_PROGRESS
with whatever ``performer_stage`` the session had. For website#160 that was
``implementing``, while its PR was already open and green. Re-running the
implementer on finished work is what recovery would have done had it fired.

  M4  drop the stage reconciliation (or never call _advance_stage)
      -> test_recovery_with_a_green_pr_advances_past_implementing
  M5  advance on any decision, not only FORWARD
      -> test_recovery_with_a_red_pr_keeps_implementing

MUTATIONS THAT MUST FAIL A TEST HERE:
  M1  drop the marker stamp in the local-test-gate branch
      -> test_local_test_gate_env_block_stamps_a_recoverable_marker
         test_the_stamped_marker_recovers_once_the_cache_is_healthy
  M2  stamp ``check_names`` non-empty (recovery reads "CI names present" as a
      CI hold and refuses env recovery)
      -> test_the_stamped_marker_recovers_once_the_cache_is_healthy
  M3  drop the marker clear after a successful recovery
      -> test_recovery_clears_the_marker_it_consumed
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from coordinare.graph.nodes.check_board import _attempt_blocked_card_recovery
from coordinare.graph.nodes.monitor_performer import monitor_performer
from tests.unit.graph.nodes.test_check_board_recovery import (
    _NO_CONTENT_BOARD,
    _ctx,
    _env_cache,
    _FakeGitHub,
    _Notify,
    _state,
)
from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

_REASON = (
    "env-cache services-start failed: /devenv/website-3ab3e0/services/services-start.sh "
    "(returncode=75): ERROR: env: postgres did not become ready within 180s"
)


async def _block_via_local_test_gate() -> dict:
    """Drive monitor_performer down the local-test-gate env_blocked branch and
    return the resulting state."""
    svc = _Performer(response={"status": "env_blocked", "reason": _REASON})
    state = _make_state(service=svc, stage="implementing", sequence=["implementing", "qa"])
    state["env_cache_service"] = MagicMock()
    state["current_symphony"] = "website"
    slot_mgr = MagicMock()
    slot_mgr.acquire.return_value = svc
    state["slot_manager"] = slot_mgr
    return await monitor_performer(state)


def _blocked_session_state(marker, *, cache, notify=None, stage="implementing", sequence=None):
    st = _state(notify or _Notify())
    st["current_symphony"] = "website"
    st["env_cache"] = {"website": cache}
    st["active_sessions"] = {
        "CARD_1": {
            "env_blocked": marker,
            "current_card": {"id": "CARD_1", "previous_status": "IN_PROGRESS"},
            "performer_stage": stage,
            "lifecycle_sequence": sequence or ["implementing", "qa"],
        }
    }
    return st


@pytest.mark.asyncio
async def test_local_test_gate_env_block_stamps_a_recoverable_marker() -> None:
    """M1: the branch every card took overnight must leave the evidence
    recovery keys on. The shape mirrors the CI-infrastructure marker so one
    reader serves both."""
    result = await _block_via_local_test_gate()

    assert result["phase"] == "blocked"
    marker = result.get("env_blocked")
    assert isinstance(marker, dict), "no env_blocked marker was stamped"
    assert marker["pattern_id"] == "local_test_gate"
    assert marker["check_names"] == [], (
        "check_names must be EMPTY: recovery treats any names as a CI hold that "
        "re-evaluates its own checks, and refuses to env-recover"
    )
    assert marker["reason"].startswith("env-cache services-start failed")
    assert marker["stage"] == "implementing"
    assert marker["blocked_at"]


@pytest.mark.asyncio
async def test_the_stamped_marker_recovers_once_the_cache_is_healthy(monkeypatch) -> None:
    """M1/M2: the cross-module contract that was missing. Whatever
    monitor_performer stamps is what check_board's recovery must accept, or the
    two drift apart again and the card is stranded again."""
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    blocked = await _block_via_local_test_gate()
    marker = blocked["env_blocked"]

    gh = _FakeGitHub(_ctx([]), pr=None)
    notify = _Notify()
    st = _blocked_session_state(marker, cache=_env_cache(), notify=notify)
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _NO_CONTENT_BOARD)

    assert ("CARD_1", "IN_PROGRESS") in gh.moved, (
        f"recovery did not lift the card; moved={gh.moved}"
    )
    assert [e.event_type.value for e in notify.events] == ["card_auto_recovered"]


@pytest.mark.asyncio
async def test_recovery_clears_the_marker_it_consumed(monkeypatch) -> None:
    """M3: a recovered card must not carry a stale env marker into its next
    block. Otherwise a later stale-review block would also demand
    env_recovered, for an environment problem that ended long ago."""
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    blocked = await _block_via_local_test_gate()
    st = _blocked_session_state(blocked["env_blocked"], cache=_env_cache())
    gh = _FakeGitHub(_ctx([]), pr=None)

    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _NO_CONTENT_BOARD)

    assert ("CARD_1", "IN_PROGRESS") in gh.moved
    assert st["active_sessions"]["CARD_1"]["env_blocked"] is None


# --- second defect: a recovered card resumed at a stage its PR had finished ---

def _rollup(*conclusions: str, pending: bool = False):
    from coordinare.services.pr_checks_service import CheckEntry, CheckRollup

    checks = [
        CheckEntry(
            name=f"check-{i}",
            status="in_progress" if (pending and i == 0) else "completed",
            conclusion=None if (pending and i == 0) else c,
            is_required=True,
        )
        for i, c in enumerate(conclusions)
    ]
    return CheckRollup(
        pr_number=193, head_sha="abc123", head_pushed_at=None,
        branch_protection_readable=True, checks=checks,
    )


def _pr_state(marker, *, notify=None):
    st = _blocked_session_state(marker, cache=_env_cache(), notify=notify)
    return st


_PR = {"pr_node_id": "PR1", "pr_url": "https://github.com/ViviDynamics/website/pull/193"}
_BOARD_WITH_ISSUE = {"content_node_ids": {"CARD_1": "ISSUE_1"}}


@pytest.mark.asyncio
async def test_recovery_with_a_green_pr_advances_past_implementing(monkeypatch) -> None:
    """M4: website#160 sat in BLOCKED at performer_stage=implementing with an
    open, fully green PR. Recovery resumed to IN_PROGRESS as-is, which would
    have re-run the implementer on finished work. A PR whose required checks
    all pass means implementing is done: advance to the next stage in the
    session's own lifecycle sequence, through the same _advance_stage the
    normal path uses, so the reset fields match."""
    import coordinare.graph.nodes.check_board as cb

    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    blocked = await _block_via_local_test_gate()
    st = _pr_state(blocked["env_blocked"])
    asked: list[tuple[str, str, int]] = []

    async def fake_rollup(github, owner, repo, number):
        asked.append((owner, repo, number))
        return _rollup("success", "success", "skipped")

    monkeypatch.setattr(cb, "_fetch_pr_rollup", fake_rollup)
    gh = _FakeGitHub(_ctx([], decision="APPROVED"), pr=_PR)

    await cb._attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD_WITH_ISSUE)

    sess = st["active_sessions"]["CARD_1"]
    assert ("CARD_1", "IN_PROGRESS") in gh.moved, "every performer stage runs from IN_PROGRESS"
    assert asked == [("ViviDynamics", "website", 193)], asked
    assert sess["performer_stage"] == "qa", sess.get("performer_stage")
    # Review: the first draft checked three fields and a mutation that set them
    # by hand, bypassing _advance_stage, passed. Pin the whole contract: every
    # key the real transition returns (bar current_card) must land on the
    # session with the value the transition gives it. The production clock and
    # the reprieve counter belong to ONE performer run and must die with it.
    from coordinare.graph.nodes.monitor_performer import _advance_stage

    expected = _advance_stage(
        {"lifecycle_sequence": ["implementing", "qa"], "performer_stage": "implementing"}, None
    )
    expected.pop("current_card", None)
    assert set(expected) >= {"performer_stage", "phase", "agent_dispatch", "agent_dispatch_at",
                             "last_production_at", "last_production_fingerprint", "convergence_reprieves"}
    for key, value in expected.items():
        assert sess.get(key) == value, f"{key}: {sess.get(key)!r} != {value!r}"


@pytest.mark.asyncio
async def test_recovery_with_a_red_pr_keeps_implementing(monkeypatch) -> None:
    """M5: a failing required check means the repair lane still owns the card.
    Recover it (the env is fine) but leave the stage alone."""
    import coordinare.graph.nodes.check_board as cb

    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    blocked = await _block_via_local_test_gate()
    st = _pr_state(blocked["env_blocked"])

    async def fake_rollup(github, owner, repo, number):
        return _rollup("success", "failure")

    monkeypatch.setattr(cb, "_fetch_pr_rollup", fake_rollup)
    gh = _FakeGitHub(_ctx([], decision="APPROVED"), pr=_PR)

    await cb._attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD_WITH_ISSUE)

    assert ("CARD_1", "IN_PROGRESS") in gh.moved
    assert st["active_sessions"]["CARD_1"]["performer_stage"] == "implementing"


@pytest.mark.asyncio
async def test_recovery_still_lifts_the_card_when_the_rollup_is_unreadable(monkeypatch) -> None:
    """Fail-safe: the stage reconciliation is a refinement of recovery, never a
    precondition for it. A GitHub hiccup reading checks must not leave the card
    stranded a second time."""
    import coordinare.graph.nodes.check_board as cb

    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    blocked = await _block_via_local_test_gate()
    st = _pr_state(blocked["env_blocked"])

    async def boom(github, owner, repo, number):
        raise RuntimeError("502 from GitHub")

    monkeypatch.setattr(cb, "_fetch_pr_rollup", boom)
    gh = _FakeGitHub(_ctx([], decision="APPROVED"), pr=_PR)

    await cb._attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD_WITH_ISSUE)

    assert ("CARD_1", "IN_PROGRESS") in gh.moved, "recovery must not depend on the checks read"
    assert st["active_sessions"]["CARD_1"]["performer_stage"] == "implementing"


@pytest.mark.asyncio
async def test_fetch_pr_rollup_uses_the_real_checks_service_contract(monkeypatch) -> None:
    """Review: the stage tests substitute _fetch_pr_rollup, so nothing pinned
    that the real helper builds PrChecksService the way the CI gate does and
    calls the method that exists. A fake that re-declares a contract drifts
    from it; this test holds the real one still."""
    import inspect

    import coordinare.graph.nodes.check_board as cb
    from coordinare.services import pr_checks_service as pcs

    # The real shape the helper relies on: three positional constructor args
    # after self, and an async rollup getter taking one positional argument.
    # The helper passes positionally, so names are not the contract; arity is.
    ctor = [q for q in list(inspect.signature(pcs.PrChecksService.__init__).parameters.values())[1:]
            if q.kind in (q.POSITIONAL_ONLY, q.POSITIONAL_OR_KEYWORD)]
    assert len(ctor) >= 3 and all(q.default is q.empty for q in ctor[:3]), [q.name for q in ctor]
    assert inspect.iscoroutinefunction(pcs.PrChecksService.get_pr_check_rollup)
    getter = [q for q in list(inspect.signature(pcs.PrChecksService.get_pr_check_rollup).parameters.values())[1:]
              if q.kind in (q.POSITIONAL_ONLY, q.POSITIONAL_OR_KEYWORD)]
    assert len(getter) >= 1 and getter[0].default is getter[0].empty, [q.name for q in getter]

    built: list[tuple] = []

    class _Recording:
        def __init__(self, github, owner, repo):
            built.append((github, owner, repo))

        async def get_pr_check_rollup(self, pr_number):
            built.append(("rollup", pr_number))
            return _rollup("success")

    monkeypatch.setattr(pcs, "PrChecksService", _Recording)
    rollup = await cb._fetch_pr_rollup("GH", "ViviDynamics", "website", 193)
    assert built == [("GH", "ViviDynamics", "website"), ("rollup", 193)]
    assert rollup.pr_number == 193
