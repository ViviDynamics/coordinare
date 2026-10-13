"""096/555 — check_board main-baseline tracking and the divergence sweep.

096 US1: once last_known_main_sha is RESTORED from the snapshot, a main advance
that happened while the daemon was down is noticed on the first cycle.

555: noticing is all the daemon does. It records the new main and, when
`implementer_rebase.enabled`, hands any CONFLICTING/BEHIND in-flight branch to
the implementer. It never rebases.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import initial_state

_FETCH = "coordinare.services.rebase.fetch_main_sha"


class _GitHub:
    """check_mergeability returns a configured verdict (and records call order)."""

    def __init__(self, verdicts: dict[str, dict] | None = None, *, pr_state: str = "OPEN") -> None:
        self._verdicts = verdicts or {}
        self._pr_state = pr_state
        self.checked: list[str] = []

    async def poll_board(self):
        return {
            "snapshot": {"IN_REVIEW": ["ITEM_2"]},
            "titles": {"ITEM_2": "Reviewed"},
            "descriptions": {"ITEM_2": ""},
            "issue_numbers": {"ITEM_2": 20},
            "issue_urls": {},
            "content_node_ids": {},
        }

    async def _current_token(self):
        return "test-token"

    async def check_mergeability(self, pr_id: str) -> dict:
        self.checked.append(pr_id)
        verdict = self._verdicts.get(pr_id, {})
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    async def get_pr_review_context(self, pr_id: str) -> dict:
        return {"state": self._pr_state}


def _state(github, sessions: dict, *, baseline: str | None = "same-sha", enabled: bool = True) -> dict:
    state = initial_state()
    state["github_service"] = github
    state["config"] = SimpleNamespace(
        github_org="acme", project_name="repo",
        max_concurrent_cards=3, priority=SimpleNamespace(field_name=""),
        github_api_url="",
        implementer_rebase=SimpleNamespace(enabled=enabled),
    )
    state["active_sessions"] = sessions
    state["last_known_main_sha"] = baseline
    return state


def _session(num: int, *, phase: str = "monitoring_pr", node: str | None = None) -> dict:
    return {
        "current_card": {
            "id": f"ITEM_{num}",
            "pr_url": f"https://github.com/acme/repo/pull/{num}",
            "pr_node_id": node if node is not None else f"PR_NODE_{num}",
        },
        "workspace_branch": f"coordinare/item-{num}",
        "phase": phase,
        "performer_stage": "reviewing",
    }


_CONFLICT = {"mergeable_raw": "CONFLICTING", "head_ref_oid": "headX"}


async def _run(state, main: str = "same-sha"):
    with patch(_FETCH, new=AsyncMock(return_value=main)):
        return await check_board(state)


# --- 096 US1: baseline tracking ----------------------------------------------- #


@pytest.mark.asyncio
async def test_restored_baseline_drift_updates_baseline() -> None:
    state = _state(_GitHub(), {"ITEM_2": _session(2)}, baseline="old-sha-111")
    await _run(state, "new-sha-222")
    assert state["last_known_main_sha"] == "new-sha-222"


@pytest.mark.asyncio
async def test_no_baseline_first_run_initializes() -> None:
    state = _state(_GitHub(), {"ITEM_2": _session(2)}, baseline=None)
    await _run(state, "first-sha")
    assert state["last_known_main_sha"] == "first-sha"


@pytest.mark.asyncio
async def test_main_movement_alone_requests_nothing() -> None:
    """555: 047's edge rebased every branch on a main move. Now a move with no
    platform-reported divergence leaves every session alone."""
    gh = _GitHub({"PR_NODE_2": {"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN",
                                "head_ref_oid": "headX"}})
    state = _state(gh, {"ITEM_2": _session(2)}, baseline="old-sha")
    await _run(state, "new-sha")
    sess = state["active_sessions"]["ITEM_2"]
    assert sess["phase"] == "monitoring_pr" and not sess.get("relay_feedback")
    assert "last_rebase_attempt" not in sess


# --- 555: divergence sweep ---------------------------------------------------- #


@pytest.mark.asyncio
async def test_disabled_runs_no_classification() -> None:
    gh = _GitHub({"PR_NODE_2": _CONFLICT})
    state = _state(gh, {"ITEM_2": _session(2)}, enabled=False)
    await _run(state)
    assert gh.checked == []
    assert not state["active_sessions"]["ITEM_2"].get("relay_feedback")


@pytest.mark.asyncio
@pytest.mark.parametrize("main", ["same-sha", "moved-sha"])
async def test_conflicting_branch_is_handed_to_implementer(main) -> None:
    """With or without a main-moved edge, a CONFLICTING PR is routed."""
    gh = _GitHub({"PR_NODE_2": _CONFLICT})
    state = _state(gh, {"ITEM_2": _session(2)})
    result = await _run(state, main)

    sess = state["active_sessions"]["ITEM_2"]
    assert sess["last_rebase_attempt"] == {"main_sha": main, "head_sha": "headX", "outcome": "delegated"}
    assert sess["performer_stage"] == "implementing" and sess["phase"] == "dispatching"
    assert "coordinare/item-2" in sess["relay_feedback"][0]["body"]
    job = result["last_rebase_round"]["jobs"][0]
    assert job["card_id"] == "ITEM_2" and job["outcome"] == "delegated"


@pytest.mark.asyncio
async def test_behind_branch_is_handed_to_implementer() -> None:
    gh = _GitHub({"PR_NODE_2": {"mergeable_raw": "MERGEABLE", "merge_state_status": "BEHIND",
                                "head_ref_oid": "headB"}})
    state = _state(gh, {"ITEM_2": _session(2)})
    await _run(state)
    assert "is behind `main`" in state["active_sessions"]["ITEM_2"]["relay_feedback"][0]["body"]


@pytest.mark.asyncio
async def test_existing_feedback_is_kept_behind_the_instruction() -> None:
    gh = _GitHub({"PR_NODE_2": _CONFLICT})
    sess = _session(2)
    sess["relay_feedback"] = [{"body": "reviewer note"}]
    state = _state(gh, {"ITEM_2": sess})
    await _run(state)
    bodies = [item["body"] for item in sess["relay_feedback"]]
    assert len(bodies) == 2 and bodies[1] == "reviewer note"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [
    {"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN", "head_ref_oid": "h"},
    {"mergeable_raw": "UNKNOWN", "head_ref_oid": "h"},
    {"mergeable_raw": "CONFLICTING", "head_ref_oid": ""},
])
async def test_current_unknown_or_headless_branch_is_left_alone(verdict) -> None:
    gh = _GitHub({"PR_NODE_2": verdict})
    state = _state(gh, {"ITEM_2": _session(2)})
    await _run(state)
    sess = state["active_sessions"]["ITEM_2"]
    assert not sess.get("relay_feedback") and "last_rebase_attempt" not in sess


@pytest.mark.asyncio
async def test_active_performer_is_never_rerouted() -> None:
    """047 FR-006: a card being worked is not even looked up."""
    gh = _GitHub({"PR_NODE_2": _CONFLICT})
    state = _state(gh, {"ITEM_2": _session(2, phase="monitoring_performer")})
    await _run(state)
    assert gh.checked == []
    assert state["active_sessions"]["ITEM_2"]["phase"] == "monitoring_performer"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["blocked", "failed", "delegated"])
async def test_thrash_guard_skips_repeat_request(outcome) -> None:
    gh = _GitHub({"PR_NODE_2": _CONFLICT})
    sess = _session(2)
    sess["last_rebase_attempt"] = {"main_sha": "same-sha", "head_sha": "headX", "outcome": outcome}
    state = _state(gh, {"ITEM_2": sess})
    await _run(state)
    assert not sess.get("relay_feedback") and sess["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_closed_pr_is_deferred_without_marker() -> None:
    gh = _GitHub({"PR_NODE_2": _CONFLICT}, pr_state="CLOSED")
    state = _state(gh, {"ITEM_2": _session(2)})
    result = await _run(state)
    sess = state["active_sessions"]["ITEM_2"]
    assert not sess.get("relay_feedback") and "last_rebase_attempt" not in sess
    assert result["last_rebase_round"]["jobs"][0]["outcome"] == "deferred"


@pytest.mark.asyncio
async def test_per_card_isolation() -> None:
    """One card's lookup failure does not stop the sweep reaching the next."""
    gh = _GitHub({"PR_NODE_2": RuntimeError("boom"), "PR_NODE_3": _CONFLICT})
    state = _state(gh, {"ITEM_2": _session(2), "ITEM_3": _session(3)})
    await _run(state)
    assert set(gh.checked) == {"PR_NODE_2", "PR_NODE_3"}
    assert state["active_sessions"]["ITEM_3"]["last_rebase_attempt"]["outcome"] == "delegated"
    assert not state["active_sessions"]["ITEM_2"].get("relay_feedback")


@pytest.mark.asyncio
async def test_sweep_emits_secret_free_observability() -> None:
    import structlog.testing

    gh = _GitHub({"PR_NODE_2": _CONFLICT})
    state = _state(gh, {"ITEM_2": _session(2)})
    with structlog.testing.capture_logs() as logs:
        await _run(state)

    rec = next(e for e in logs if e["event"] == "rebase.triggered")
    assert rec["reason"] == "proactive_conflict"
    assert rec["card_id"] == "ITEM_2"
    assert rec["current_main_sha"] == "same-sha"
    assert rec["outcome"] == "delegated"
    blob = " ".join(str(v) for v in rec.values()).lower()
    assert "token" not in blob and "secret" not in blob and "password" not in blob


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [{"status": "running"}, {"status": "done", "writer_active": True}])
async def test_documenter_side_run_is_never_rebased_under(side) -> None:
    """165: a side run may still push to the branch; no history rewrite is asked
    for (and the PR is not even looked up) until it has finished."""
    gh = _GitHub({"PR_NODE_2": _CONFLICT})
    sess = _session(2)
    sess["documenting_side"] = side
    state = _state(gh, {"ITEM_2": sess})
    await _run(state)
    assert gh.checked == [] and not sess.get("relay_feedback")

    sess["documenting_side"] = {"status": "done"}
    await _run(state)
    assert sess["last_rebase_attempt"]["outcome"] == "delegated"
    assert sess["relay_feedback"][0]["expected_head_sha"] == "headX"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["blocked", "merging", "system_error", "dispatching", "idle"])
async def test_sweep_only_reroutes_cards_waiting_on_their_pr(phase) -> None:
    """Review of #104: a human hold (blocked), a merge in progress or an errored
    card is never yanked to the implementer by the sweep; a dispatching card is
    the pre-dispatch guard's to handle."""
    gh = _GitHub({"PR_NODE_2": _CONFLICT})
    sess = _session(2, phase=phase)
    state = _state(gh, {"ITEM_2": sess})
    await _run(state)
    assert gh.checked == []
    assert sess["phase"] == phase and sess["performer_stage"] == "reviewing"
    assert not sess.get("relay_feedback") and "last_rebase_attempt" not in sess
