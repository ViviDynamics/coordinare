"""Spec 125 — verdict-cache skip decision table (contract V1-V7, invariants N1-N3).

A verdict stage whose recorded passing verdict matches the LIVE remote head
exactly is skipped (advance without dispatch); every other row dispatches.
Fail-open on any uncertainty; overrides and pending feedback always dispatch.

Contract: specs/125-stage-verdict-memory/contracts/skip-decision.md
"""
from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state


class _Service:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def check_health(self) -> dict[str, Any]:
        return {"status": "accepted"}

    async def dispatch_card(
        self, card_context: dict[str, Any], workspace_info: Any = None
    ) -> dict[str, Any]:
        self.dispatched.append(card_context)
        return {"status": "accepted", "session_id": "sess-1"}


class _GitHub:
    """Stub with mergeability + diff surfaces the cache/doc-gate consult."""

    def __init__(
        self,
        *,
        head_ref_oid: str = "",
        mergeability_exc: Exception | None = None,
        diff_files: list[str] | None = None,
    ) -> None:
        self._head_ref_oid = head_ref_oid
        self._mergeability_exc = mergeability_exc
        self._diff_files = diff_files if diff_files is not None else ["src/app.py"]
        self.mergeability_calls: list[str] = []
        self.get_pr_diff_calls: list[str] = []
        self.move_calls: list[tuple[str, str]] = []

    async def check_mergeability(self, pr_id: str) -> dict[str, Any]:
        self.mergeability_calls.append(pr_id)
        if self._mergeability_exc is not None:
            raise self._mergeability_exc
        return {"head_ref_oid": self._head_ref_oid, "mergeable": True}

    async def get_pr_diff(self, pr_url: str) -> tuple[str, list[str]]:
        self.get_pr_diff_calls.append(pr_url)
        return "diff --git a/x b/x", list(self._diff_files)

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))


class _GitHubNoMergeability:
    """Stub lacking check_mergeability entirely (V4 attr fail-open)."""

    async def get_pr_diff(self, pr_url: str) -> tuple[str, list[str]]:
        return "diff --git a/x b/x", ["src/app.py"]

    async def move_card(self, item_id: str, status: str) -> None:
        return None


_CARD = {
    "id": "ITEM_V",
    "status": "TODO",
    "pr_url": "https://github.com/acme/repo/pull/7",
    "pr_node_id": "PR_node_7",
}

_VERDICT = {
    "head_sha": "abc123",
    "verdict": "approved",
    "recorded_at": "2026-07-04T10:00:00+00:00",
}


def _state(github: Any, svc: _Service, **overrides: Any) -> dict[str, Any]:
    state = initial_state()
    state["current_card"] = dict(_CARD)
    state["github_service"] = github
    state["performer_services"] = {"reviewing": svc, "security": _Service()}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["reviewing", "security"]
    state["stage_verdicts"] = {"reviewing": dict(_VERDICT)}
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# V7 — full match skips: advance without dispatch, distinguishable log (N3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matching_verdict_and_live_head_skips_dispatch() -> None:
    svc = _Service()
    github = _GitHub(head_ref_oid="abc123")
    state = _state(github, svc)

    with capture_logs() as logs:
        await dispatch_performer(state)

    assert svc.dispatched == []
    assert state["performer_stage"] == "security"
    assert state["phase"] == "dispatching"
    skips = [
        e for e in logs
        if e.get("event") == "dispatch_performer.stage_skipped"
        and e.get("reason") == "verdict_cached"
    ]
    assert len(skips) == 1
    assert skips[0]["head_sha"] == "abc123"


# ---------------------------------------------------------------------------
# V6 — live head differs: dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_head_mismatch_dispatches() -> None:
    svc = _Service()
    state = _state(_GitHub(head_ref_oid="def999"), svc)

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1


# ---------------------------------------------------------------------------
# V4 — live head unresolvable: dispatch (fail-open, N1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mergeability_error_dispatches() -> None:
    svc = _Service()
    state = _state(_GitHub(mergeability_exc=RuntimeError("boom")), svc)
    await dispatch_performer(state)
    assert len(svc.dispatched) == 1


@pytest.mark.asyncio
async def test_empty_head_ref_oid_dispatches() -> None:
    svc = _Service()
    state = _state(_GitHub(head_ref_oid=""), svc)
    await dispatch_performer(state)
    assert len(svc.dispatched) == 1


@pytest.mark.asyncio
async def test_github_without_mergeability_dispatches() -> None:
    svc = _Service()
    state = _state(_GitHubNoMergeability(), svc)
    await dispatch_performer(state)
    assert len(svc.dispatched) == 1


@pytest.mark.asyncio
async def test_missing_pr_node_id_dispatches() -> None:
    svc = _Service()
    github = _GitHub(head_ref_oid="abc123")
    card = dict(_CARD)
    card.pop("pr_node_id")
    state = _state(github, svc, current_card=card)
    await dispatch_performer(state)
    assert len(svc.dispatched) == 1


# ---------------------------------------------------------------------------
# V3/V5 — missing record / wrong verdict: dispatch, no API call for V3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_record_dispatches_without_head_fetch() -> None:
    svc = _Service()
    github = _GitHub(head_ref_oid="abc123")
    state = _state(github, svc, stage_verdicts={})

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert github.mergeability_calls == []


@pytest.mark.asyncio
async def test_wrong_verdict_marker_dispatches() -> None:
    svc = _Service()
    github = _GitHub(head_ref_oid="abc123")
    bad = dict(_VERDICT, verdict="qa_passed")
    state = _state(github, svc, stage_verdicts={"reviewing": bad})

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1


# ---------------------------------------------------------------------------
# V2 — pending relay feedback vetoes the skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_relay_feedback_dispatches() -> None:
    svc = _Service()
    github = _GitHub(head_ref_oid="abc123")
    state = _state(
        github, svc,
        relay_feedback=[{"body": "please fix", "author_login": "coordinare"}],
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1


# ---------------------------------------------------------------------------
# V1 — operator override always dispatches; flag is one-shot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_forced_dispatch_bypasses_cache_and_clears_flag() -> None:
    svc = _Service()
    github = _GitHub(head_ref_oid="abc123")
    state = _state(github, svc, override_forced_dispatch="reviewing")

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert not state.get("override_forced_dispatch")


# ---------------------------------------------------------------------------
# 126 D6 (V2b) — a pending dispute for the stage vetoes the cache skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_dispute_for_stage_vetoes_cache_skip() -> None:
    svc = _Service()
    github = _GitHub(head_ref_oid="abc123")
    state = _state(
        github, svc,
        feedback_ledger=[{
            "id": "fb-1",
            "raiser": "reviewing",
            "origin_sha": "abc123",
            "body_digest": "wrong finding",
            "disposition": "disputed",
            "dispute_reason": "already correct",
            "re_raised": False,
            "round_status": "current",
        }],
    )

    await dispatch_performer(state)

    # Cache would have skipped (matching verdict + head) — the dispute forces
    # the dispatch so the raiser adjudicates it.
    assert len(svc.dispatched) == 1
    # 126 D1: the dispute rides the raiser's dispatch context.
    disputed = svc.dispatched[0].get("disputed_feedback")
    assert disputed and disputed[0]["id"] == "fb-1"
    assert disputed[0]["reason"] == "already correct"


@pytest.mark.asyncio
async def test_dispute_for_other_stage_does_not_veto() -> None:
    svc = _Service()
    github = _GitHub(head_ref_oid="abc123")
    state = _state(
        github, svc,
        feedback_ledger=[{
            "id": "fb-1",
            "raiser": "qa",
            "origin_sha": "abc123",
            "body_digest": "x",
            "disposition": "disputed",
            "dispute_reason": "r",
            "re_raised": False,
            "round_status": "current",
        }],
    )

    await dispatch_performer(state)

    # reviewing's cache skip stands — the dispute belongs to qa.
    assert svc.dispatched == []


# ---------------------------------------------------------------------------
# FR-004 — implementing never consults the cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_implementing_stage_never_consults_cache() -> None:
    svc = _Service()
    github = _GitHub(head_ref_oid="abc123")
    bogus = {"implementing": dict(_VERDICT, verdict="pr_opened")}
    state = _state(
        github, svc,
        performer_services={"implementing": svc},
        performer_stage="implementing",
        lifecycle_sequence=["implementing"],
        stage_verdicts=bogus,
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert github.mergeability_calls == []
