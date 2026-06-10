from __future__ import annotations

from coordinare.services import github as github_service


def test_github_contract_queries_exist() -> None:
    assert "PollBoard" in github_service.POLL_BOARD_QUERY
    assert "GetIssueDetails" in github_service.GET_ISSUE_DETAILS_QUERY
    assert "GetPRReviews" in github_service.GET_PR_REVIEWS_QUERY
    assert "CheckMergeability" in github_service.CHECK_MERGEABILITY_QUERY


def test_get_pr_reviews_query_fetches_most_recent_reviews() -> None:
    """The reviews connection must page from the END (most recent), not the start.

    GitHub returns a PR's reviews in chronological-ascending order. A bot that
    posts a COMMENTED review every cycle (vivi-coordinare) can push the review
    count past a single page; with ``reviews(first: N)`` the human APPROVED
    review — always the newest — falls off the page and the closer never sees
    the approval (live incident: PR #154 had 62 reviews, approval at index 61,
    invisible behind 61 bot self-comments). ``reviews(last: N)`` keeps the
    newest N in view so the approval is always fetched.
    """
    query = github_service.GET_PR_REVIEWS_QUERY
    # Find the top-level reviews(...) connection (not the nested comments(...)).
    assert "reviews(last:" in query.replace(" ", ""), (
        "GET_PR_REVIEWS_QUERY must fetch the most recent reviews via "
        "reviews(last: N); reviews(first: N) drops the newest reviews "
        "(including the human approval) on PRs with more than one page."
    )
    assert "reviews(first:" not in query.replace(" ", "")


def test_github_contract_mutations_exist() -> None:
    assert "MoveCard" in github_service.MOVE_CARD_MUTATION
    assert "AddComment" in github_service.ADD_COMMENT_MUTATION
    assert "SquashMerge" in github_service.SQUASH_MERGE_MUTATION
