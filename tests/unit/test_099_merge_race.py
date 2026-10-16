from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gql.transport.exceptions import TransportQueryError

from coordinare.graph.nodes.merge_pr import merge_pr
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services.fake_github import FakeGitHubService
from coordinare.services.github import GitHubService, PermanentGitHubError

_HEAD = "a" * 40
_NEW_HEAD = "b" * 40
_PR = "PR_99"
_HEAD_MISMATCH = "Head branch was modified. Review and try the merge again."
_BEHIND_BASE = "Head branch is out of date"
_MAIN = "c" * 40
_RETRY_ELAPSED = timedelta(minutes=1)


def _state() -> dict:
    state = initial_state()
    state["current_card"] = {"id": "CARD_99", "pr_node_id": _PR, "status": "IN_REVIEW"}
    state["review_wait_head"] = _HEAD
    github = AsyncMock(spec=GitHubService)
    github.check_mergeability.return_value = {"mergeable": True, "head_ref_oid": _HEAD}
    github.squash_merge.return_value = {"merged": True}
    state["github_service"] = github
    return state


@pytest.mark.asyncio
async def test_099_squash_mutation_pins_decision_head() -> None:
    github = GitHubService(token="test", org="acme", project_number=1)
    github._guarded_execute = AsyncMock(return_value={"mergePullRequest": {"pullRequest": {"merged": True}}})
    result = await github.squash_merge(_PR, expected_head_oid=_HEAD)
    assert result["merged"] is True
    query, variables = github._guarded_execute.call_args.args
    assert "expectedHeadOid: $expectedHeadOid" in query
    assert variables == {"pullRequestId": _PR, "expectedHeadOid": _HEAD}


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [_HEAD_MISMATCH])
async def test_099_squash_head_mismatch_is_retryable(message: str) -> None:
    github = GitHubService(token="test", org="acme", project_number=1)
    github._guarded_execute = AsyncMock(side_effect=PermanentGitHubError(message))
    assert await github.squash_merge(_PR, expected_head_oid=_HEAD) == {
        "merged": False, "reason": "head_mismatch",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["UNPROCESSABLE", "STALE_DATA"])
async def test_099_head_mismatch_transport_is_not_retried_as_an_outage(error_type: str) -> None:
    github = GitHubService(token="test", org="acme", project_number=1)
    client = AsyncMock()
    client.execute_async.side_effect = TransportQueryError(
        _HEAD_MISMATCH, errors=[{"type": error_type, "message": _HEAD_MISMATCH}],
    )
    github._client = client
    github._last_token = "test"
    assert await github.squash_merge(_PR, expected_head_oid=_HEAD) == {
        "merged": False, "reason": "head_mismatch",
    }
    client.execute_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_099_merge_passes_approved_head() -> None:
    state = _state()
    result = await merge_pr(state)
    assert result["current_card"]["status"] == "DONE"
    state["github_service"].squash_merge.assert_awaited_once_with(_PR, expected_head_oid=_HEAD)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"merged": False}, {"merged": False, "reason": "head_mismatch"}, {}])
async def test_099_unconfirmed_merge_never_completes_card(result: dict) -> None:
    state = _state()
    state["github_service"].squash_merge.return_value = result
    updated = await merge_pr(state)
    assert updated["phase"] == "monitoring_pr"
    assert updated["current_card"]["status"] == "IN_REVIEW"
    state["github_service"].move_card.assert_not_awaited()
    assert updated.get("commit_summary") is None
    assert updated.get("last_known_main_sha") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mergeable", [True, False])
async def test_099_head_advanced_before_mutation_rechecks_approval(mergeable: bool) -> None:
    state = _state()
    state["github_service"].check_mergeability.return_value.update(head_ref_oid=_NEW_HEAD, mergeable=mergeable)
    updated = await merge_pr(state)
    assert updated["phase"] == "monitoring_pr"
    assert updated["current_card"]["status"] == "IN_REVIEW"
    state["github_service"].squash_merge.assert_not_awaited()


@pytest.mark.asyncio
async def test_099_missing_head_never_sends_merge_mutation() -> None:
    github = GitHubService(token="test", org="acme", project_number=1)
    github.check_mergeability = AsyncMock(return_value={"mergeable": True})
    github._guarded_execute = AsyncMock()
    assert await github.squash_merge(_PR) == {"merged": False, "reason": "missing_head"}
    github._guarded_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_099_legacy_squash_caller_still_pins_observed_head() -> None:
    github = GitHubService(token="test", org="acme", project_number=1)
    github.check_mergeability = AsyncMock(return_value={"mergeable": True, "head_ref_oid": _HEAD})
    github._guarded_execute = AsyncMock(return_value={"mergePullRequest": {"pullRequest": {"merged": True}}})
    assert (await github.squash_merge(_PR))["merged"]
    assert github._guarded_execute.call_args.args[1]["expectedHeadOid"] == _HEAD


@pytest.mark.asyncio
async def test_099_unrelated_merge_rejection_remains_permanent() -> None:
    github = GitHubService(token="test", org="acme", project_number=1)
    github._guarded_execute = AsyncMock(side_effect=PermanentGitHubError("Not authorized"))
    with pytest.raises(PermanentGitHubError, match="Not authorized"):
        await github.squash_merge(_PR, expected_head_oid=_HEAD)


@pytest.mark.asyncio
async def test_099_fake_merge_refuses_an_intervening_push(tmp_path: Path) -> None:
    github = FakeGitHubService(bare_repo_path=tmp_path / "repo.git", work_dir=tmp_path / "work")
    github._prs[_PR] = {"head_ref": "feature"}
    github._rev = AsyncMock(return_value=_NEW_HEAD)
    github._do_local_merge = AsyncMock()
    assert await github.squash_merge(_PR, expected_head_oid=_HEAD) == {
        "merged": False, "reason": "head_mismatch",
    }
    github._do_local_merge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["UNPROCESSABLE", "STALE_DATA"])
async def test_099_out_of_date_head_is_behind_base(error_type: str) -> None:
    github = GitHubService(token="test", org="acme", project_number=1)
    client = AsyncMock()
    client.execute_async.side_effect = TransportQueryError(
        _BEHIND_BASE, errors=[{"type": error_type, "message": _BEHIND_BASE}],
    )
    github._client = client
    github._last_token = "test"
    assert await github.squash_merge(_PR, expected_head_oid=_HEAD) == {
        "merged": False, "reason": "head_behind_base",
    }
    client.execute_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_099_behind_base_wait_is_visible_and_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    github = state["github_service"]
    github.squash_merge.return_value = {"merged": False, "reason": "head_behind_base"}
    github.get_pr_review_context.return_value = {
        "head_oid": _HEAD, "state": "OPEN", "reviews": [
            {"id": "R_99", "author_login": "alice", "state": "APPROVED", "commit_oid": _HEAD},
        ],
    }
    state["human_reviewers"] = ["alice"]
    monkeypatch.setattr("coordinare.graph.nodes.monitor_pr.poll_pr_conversation_feedback", AsyncMock(return_value=[]))
    first = await merge_pr(state)
    assert first["phase"] == "monitoring_pr"
    assert first["review_wait_reason"] == "head_behind_base"
    assert "behind" in first["review_wait_detail"]
    assert first["current_card"]["status"] == "IN_REVIEW"
    (retry,) = state["github_retry_queue"]
    assert retry["retry_at"] > datetime.now(UTC)
    assert (await monitor_pr(state))["review_wait_reason"] == "head_behind_base"
    await merge_pr(state)
    github.squash_merge.assert_awaited_once()
    retry["retry_at"] = datetime.now(UTC) - _RETRY_ELAPSED
    await merge_pr(state)
    (second_retry,) = state["github_retry_queue"]
    assert second_retry["attempt"] == retry["attempt"] + 1
    assert second_retry["retry_at"] - second_retry["deferred_at"] > retry["retry_at"] - retry["deferred_at"] + _RETRY_ELAPSED


@pytest.mark.asyncio
async def test_099_behind_base_new_head_can_merge_during_old_backoff() -> None:
    state = _state()
    github = state["github_service"]
    github.squash_merge.return_value = {"merged": False, "reason": "head_behind_base"}
    await merge_pr(state)
    state["review_wait_head"] = _NEW_HEAD
    github.check_mergeability.return_value["head_ref_oid"] = _NEW_HEAD
    github.squash_merge.return_value = {"merged": True}
    assert (await merge_pr(state))["current_card"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_099_merge_backoff_does_not_delay_actionable_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    github = state["github_service"]
    github.squash_merge.return_value = {"merged": False, "reason": "head_behind_base"}
    await merge_pr(state)
    state["human_reviewers"] = ["alice"]
    github.get_pr_review_context.return_value = {
        "head_oid": _HEAD, "state": "OPEN", "reviews": [
            {"id": "R_99", "author_login": "alice", "state": "CHANGES_REQUESTED", "commit_oid": _HEAD},
        ],
    }
    monkeypatch.setattr("coordinare.graph.nodes.monitor_pr.poll_pr_conversation_feedback", AsyncMock(return_value=[]))
    result = await monitor_pr(state)
    assert result["phase"] == "relay_feedback"
    assert [review["id"] for review in result["pending_reviews"]] == ["R_99"]


@pytest.mark.asyncio
@pytest.mark.parametrize("side_active", [True, False])
async def test_099_behind_base_routes_to_implementer_unless_side_writer_active(side_active: bool) -> None:
    state = _state()
    state["config"] = SimpleNamespace()
    state["workspace_branch"] = "coordinare/CARD_99/work"
    state["documenting_side"] = {"status": "running" if side_active else "done"}
    github = state["github_service"]
    github.squash_merge.return_value = {"merged": False, "reason": "head_behind_base"}
    github.get_branch_head_sha.return_value = _MAIN
    github.get_pr_review_context.return_value = {"state": "OPEN"}
    result = await merge_pr(state)
    if side_active:
        assert result["phase"] == "monitoring_pr"
        assert not result.get("relay_feedback")
    else:
        assert result["phase"] == "dispatching"
        assert result["performer_stage"] == "implementing"
        (feedback,) = result["relay_feedback"]
        assert feedback["kind"] == "branch_divergence"
        assert feedback["expected_head_sha"] == _HEAD
        assert feedback["main_sha"] == _MAIN
        await merge_pr(state)
        assert result["last_rebase_attempt"]["head_sha"] == _HEAD
        assert len(result["relay_feedback"]) == 1
