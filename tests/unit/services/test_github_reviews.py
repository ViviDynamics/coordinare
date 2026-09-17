"""128: contract tests for the extended review context + request_reviews."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.services.github import GitHubService


class _FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses

    async def execute(self, query: object, variable_values: dict[str, Any]) -> dict[str, Any]:
        _ = (query, variable_values)
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class _TestGitHubService(GitHubService):
    def __init__(self, responses: list[Any]) -> None:
        super().__init__(token="tok", org="acme", project_number=1)
        self._responses = responses

    def _build_client(self, token: str = ""):
        return _FakeClient(self._responses)


def _reviews_response(review_nodes, thread_nodes, head="HEAD", decision="CHANGES_REQUESTED"):
    return {
        "node": {
            "reviews": {"nodes": review_nodes},
            "reviewThreads": {"nodes": thread_nodes},
            "headRefOid": head,
            "reviewDecision": decision,
        },
    }


@pytest.mark.asyncio
async def test_context_parses_commit_oid_threads_head_decision() -> None:
    svc = _TestGitHubService([
        _reviews_response(
            review_nodes=[{
                "id": "RVW_1",
                "author": {"login": "jason", "__typename": "User"},
                "state": "CHANGES_REQUESTED",
                "body": "fix it",
                "submittedAt": "2026-05-18T14:08:38Z",
                "commit": {"oid": "oldsha"},
            }],
            thread_nodes=[
                {"id": "T1", "isResolved": True,
                 "comments": {"nodes": [{"pullRequestReview": {"id": "RVW_1"}}]}},
                {"id": "T2", "isResolved": False,
                 "comments": {"nodes": [{"pullRequestReview": {"id": "RVW_1"}}]}},
            ],
            head="newsha",
        ),
    ])
    ctx = await svc.get_pr_review_context("PR_1")
    assert ctx["head_oid"] == "newsha"
    assert ctx["review_decision"] == "CHANGES_REQUESTED"
    assert ctx["reviews"][0]["commit_oid"] == "oldsha"
    threads = {t["id"]: t for t in ctx["review_threads"]}
    assert threads["T1"]["is_resolved"] is True and threads["T1"]["review_id"] == "RVW_1"
    assert threads["T2"]["is_resolved"] is False


@pytest.mark.asyncio
async def test_context_safe_on_missing_fields() -> None:
    # older-shape response: no commit, no reviewThreads, no headRefOid
    svc = _TestGitHubService([
        {"node": {"reviews": {"nodes": [{
            "id": "RVW_1", "author": {"login": "jason"}, "state": "CHANGES_REQUESTED",
            "body": "", "submittedAt": "2026-05-18T14:08:38Z",
        }]}}},
    ])
    ctx = await svc.get_pr_review_context("PR_1")
    assert ctx["reviews"][0]["commit_oid"] == ""
    assert ctx["review_threads"] == []
    assert ctx["head_oid"] == ""


@pytest.mark.asyncio
async def test_context_page_cap_logs(caplog) -> None:
    threads = [
        {"id": f"T{i}", "isResolved": True, "comments": {"nodes": []}}
        for i in range(100)
    ]
    svc = _TestGitHubService([_reviews_response([], threads)])
    ctx = await svc.get_pr_review_context("PR_1")
    assert len(ctx["review_threads"]) == 100
    # unattributed threads carry review_id None (caller treats as body-only/safe)
    assert all(t["review_id"] is None for t in ctx["review_threads"])


@pytest.mark.asyncio
async def test_get_pr_reviews_still_returns_list_with_commit_oid() -> None:
    svc = _TestGitHubService([
        _reviews_response([{
            "id": "RVW_1", "author": {"login": "jason"}, "state": "APPROVED",
            "body": "ok", "submittedAt": "2026-02-25T12:00:00Z", "commit": {"oid": "c1"},
        }], []),
    ])
    reviews = await svc.get_pr_reviews("PR_1")
    assert reviews[0]["author_login"] == "jason" and reviews[0]["commit_oid"] == "c1"


@pytest.mark.asyncio
async def test_request_reviews_resolves_and_requests() -> None:
    svc = _TestGitHubService([
        {"user": {"id": "U_jason"}},                       # GET_USER_ID_QUERY
        {"requestReviews": {"pullRequest": {"id": "PR_1"}}},  # REQUEST_REVIEWS_MUTATION
    ])
    res = await svc.request_reviews("PR_1", ["jason"])
    assert res["requested"] is True and res["count"] == 1


@pytest.mark.asyncio
async def test_request_reviews_no_resolvable_reviewers() -> None:
    svc = _TestGitHubService([{"user": None}])  # login doesn't resolve
    res = await svc.request_reviews("PR_1", ["ghost"])
    assert res["requested"] is False


@pytest.mark.asyncio
async def test_request_reviews_failsafe_on_api_error() -> None:
    svc = _TestGitHubService([
        {"user": {"id": "U_jason"}},
        RuntimeError("boom"),  # mutation raises
    ])
    res = await svc.request_reviews("PR_1", ["jason"])  # must NOT raise
    assert res["requested"] is False and "boom" in res["reason"]
