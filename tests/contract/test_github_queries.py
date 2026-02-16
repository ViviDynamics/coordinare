from __future__ import annotations

from coordinare.services import github as github_service


def test_github_contract_queries_exist() -> None:
    assert "PollBoard" in github_service.POLL_BOARD_QUERY
    assert "GetIssueDetails" in github_service.GET_ISSUE_DETAILS_QUERY
    assert "GetPRReviews" in github_service.GET_PR_REVIEWS_QUERY
    assert "CheckMergeability" in github_service.CHECK_MERGEABILITY_QUERY


def test_github_contract_mutations_exist() -> None:
    assert "MoveCard" in github_service.MOVE_CARD_MUTATION
    assert "AddComment" in github_service.ADD_COMMENT_MUTATION
    assert "SquashMerge" in github_service.SQUASH_MERGE_MUTATION
