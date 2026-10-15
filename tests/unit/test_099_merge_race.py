from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from gql.transport.exceptions import TransportQueryError

from coordinare.graph.nodes.merge_pr import merge_pr
from coordinare.graph.state import initial_state
from coordinare.services.fake_github import FakeGitHubService
from coordinare.services.github import GitHubService, PermanentGitHubError

_HEAD = "a" * 40
_NEW_HEAD = "b" * 40
_PR = "PR_99"
_HEAD_MISMATCH = "Head branch was modified. Review and try the merge again."


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
@pytest.mark.parametrize("message", [_HEAD_MISMATCH, "Head branch is out of date"])
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
