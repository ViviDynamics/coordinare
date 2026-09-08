"""Exercise GitHub boundary failures without network access."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from coordinare.services.github import GitHubService


@pytest.fixture
def github():
    service = GitHubService(org="acme", project_number=1, token="test-token")
    service._project_name = "widget"
    return service


def rest(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return patch("coordinare.services.github.httpx.AsyncClient", return_value=client)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "body", "expected"), [
    (200, {"state": "closed"}, "closed"),
    (200, {}, "open"),
    (404, {}, "not_found"),
    (503, {}, "api_error"),
])
async def test_issue_state_distinguishes_missing_from_transient(github, status, body, expected):
    def respond(request):
        assert request.url.path == "/repos/acme/widget/issues/12"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(status, json=body)

    with rest(respond):
        assert await github.check_issue_state("acme/widget", 12) == expected


@pytest.mark.asyncio
async def test_issue_state_auth_and_network_failure_are_unknown(github):
    with patch.object(github, "_current_token", AsyncMock(side_effect=RuntimeError("expired"))):
        assert await github.check_issue_state("acme/widget", 12) == "auth_error"

    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    with rest(unavailable):
        assert await github.check_issue_state("acme/widget", 12) == "api_error"


@pytest.mark.asyncio
async def test_comments_paginate_filter_and_normalize_authors(github):
    seen = []

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            assert request.url.params["per_page"] == "100"
            return httpx.Response(200, json=[None, {"id": 1}, {
                "id": 2, "user": {"login": "alice"}, "body": "ready", "created_at": "today",
            }], headers={"link": '<https://api.github.com/page2>; rel="next", <https://api.github.com/page2>; rel="last"'})
        assert str(request.url) == "https://api.github.com/page2"
        return httpx.Response(200, json=[{"id": 3, "user": None}])

    with rest(respond):
        assert await github.get_issue_comments(12, since_id=1) == [
            {"id": 2, "author": "alice", "body": "ready", "created_at": "today"},
            {"id": 3, "author": "", "body": "", "created_at": ""},
        ]
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_comments_discard_partial_result_on_later_page_failure(github):
    responses = iter([
        httpx.Response(200, json=[{"id": 2}], headers={"link": '<https://api.github.com/page2>; rel="next"'}),
        httpx.Response(503),
    ])
    with rest(lambda _: next(responses)):
        assert await github.get_issue_comments(12) == []


@pytest.mark.asyncio
async def test_comments_missing_configuration_auth_or_malformed_data(github):
    github._project_name = ""
    assert await github.get_issue_comments(12) == []
    github._project_name = "widget"
    with patch.object(github, "_current_token", AsyncMock(side_effect=RuntimeError("expired"))):
        assert await github.get_issue_comments(12) == []
    with rest(lambda _: httpx.Response(200, json=[{"id": "invalid"}])):
        assert await github.get_issue_comments(12) == []


@pytest.mark.asyncio
async def test_job_log_redirect_does_not_forward_token_and_returns_tail(github):
    seen = []

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            assert request.headers["authorization"] == "Bearer test-token"
            assert request.url.path == "/repos/acme/widget/actions/jobs/9/logs"
            return httpx.Response(302, headers={"location": "https://storage.example/log"})
        assert "authorization" not in request.headers
        return httpx.Response(200, text="first line\nlast line")

    with rest(respond):
        assert await github.fetch_failed_job_log("acme", "widget", 9, max_chars=9) == "... (log truncated) ...\nlast line"
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [(200, "log"), (302, ""), (404, "")])
async def test_job_log_success_or_unavailable(github, status, expected):
    with rest(lambda _: httpx.Response(status, text="log")):
        assert await github.fetch_failed_job_log("acme", "widget", 9) == expected


@pytest.mark.asyncio
async def test_job_log_invalid_inputs_auth_and_transport_failures(github):
    with patch.object(github, "_current_token", AsyncMock(side_effect=RuntimeError("expired"))) as token:
        assert await github.fetch_failed_job_log("acme", "widget", 0) == ""
        assert await github.fetch_failed_job_log("acme", "widget", 9, max_chars=0) == ""
        token.assert_not_called()
        assert await github.fetch_failed_job_log("acme", "widget", 9) == ""
    with patch.object(github, "_current_token", AsyncMock(return_value="  ")):
        assert await github.fetch_failed_job_log("acme", "widget", 9) == ""

    def unavailable(request):
        raise httpx.ReadTimeout("timeout", request=request)

    with rest(unavailable):
        assert await github.fetch_failed_job_log("acme", "widget", 9) == ""


@pytest.mark.asyncio
async def test_link_to_project_moves_returned_item(github):
    github.project_id = "PROJECT"
    execute = AsyncMock(return_value={"addProjectV2ItemById": {"item": {"id": "ITEM"}}})
    with patch.object(github, "_guarded_execute", execute), patch.object(github, "move_card", AsyncMock()) as move:
        assert await github.link_to_project("ISSUE", "TODO") == "ITEM"
        assert execute.call_args.args[1] == {"projectId": "PROJECT", "contentId": "ISSUE"}
        move.assert_awaited_once_with("ITEM", "TODO")


@pytest.mark.asyncio
async def test_link_to_project_failure_does_not_report_success(github):
    assert await github.link_to_project("ISSUE") is None
    github.project_id = "PROJECT"
    with patch.object(github, "_guarded_execute", AsyncMock(return_value={})):
        assert await github.link_to_project("ISSUE") is None
    with patch.object(github, "_guarded_execute", AsyncMock(side_effect=RuntimeError("denied"))):
        assert await github.link_to_project("ISSUE") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [201, 422])
async def test_request_reviewers_rest_payload(github, status):
    def respond(request):
        assert request.method == "POST"
        assert request.url.path == "/repos/acme/widget/pulls/8/requested_reviewers"
        assert request.content == b'{"reviewers":["alice"]}'
        return httpx.Response(status)

    with rest(respond):
        await github.request_reviewers("acme", "widget", 8, ["alice"])


@pytest.mark.asyncio
async def test_request_reviewers_empty_and_transport_failure(github):
    with patch.object(github, "_current_token", AsyncMock()) as token:
        await github.request_reviewers("acme", "widget", 8, [])
        token.assert_not_called()

    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    with rest(unavailable):
        await github.request_reviewers("acme", "widget", 8, ["alice"])


@pytest.mark.asyncio
async def test_branch_prefix_filters_invalid_nodes_and_limits_matches(github):
    nodes = [None, {"headRefName": None}, {"headRefName": "other"},
             {"number": 1, "url": "one", "headRefName": "coordinare/card/a"},
             {"number": 2, "url": "two", "headRefName": "coordinare/card/b"}]
    execute = AsyncMock(return_value={"repository": {"pullRequests": {"nodes": nodes}}})
    with patch.object(github, "_guarded_execute", execute):
        assert await github.list_prs_by_branch_prefix("acme", "widget", "coordinare/card/", limit=1) == [
            {"number": 1, "url": "one", "head_ref": "coordinare/card/a"},
        ]
        assert execute.call_args.args[1]["states"] == ["OPEN"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, {"repository": {"pullRequests": {"nodes": None}}}])
async def test_branch_prefix_missing_response_is_empty(github, result):
    with patch.object(github, "_guarded_execute", AsyncMock(return_value=result)):
        assert await github.list_prs_by_branch_prefix("acme", "widget", "coordinare/") == []


@pytest.mark.asyncio
async def test_branch_prefix_api_error_is_empty(github):
    with patch.object(github, "_guarded_execute", AsyncMock(side_effect=RuntimeError("denied"))):
        assert await github.list_prs_by_branch_prefix("acme", "widget", "coordinare/") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("obj", "expected"), [({"text": "content"}, "content"), (None, None)])
async def test_file_content_preserves_requested_ref(github, obj, expected):
    execute = AsyncMock(return_value={"repository": {"object": obj}})
    with patch.object(github, "_guarded_execute", execute):
        assert await github.get_file_content("acme", "widget", "README.md", "feature") == expected
    assert execute.call_args.args[1] == {"owner": "acme", "repo": "widget", "expression": "feature:README.md"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("obj", "expected"), [({"oid": "blob-sha"}, "blob-sha"), (None, None)])
async def test_file_blob_sha_preserves_requested_ref(github, obj, expected):
    execute = AsyncMock(return_value={"repository": {"object": obj}})
    with patch.object(github, "_guarded_execute", execute):
        assert await github.get_file_blob_sha("acme", "widget", "README.md", "feature") == expected
    assert execute.call_args.args[1] == {"owner": "acme", "repo": "widget", "expression": "feature:README.md"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("endpoint", "expected_path"), [
    ("https://github.example/api/GraphQL/", "/api/v3/repos/acme/widget/issues/12"),
    ("https://github.example/custom/graphql", "/custom/repos/acme/widget/issues/12"),
])
async def test_issue_rest_api_uses_configured_enterprise_endpoint(github, endpoint, expected_path):
    github._endpoint = endpoint

    def respond(request):
        assert request.url.host == "github.example"
        assert request.url.path == expected_path
        return httpx.Response(200, json={"state": "closed"})

    with rest(respond):
        assert await github.check_issue_state("acme/widget", 12) == "closed"
