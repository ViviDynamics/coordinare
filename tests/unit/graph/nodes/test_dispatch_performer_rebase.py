"""097 — pre-dispatch rebase guard in dispatch_performer.

Tests `_pre_dispatch_rebase_guard` directly (returns True=proceed-with-dispatch /
False=skip-this-cycle and mutates state), covering US1/US2/US3, plus a wiring
test that the dispatch node skips `_dispatch_performer_body` when the guard
returns False.

NOTE: the guard sources the branch from the PR (`head_ref_name`), NOT
`workspace_branch` (which is unset at pre-dispatch time) — so the test state does
NOT set workspace_branch; the conflicting verdicts carry a `head_ref_name`.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from coordinare.graph.nodes.dispatch_performer import _pre_dispatch_rebase_guard
from coordinare.models.rebase import RebaseJob, RebaseOutcome, RebaseRound

_BRANCH = "coordinare/item-2"


class _GH:
    def __init__(self, verdict: dict) -> None:
        self._verdict = verdict
        self.merge_calls = 0

    async def _current_token(self) -> str:
        return "tok"

    async def check_mergeability(self, pr_id: str) -> dict:
        self.merge_calls += 1
        return self._verdict


def _state(verdict: dict | None, *, pr: bool = True, marker: dict | None = None,
           relay: list | None = None) -> dict:
    card = {"id": "ITEM_2"}
    if pr:
        card.update({"pr_url": "https://github.com/acme/repo/pull/2", "pr_node_id": "PR_NODE_2"})
    s: dict = {
        "current_card": card,
        "phase": "dispatching",  # NOTE: no workspace_branch — realistic pre-dispatch
        "config": SimpleNamespace(github_org="acme", project_name="repo"),
        "github_service": _GH(verdict or {}),
        "last_known_main_sha": "main-sha-0",
    }
    if marker is not None:
        s["last_rebase_attempt"] = marker
    if relay is not None:
        s["relay_feedback"] = relay
    return s


def _conflicting(head: str = "hX") -> dict:
    return {"mergeable_raw": "CONFLICTING", "merge_state_status": "DIRTY",
            "head_ref_oid": head, "head_ref_name": _BRANCH}


def _rr(outcome: RebaseOutcome, *, post: str = ""):
    return RebaseRound(
        trigger_sha="main-sha-0",
        jobs=[RebaseJob(card_id="ITEM_2", branch=_BRANCH,
                        pre_rebase_sha="h0", post_rebase_sha=post, outcome=outcome)],
    )


_RU = "coordinare.services.rebase.repo_url_from_config"
_RR = "coordinare.services.rebase.run_rebase_round"
_PC = "coordinare.services.rebase.prepare_conflict_resolution"


@pytest.mark.asyncio
async def test_no_pr_skips_guard_and_proceeds() -> None:
    """FR-006: a card with no open PR (first run) → guard N/A, dispatch proceeds."""
    st = _state(None, pr=False)
    with patch(_RU, return_value="https://github.com/acme/repo.git"), patch(_RR, new=AsyncMock()) as rr:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert st["github_service"].merge_calls == 0
    rr.assert_not_called()


@pytest.mark.asyncio
async def test_relay_feedback_dispatch_is_let_through() -> None:
    """A feedback-driven dispatch (incl. 047 conflict-resolution) proceeds without
    the guard intercepting — so the resolution path is never starved."""
    st = _state(_conflicting(), relay=[{"body": "**Rebase conflict** ..."}])
    with patch(_RU, return_value="https://github.com/acme/repo.git"), patch(_RR, new=AsyncMock()) as rr:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert st["github_service"].merge_calls == 0  # not even consulted
    rr.assert_not_called()


@pytest.mark.asyncio
async def test_conflict_resolution_is_bounded_to_one_attempt_per_head() -> None:
    """Round-3 review: prove the relay_feedback passthrough is BOUNDED — a conflict
    gets at most one performer resolution attempt per head, then is held
    (blocked_thrash), never an unbounded dispatch-onto-conflict.

    Cycle 1: fresh dispatch, CONFLICTING → rebase BLOCKED → set up resolution
             (relay_feedback + marker) → defer (no dispatch).
    Cycle 2: relay_feedback present → passthrough (the ONE resolution dispatch);
             _dispatch_performer_body would consume relay_feedback here.
    Cycle 3: feedback consumed, head unchanged → blocked_thrash → held.
    """
    # Cycle 1
    st = _state(_conflicting(head="hX"))
    with patch(_RU, return_value="https://github.com/acme/repo.git"), \
         patch(_RR, new=AsyncMock(return_value=_rr(RebaseOutcome.BLOCKED))), \
         patch(_PC):
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    assert st["last_rebase_attempt"]["outcome"] == "blocked"

    # Cycle 2 — resolution feedback is now present → exactly one passthrough
    st["relay_feedback"] = [{"body": "**Rebase conflict** resolve files ..."}]
    with patch(_RU, return_value="https://github.com/acme/repo.git"), patch(_RR, new=AsyncMock()) as rr2:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    rr2.assert_not_called()  # passthrough, no new rebase

    # Cycle 3 — body consumed relay_feedback; head still hX → held, no re-dispatch
    st["relay_feedback"] = []
    with patch(_RU, return_value="https://github.com/acme/repo.git"), patch(_RR, new=AsyncMock()) as rr3:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    rr3.assert_not_called()


@pytest.mark.asyncio
async def test_current_branch_proceeds_no_rebase() -> None:
    """SC-003: MERGEABLE/current → proceed, no rebase."""
    st = _state({"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN", "head_ref_oid": "h0"})
    with patch(_RU, return_value="https://github.com/acme/repo.git"), patch(_RR, new=AsyncMock()) as rr:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    rr.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_mergeability_defers() -> None:
    """FR-005: UNKNOWN → defer (no dispatch, no rebase)."""
    st = _state({"mergeable_raw": "UNKNOWN", "head_ref_oid": "h0"})
    with patch(_RU, return_value="https://github.com/acme/repo.git"), patch(_RR, new=AsyncMock()) as rr:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    rr.assert_not_called()


@pytest.mark.asyncio
async def test_conflicting_clean_rebase_then_proceeds() -> None:
    """FR-001/002: CONFLICTING (branch from the PR) → rebase; clean → proceed;
    marker records the POST-rebase head + rebase.triggered emitted."""
    st = _state(_conflicting())
    with patch(_RU, return_value="https://github.com/acme/repo.git"), \
         patch(_RR, new=AsyncMock(return_value=_rr(RebaseOutcome.CLEAN, post="h1"))) as rr:
        proceed = await _pre_dispatch_rebase_guard(st, "ITEM_2")
    assert proceed is True
    rr.assert_called_once()
    # run_rebase_round was given the PR branch despite no workspace_branch in state
    sessions = rr.call_args.args[0]
    assert sessions["ITEM_2"]["workspace_branch"] == _BRANCH
    assert st["last_rebase_attempt"] == {"main_sha": "main-sha-0", "head_sha": "h1", "outcome": "clean"}


@pytest.mark.asyncio
async def test_conflicting_blocked_defers_after_routing_to_resolution() -> None:
    """FR-003/FR-004: a genuine conflict → set up 047 conflict-resolution feedback
    and DEFER (return False) so next cycle re-validates the implementer stage via
    check_inflight before dispatching. Marker=blocked."""
    st = _state(_conflicting())
    with patch(_RU, return_value="https://github.com/acme/repo.git"), \
         patch(_RR, new=AsyncMock(return_value=_rr(RebaseOutcome.BLOCKED))), \
         patch(_PC) as pc:
        proceed = await _pre_dispatch_rebase_guard(st, "ITEM_2")
    assert proceed is False  # do NOT dispatch this cycle (FR-004)
    pc.assert_called_once()
    assert st["last_rebase_attempt"]["outcome"] == "blocked"


@pytest.mark.asyncio
async def test_failed_rebase_skips_dispatch() -> None:
    """A FAILED rebase → do not dispatch this cycle (retry); marker=failed."""
    st = _state(_conflicting())
    with patch(_RU, return_value="https://github.com/acme/repo.git"), \
         patch(_RR, new=AsyncMock(return_value=_rr(RebaseOutcome.FAILED))):
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    assert st["last_rebase_attempt"]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_conflicting_unknown_branch_defers_not_dispatches() -> None:
    """FR-002 (review fix): CONFLICTING but the branch can't be identified
    (no head_ref_name, no workspace_branch) → DEFER (False), never degrade to a
    dispatch onto the conflicting base."""
    st = _state({"mergeable_raw": "CONFLICTING", "merge_state_status": "DIRTY",
                 "head_ref_oid": "hX"})  # no head_ref_name
    with patch(_RU, return_value="https://github.com/acme/repo.git"), patch(_RR, new=AsyncMock()) as rr:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    rr.assert_not_called()
    # review fix #1/#2: a marker is written so the guard doesn't re-attempt every
    # cycle (next cycle → blocked_thrash → held).
    assert st["last_rebase_attempt"] == {"main_sha": "main-sha-0", "head_sha": "hX", "outcome": "failed"}


@pytest.mark.asyncio
async def test_rr_jobs_empty_writes_marker_and_defers() -> None:
    """Review fix #1/#2: CONFLICTING branch but run_rebase_round returns no jobs →
    defer (False) AND write a marker so the guard doesn't loop re-rebasing it."""
    st = _state(_conflicting())
    empty = RebaseRound(trigger_sha="main-sha-0", jobs=[])
    with patch(_RU, return_value="https://github.com/acme/repo.git"), \
         patch(_RR, new=AsyncMock(return_value=empty)):
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    assert st["last_rebase_attempt"] == {"main_sha": "main-sha-0", "head_sha": "hX", "outcome": "failed"}
    # and next cycle, the same state is thrash-guarded (no re-rebase)
    with patch(_RU, return_value="https://github.com/acme/repo.git"), patch(_RR, new=AsyncMock()) as rr2:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    rr2.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_thrash_holds_without_rebase() -> None:
    """FR-008: already BLOCKED against the same (main, head) → held, no re-rebase."""
    st = _state(_conflicting(),
                marker={"main_sha": "main-sha-0", "head_sha": "hX", "outcome": "blocked"})
    with patch(_RU, return_value="https://github.com/acme/repo.git"), patch(_RR, new=AsyncMock()) as rr:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    rr.assert_not_called()


@pytest.mark.asyncio
async def test_guard_failure_is_isolated_and_proceeds() -> None:
    """FR-009: any guard error degrades to a normal dispatch (returns True)."""
    st = _state(_conflicting())
    with patch(_RU, return_value="https://github.com/acme/repo.git"), \
         patch(_RR, new=AsyncMock(side_effect=RuntimeError("git boom"))):
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True


@pytest.mark.asyncio
async def test_observability_secret_free() -> None:
    """FR-010: rebase.triggered(reason=pre_dispatch) carries only ids/sha/outcome."""
    import structlog.testing

    st = _state(_conflicting())
    with patch(_RU, return_value="https://github.com/acme/repo.git"), \
         patch(_RR, new=AsyncMock(return_value=_rr(RebaseOutcome.CLEAN, post="h1"))), \
         structlog.testing.capture_logs() as logs:
        await _pre_dispatch_rebase_guard(st, "ITEM_2")
    rec = next(e for e in logs if e["event"] == "rebase.triggered")
    assert rec["reason"] == "pre_dispatch" and rec["outcome"] == "clean"
    blob = " ".join(str(v) for v in rec.values()).lower()
    assert "tok" not in blob and "token" not in blob and "secret" not in blob


# --- call-site wiring: dispatch_performer routes on the guard's bool ---------- #


@pytest.mark.asyncio
async def test_dispatch_node_skips_body_when_guard_holds() -> None:
    """The dispatch node must NOT run _dispatch_performer_body when the guard
    returns False (held/defer), and MUST run it when the guard returns True."""
    from coordinare.graph.nodes.dispatch_performer import dispatch_performer
    from tests.unit.graph.nodes.test_dispatch_performer import _base_state

    base = _base_state(performer_stage="implementing")

    with patch("coordinare.graph.nodes.dispatch_performer._pre_dispatch_rebase_guard",
               new=AsyncMock(return_value=False)), \
         patch("coordinare.graph.nodes.dispatch_performer._dispatch_performer_body",
               new=AsyncMock()) as body:
        await dispatch_performer(dict(base))
    body.assert_not_called()

    with patch("coordinare.graph.nodes.dispatch_performer._pre_dispatch_rebase_guard",
               new=AsyncMock(return_value=True)), \
         patch("coordinare.graph.nodes.dispatch_performer._dispatch_performer_body",
               new=AsyncMock(return_value={"phase": "monitoring_performer"})) as body:
        await dispatch_performer(dict(base))
    body.assert_called_once()
