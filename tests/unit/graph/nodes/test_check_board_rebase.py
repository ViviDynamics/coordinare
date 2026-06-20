"""096 — auto-rebase restart resilience: trigger tests for check_board.

US1 (P1): once last_known_main_sha is RESTORED from the snapshot (persistence,
Phase 2), the existing edge (`current_main != prev_main`) fires on the first
post-restart cycle when main advanced while the daemon was down — and does NOT
fire spuriously when nothing changed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import initial_state
from coordinare.models.rebase import RebaseJob, RebaseOutcome, RebaseRound


class _GitHubWithToken:
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


def _state_with_inflight_pr(last_known_main: str | None) -> dict:
    state = initial_state()
    state["github_service"] = _GitHubWithToken()
    state["config"] = SimpleNamespace(
        github_org="acme", project_name="repo",
        max_concurrent_cards=2, priority=SimpleNamespace(field_name=""),
        github_api_url="",
    )
    state["active_sessions"] = {
        "ITEM_2": {
            "current_card": {"id": "ITEM_2", "pr_url": "https://github.com/acme/repo/pull/20"},
            "workspace_branch": "coordinare/item-2",
            "phase": "monitoring_performer",
        },
    }
    state["last_known_main_sha"] = last_known_main
    return state


@pytest.mark.asyncio
async def test_restored_baseline_drift_triggers_rebase() -> None:
    """US1 (FR-001/002, SC-001): a restored OLD baseline + an advanced live main
    (the cross-restart-drift case) fires run_rebase_round and updates the baseline."""
    rr = RebaseRound(
        trigger_sha="new-sha-222",
        jobs=[RebaseJob(card_id="ITEM_2", branch="coordinare/item-2", outcome=RebaseOutcome.CLEAN)],
    )
    state = _state_with_inflight_pr("old-sha-restored")
    mock_rebase = AsyncMock(return_value=rr)
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="new-sha-222")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        result = await check_board(state)

    mock_rebase.assert_called_once()
    assert result.get("last_known_main_sha") == "new-sha-222"


@pytest.mark.asyncio
async def test_restored_baseline_no_drift_does_not_rebase() -> None:
    """US1 edge (SC-003): a restored baseline that EQUALS the live main (restart
    with no intervening merge) must NOT trigger a rebase — no spurious churn."""
    state = _state_with_inflight_pr("same-sha-999")
    mock_rebase = AsyncMock()
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="same-sha-999")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        await check_board(state)

    mock_rebase.assert_not_called()


@pytest.mark.asyncio
async def test_no_baseline_first_run_initializes_without_rebase() -> None:
    """US1 (FR-001, first run / post-upgrade): with no prior baseline (None), the
    cycle initializes last_known_main_sha to live main and does not edge-rebase."""
    state = _state_with_inflight_pr(None)
    mock_rebase = AsyncMock()
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="fresh-sha-000")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        result = await check_board(state)

    mock_rebase.assert_not_called()
    assert result.get("last_known_main_sha") == "fresh-sha-000"


# --------------------------------------------------------------------------- #
# US2 (P2): proactive conflicting/behind-branch rebase, independent of the edge
# --------------------------------------------------------------------------- #


class _GitHubWithMergeability(_GitHubWithToken):
    """check_mergeability returns a configured verdict (and records call order)."""

    def __init__(self, verdicts: dict[str, dict]) -> None:
        self._verdicts = verdicts
        self.checked: list[str] = []

    async def check_mergeability(self, pr_id: str) -> dict:
        self.checked.append(pr_id)
        return self._verdicts.get(pr_id, {})


def _proactive_state(github, sessions: dict, baseline: str = "same-sha") -> dict:
    state = initial_state()
    state["github_service"] = github
    state["config"] = SimpleNamespace(
        github_org="acme", project_name="repo",
        max_concurrent_cards=3, priority=SimpleNamespace(field_name=""),
        github_api_url="",
    )
    state["active_sessions"] = sessions
    state["last_known_main_sha"] = baseline  # == live main → no edge, only proactive
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
    }


def _rr_for(card_id: str, outcome: RebaseOutcome):
    return RebaseRound(
        trigger_sha="same-sha",
        jobs=[RebaseJob(card_id=card_id, branch=f"coordinare/{card_id}",
                        pre_rebase_sha="head1", outcome=outcome)],
    )


@pytest.mark.asyncio
async def test_proactive_conflicting_branch_triggers_rebase() -> None:
    """US2 (FR-003, SC-002): baseline == live main, but a PR is CONFLICTING →
    a rebase is initiated for that card with no main-moved edge."""
    gh = _GitHubWithMergeability({"PR_NODE_2": {"mergeable_raw": "CONFLICTING",
                                                "head_ref_oid": "headX"}})
    state = _proactive_state(gh, {"ITEM_2": _session(2)})
    mock_rebase = AsyncMock(return_value=_rr_for("ITEM_2", RebaseOutcome.CLEAN))
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="same-sha")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        result = await check_board(state)

    mock_rebase.assert_called_once()
    marker = state["active_sessions"]["ITEM_2"]["last_rebase_attempt"]
    assert marker["main_sha"] == "same-sha" and marker["outcome"] == "clean"
    assert result.get("last_rebase_round") is not None


@pytest.mark.asyncio
async def test_proactive_behind_branch_triggers_rebase() -> None:
    gh = _GitHubWithMergeability({"PR_NODE_2": {"mergeable_raw": "MERGEABLE",
                                                "merge_state_status": "BEHIND",
                                                "head_ref_oid": "headX"}})
    state = _proactive_state(gh, {"ITEM_2": _session(2)})
    mock_rebase = AsyncMock(return_value=_rr_for("ITEM_2", RebaseOutcome.CLEAN))
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="same-sha")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        await check_board(state)
    mock_rebase.assert_called_once()


@pytest.mark.asyncio
async def test_proactive_current_branch_no_rebase() -> None:
    """SC-003: a MERGEABLE/CLEAN branch is never rebased (no churn)."""
    gh = _GitHubWithMergeability({"PR_NODE_2": {"mergeable_raw": "MERGEABLE",
                                                "merge_state_status": "CLEAN",
                                                "head_ref_oid": "headX"}})
    state = _proactive_state(gh, {"ITEM_2": _session(2)})
    mock_rebase = AsyncMock()
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="same-sha")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        await check_board(state)
    mock_rebase.assert_not_called()


@pytest.mark.asyncio
async def test_proactive_unknown_mergeability_defers() -> None:
    """Defer on UNKNOWN — never rebase on an uncomputed mergeability signal."""
    gh = _GitHubWithMergeability({"PR_NODE_2": {"mergeable_raw": "UNKNOWN",
                                                "head_ref_oid": "headX"}})
    state = _proactive_state(gh, {"ITEM_2": _session(2)})
    mock_rebase = AsyncMock()
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="same-sha")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        await check_board(state)
    mock_rebase.assert_not_called()


@pytest.mark.asyncio
async def test_proactive_thrash_guard_skips_repeat_blocked() -> None:
    """FR-007: a card BLOCKED against the same (main, head) is not re-attempted."""
    gh = _GitHubWithMergeability({"PR_NODE_2": {"mergeable_raw": "CONFLICTING",
                                                "head_ref_oid": "headX"}})
    sess = _session(2)
    sess["last_rebase_attempt"] = {"main_sha": "same-sha", "head_sha": "headX", "outcome": "blocked"}
    state = _proactive_state(gh, {"ITEM_2": sess})
    mock_rebase = AsyncMock()
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="same-sha")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        await check_board(state)
    mock_rebase.assert_not_called()


@pytest.mark.asyncio
async def test_proactive_per_card_isolation() -> None:
    """FR-006/SC-004: one card's rebase failure does not block the others."""
    gh = _GitHubWithMergeability({
        "PR_NODE_2": {"mergeable_raw": "CONFLICTING", "head_ref_oid": "h2"},
        "PR_NODE_3": {"mergeable_raw": "CONFLICTING", "head_ref_oid": "h3"},
    })
    state = _proactive_state(gh, {"ITEM_2": _session(2), "ITEM_3": _session(3)})

    calls: list[str] = []

    async def _flaky(sessions, main, repo, token, **kw):
        cid = next(iter(sessions))
        calls.append(cid)
        if cid == "ITEM_2":
            raise RuntimeError("transient git error")
        return _rr_for(cid, RebaseOutcome.CLEAN)

    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="same-sha")),
        patch("coordinare.services.rebase.run_rebase_round", new=_flaky),
    ):
        await check_board(state)

    # both cards were attempted despite ITEM_2 raising
    assert set(calls) == {"ITEM_2", "ITEM_3"}


@pytest.mark.asyncio
async def test_proactive_rebase_emits_secret_free_observability(caplog) -> None:
    """US3 (T014/FR-008/009): a triggered rebase emits rebase.triggered with the
    id/branch/sha/outcome fields and no secret values."""
    import structlog.testing

    gh = _GitHubWithMergeability({"PR_NODE_2": {"mergeable_raw": "CONFLICTING",
                                                "head_ref_oid": "headX"}})
    state = _proactive_state(gh, {"ITEM_2": _session(2)})
    mock_rebase = AsyncMock(return_value=_rr_for("ITEM_2", RebaseOutcome.CLEAN))
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="same-sha")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
        structlog.testing.capture_logs() as logs,
    ):
        await check_board(state)

    rec = next(e for e in logs if e["event"] == "rebase.triggered")
    assert rec["reason"] == "proactive_conflict"
    assert rec["card_id"] == "ITEM_2"
    assert rec["current_main_sha"] == "same-sha"
    assert rec["outcome"] == "clean"
    blob = " ".join(str(v) for v in rec.values()).lower()
    assert "token" not in blob and "secret" not in blob and "password" not in blob


@pytest.mark.asyncio
async def test_edge_rebase_writes_anti_thrash_marker() -> None:
    """096 (review fix, FR-007): the EDGE path must record last_rebase_attempt for
    every rebased card — otherwise a just-BLOCKED branch is re-attempted by the
    next cycle's proactive sweep (no marker → should_attempt_rebase returns True)."""
    rr = RebaseRound(
        trigger_sha="new-sha-222",
        jobs=[RebaseJob(card_id="ITEM_2", branch="coordinare/item-2",
                        pre_rebase_sha="headBLOCK", outcome=RebaseOutcome.BLOCKED)],
    )
    state = _state_with_inflight_pr("old-sha-restored")
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="new-sha-222")),
        patch("coordinare.services.rebase.run_rebase_round", new=AsyncMock(return_value=rr)),
        patch("coordinare.services.rebase.prepare_conflict_resolution", new=lambda *a, **k: None),
    ):
        await check_board(state)

    marker = state["active_sessions"]["ITEM_2"]["last_rebase_attempt"]
    assert marker == {"main_sha": "new-sha-222", "head_sha": "headBLOCK", "outcome": "blocked"}


@pytest.mark.asyncio
async def test_proactive_missing_head_oid_defers() -> None:
    """096 (review fix): a CONFLICTING PR with an empty head_ref_oid is deferred
    (not rebased) — without a reliable head the anti-thrash guard is unsound."""
    gh = _GitHubWithMergeability({"PR_NODE_2": {"mergeable_raw": "CONFLICTING",
                                                "head_ref_oid": ""}})
    state = _proactive_state(gh, {"ITEM_2": _session(2)})
    mock_rebase = AsyncMock()
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="same-sha")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        await check_board(state)
    mock_rebase.assert_not_called()
