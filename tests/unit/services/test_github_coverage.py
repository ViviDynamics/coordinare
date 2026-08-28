"""Coverage tests for GitHubService edge-case and advocate-method paths."""
from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from coordinare.services.github import (
    GitHubService,
    PermanentGitHubError,
    RateLimitedGitHubError,
    TransientGitHubError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal fake GQL client that pops from a response queue."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)

    def execute(self, _query: Any, variable_values: dict[str, Any]) -> Any:
        return self._responses.pop(0)


class _AsyncFakeClient:
    """Fake GQL client that exposes execute_async (covering the hasattr branch)."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)

    async def execute_async(self, _query: Any, variable_values: dict[str, Any]) -> Any:
        return self._responses.pop(0)


def _svc(*responses: Any) -> GitHubService:
    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc._client = _FakeClient(list(responses))
    svc._last_token = "tok"
    return svc


def _async_svc(*responses: Any) -> GitHubService:
    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc._client = _AsyncFakeClient(list(responses))
    svc._last_token = "tok"
    return svc


def _initialized_svc(*extra_responses: Any) -> GitHubService:
    """Return a service already initialized (project_id + field_cache set)."""
    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc.project_id = "PVT_1"
    svc.field_cache = {
        "status_field_id": "FLD_1",
        "status_option_ids": {
            "todo": "OPT_TODO",
            "blocked": "OPT_BLOCKED",
            "in progress": "OPT_IP",
            "in review": "OPT_IR",
            "done": "OPT_DONE",
        },
    }
    svc._client = _FakeClient(list(extra_responses))
    svc._last_token = "tok"
    return svc


# ---------------------------------------------------------------------------
# _build_client
# ---------------------------------------------------------------------------


def test_build_client_returns_gql_client() -> None:
    """_build_client() produces a usable GQL Client object."""
    svc = GitHubService(token="tok", org="acme", project_number=1)
    client = svc._build_client("tok")
    # Just verify it is a gql Client (duck-type: has execute / execute_async)
    assert hasattr(client, "execute") or hasattr(client, "execute_async")


# ---------------------------------------------------------------------------
# _execute — execute_async path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_uses_execute_async_when_available() -> None:
    """When client has execute_async, that method is used instead of execute."""
    svc = _async_svc({"result": True})
    result = await svc._execute("query { __typename }", {})
    assert result == {"result": True}


# ---------------------------------------------------------------------------
# _execute — Retry-After header with invalid value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_429_with_invalid_retry_after_defaults_to_60(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric Retry-After falls back to 60 s."""
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("coordinare.services.github.asyncio.sleep", fake_sleep)

    class _InvalidRetryClient:
        def execute(self, _q: Any, variable_values: dict[str, Any]) -> Any:
            return self._raise()

        async def _raise(self) -> Any:
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(
                    url="https://api.github.com/graphql",
                    method="POST",
                    headers={},
                    real_url="https://api.github.com/graphql",
                ),
                history=(),
                status=429,
                headers={"Retry-After": "not-a-number"},
            )

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc._client = _InvalidRetryClient()
    svc._last_token = "tok"

    with pytest.raises(RateLimitedGitHubError) as exc_info:
        await svc._execute("query { __typename }", {})

    assert exc_info.value.retry_after == 60.0
    assert slept == [60.0]


# ---------------------------------------------------------------------------
# _execute — 4xx raises PermanentGitHubError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_4xx_raises_permanent_error() -> None:
    """Non-429, non-5xx status → PermanentGitHubError."""

    class _ClientError4xx:
        def execute(self, _q: Any, variable_values: dict[str, Any]) -> Any:
            return self._raise()

        async def _raise(self) -> Any:
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(
                    url="https://api.github.com/graphql",
                    method="POST",
                    headers={},
                    real_url="https://api.github.com/graphql",
                ),
                history=(),
                status=401,
            )

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc._client = _ClientError4xx()
    svc._last_token = "tok"

    with pytest.raises(PermanentGitHubError):
        await svc._execute("query { __typename }", {})


# ---------------------------------------------------------------------------
# _execute — OSError / TimeoutError → TransientGitHubError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_oserror_raises_transient() -> None:
    """OSError from the underlying transport raises TransientGitHubError."""

    class _OSErrorClient:
        def execute(self, _q: Any, variable_values: dict[str, Any]) -> Any:
            return self._raise()

        async def _raise(self) -> Any:
            raise OSError("network unreachable")

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc._client = _OSErrorClient()
    svc._last_token = "tok"

    with pytest.raises(TransientGitHubError):
        await svc._execute("query { __typename }", {})


# ---------------------------------------------------------------------------
# _execute — ValueError → PermanentGitHubError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_value_error_raises_permanent() -> None:
    """ValueError from the transport raises PermanentGitHubError."""

    class _ValueErrorClient:
        def execute(self, _q: Any, variable_values: dict[str, Any]) -> Any:
            return self._raise()

        async def _raise(self) -> Any:
            raise ValueError("bad query")

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc._client = _ValueErrorClient()
    svc._last_token = "tok"

    with pytest.raises(PermanentGitHubError):
        await svc._execute("query { __typename }", {})


# ---------------------------------------------------------------------------
# initialize — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_raises_when_project_not_found() -> None:
    svc = _svc({"repositoryOwner": {"projectV2": None}})
    with pytest.raises(ValueError, match="Project #1 not found for owner"):
        await svc.initialize()


@pytest.mark.asyncio
async def test_initialize_raises_when_fields_malformed() -> None:
    svc = _svc(
        {"repositoryOwner": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
        {"node": {"fields": {"nodes": "not-a-list"}}},
    )
    with pytest.raises(ValueError, match="malformed"):
        await svc.initialize()


@pytest.mark.asyncio
async def test_initialize_handles_options_not_a_list() -> None:
    """When a Status field's options is not a list, it is skipped gracefully."""
    svc = _svc(
        {"repositoryOwner": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
        {
            "node": {
                "fields": {
                    "nodes": [
                        {
                            "id": "FLD_1",
                            "name": "Status",
                            "options": "not-a-list",  # triggers 449->442 false branch
                        },
                        {
                            "id": "FLD_1",
                            "name": "Status",
                            "options": [{"id": "OPT_1", "name": "Todo"}],
                        },
                    ]
                }
            }
        },
    )
    await svc.initialize()
    assert svc.field_cache["status_option_ids"]["todo"] == "OPT_1"


@pytest.mark.asyncio
async def test_initialize_raises_when_status_field_missing() -> None:
    """Fields list with no Status field raises ValueError."""
    svc = _svc(
        {"repositoryOwner": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
        {"node": {"fields": {"nodes": [{"id": "FLD_X", "name": "Priority", "options": []}]}}},
    )
    with pytest.raises(ValueError, match="Status field not found"):
        await svc.initialize()


@pytest.mark.asyncio
async def test_initialize_skips_non_dict_nodes_and_options() -> None:
    """Non-dict nodes and options are silently skipped."""
    svc = _svc(
        {"repositoryOwner": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
        {
            "node": {
                "fields": {
                    "nodes": [
                        "not-a-dict",  # skipped
                        {
                            "id": "FLD_1",
                            "name": "Status",
                            "options": [
                                "not-a-dict",           # skipped
                                {"id": "", "name": "Empty"},  # empty id skipped
                                {"id": "OPT_1", "name": "Todo"},
                            ],
                        },
                    ]
                }
            }
        },
    )
    await svc.initialize()
    assert svc.field_cache["status_option_ids"]["todo"] == "OPT_1"


# ---------------------------------------------------------------------------
# poll_board — edge cases
# ---------------------------------------------------------------------------


def _poll_response(items: list[Any]) -> dict[str, Any]:
    return {"node": {"items": {"nodes": items}}}


@pytest.mark.asyncio
async def test_poll_board_malformed_items_returns_empty_snapshot() -> None:
    """Non-list items node yields empty snapshot buckets but full result shape."""
    svc = _initialized_svc({"node": {"items": {"nodes": "bad"}}})
    result = await svc.poll_board()
    assert result["snapshot"] == {
        "BACKLOG": [], "TODO": [], "BLOCKED": [], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": []
    }
    assert result["titles"] == {}
    assert result["issue_numbers"] == {}


@pytest.mark.asyncio
async def test_poll_board_item_without_id_is_skipped() -> None:
    """Item with no id (or empty string id) is skipped."""
    svc = _initialized_svc(_poll_response([{"id": "", "fieldValues": {"nodes": []}}]))
    result = await svc.poll_board()
    assert result["snapshot"] == {
        "BACKLOG": [], "TODO": [], "BLOCKED": [], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": []
    }


@pytest.mark.asyncio
async def test_poll_board_non_dict_item_is_skipped() -> None:
    """Non-dict entries in items list are skipped."""
    svc = _initialized_svc(_poll_response(["not-a-dict"]))
    result = await svc.poll_board()
    assert result["snapshot"]["TODO"] == []


@pytest.mark.asyncio
async def test_poll_board_field_values_not_a_list() -> None:
    """When fieldValues.nodes is not a list, item has unknown status and is skipped."""
    svc = _initialized_svc(
        _poll_response([{"id": "ITEM_1", "fieldValues": {"nodes": "bad"}, "content": {}}])
    )
    result = await svc.poll_board()
    # Unknown status — not in any column
    assert "ITEM_1" not in result["snapshot"]["TODO"]


@pytest.mark.asyncio
async def test_poll_board_non_dict_field_value_is_skipped() -> None:
    """Non-dict entries in fieldValues.nodes are skipped."""
    svc = _initialized_svc(
        _poll_response([
            {
                "id": "ITEM_1",
                "fieldValues": {"nodes": ["not-a-dict", {"name": "Done"}]},
                "content": {},
            }
        ])
    )
    result = await svc.poll_board()
    assert "ITEM_1" in result["snapshot"]["DONE"]


@pytest.mark.asyncio
async def test_poll_board_all_status_values() -> None:
    """All status name variants map to the correct snapshot bucket."""
    items = [
        {"id": "I_BLOCKED", "fieldValues": {"nodes": [{"name": "Blocked"}]}, "content": {}},
        {"id": "I_IP", "fieldValues": {"nodes": [{"name": "In Progress"}]}, "content": {}},
        {"id": "I_IR", "fieldValues": {"nodes": [{"name": "In Review"}]}, "content": {}},
        {"id": "I_DONE", "fieldValues": {"nodes": [{"name": "Done"}]}, "content": {}},
    ]
    svc = _initialized_svc(_poll_response(items))
    result = await svc.poll_board()
    assert "I_BLOCKED" in result["snapshot"]["BLOCKED"]
    assert "I_IP" in result["snapshot"]["IN_PROGRESS"]
    assert "I_IR" in result["snapshot"]["IN_REVIEW"]
    assert "I_DONE" in result["snapshot"]["DONE"]


@pytest.mark.asyncio
async def test_poll_board_unknown_status_name_is_skipped() -> None:
    """A field value with an unrecognized name is skipped (not defaulted to TODO)."""
    svc = _initialized_svc(
        _poll_response([
            {
                "id": "ITEM_1",
                "fieldValues": {"nodes": [{"name": "Custom Column"}]},
                "content": {},
            }
        ])
    )
    result = await svc.poll_board()
    assert "ITEM_1" not in result["snapshot"]["TODO"]


@pytest.mark.asyncio
async def test_poll_board_backlog_is_separate_from_todo() -> None:
    """BACKLOG items must NOT be treated as TODO."""
    svc = _initialized_svc(
        _poll_response([
            {
                "id": "ITEM_1",
                "fieldValues": {"nodes": [{"name": "BACKLOG"}]},
                "content": {"title": "Backlog item"},
            },
            {
                "id": "ITEM_2",
                "fieldValues": {"nodes": [{"name": "Todo"}]},
                "content": {"title": "Todo item"},
            },
        ])
    )
    result = await svc.poll_board()
    assert "ITEM_1" in result["snapshot"]["BACKLOG"]
    assert "ITEM_1" not in result["snapshot"]["TODO"]
    assert "ITEM_2" in result["snapshot"]["TODO"]


@pytest.mark.asyncio
async def test_poll_board_content_not_dict() -> None:
    """Non-dict content is gracefully ignored; item has unknown status and is skipped."""
    svc = _initialized_svc(
        _poll_response([{"id": "ITEM_1", "fieldValues": {"nodes": []}, "content": "bad"}])
    )
    result = await svc.poll_board()
    assert "ITEM_1" not in result["snapshot"]["TODO"]
    assert "ITEM_1" not in result.get("titles", {})


@pytest.mark.asyncio
async def test_poll_board_labels_not_dict() -> None:
    """Non-dict labels are gracefully ignored."""
    svc = _initialized_svc(
        _poll_response([
            {
                "id": "ITEM_1",
                "fieldValues": {"nodes": []},
                "content": {"title": "T", "body": "", "number": 1, "labels": "bad"},
            }
        ])
    )
    result = await svc.poll_board()
    assert result["item_labels"].get("ITEM_1") is None


# ---------------------------------------------------------------------------
# move_card — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_card_raises_when_status_options_cache_malformed() -> None:
    svc = _initialized_svc()
    svc.field_cache["status_option_ids"] = "not-a-dict"  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="malformed"):
        await svc.move_card("ITEM_1", "TODO")


@pytest.mark.asyncio
async def test_move_card_raises_on_unknown_status() -> None:
    svc = _initialized_svc()
    with pytest.raises(ValueError, match="Unknown status"):
        await svc.move_card("ITEM_1", "NONEXISTENT")


# ---------------------------------------------------------------------------
# get_pr_reviews — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pr_reviews_returns_empty_on_non_list_nodes() -> None:
    svc = _initialized_svc({"node": {"reviews": {"nodes": "bad"}}})
    result = await svc.get_pr_reviews("PR_1")
    assert result == []


@pytest.mark.asyncio
async def test_get_pr_reviews_skips_non_dict_reviews() -> None:
    svc = _initialized_svc({"node": {"reviews": {"nodes": ["not-a-dict"]}}})
    result = await svc.get_pr_reviews("PR_1")
    assert result == []


# ---------------------------------------------------------------------------
# check_mergeability — non-dict node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_mergeability_non_dict_node_returns_missing_pr() -> None:
    svc = _initialized_svc({"node": "bad"})
    result = await svc.check_mergeability("PR_1")
    assert result == {"mergeable": False, "reason": "missing_pr"}


# ---------------------------------------------------------------------------
# squash_merge — non-dict pullRequest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_squash_merge_non_dict_returns_not_merged() -> None:
    svc = _initialized_svc({"mergePullRequest": {"pullRequest": "bad"}})
    result = await svc.squash_merge("PR_1")
    assert result == {"merged": False}


# ---------------------------------------------------------------------------
# add_comment — non-dict node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_comment_non_dict_returns_empty() -> None:
    svc = _initialized_svc({"addComment": {"commentEdge": {"node": "bad"}}})
    result = await svc.add_comment("SUBJ_1", "hello")
    assert result == {}


# ---------------------------------------------------------------------------
# Advocate methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_repository_id_raises_when_not_found() -> None:
    svc = _initialized_svc({"repository": {"id": None}})
    with pytest.raises(ValueError, match="Repository not found"):
        await svc.get_repository_id("acme", "repo")


@pytest.mark.asyncio
async def test_get_repository_id_success() -> None:
    svc = _initialized_svc({"repository": {"id": "REPO_1"}})
    result = await svc.get_repository_id("acme", "repo")
    assert result == "REPO_1"


@pytest.mark.asyncio
async def test_get_label_ids_malformed_nodes_returns_empty() -> None:
    svc = _initialized_svc({"repository": {"labels": {"nodes": "bad"}}})
    result = await svc.get_label_ids("acme", "repo")
    assert result == {}


@pytest.mark.asyncio
async def test_get_label_ids_success() -> None:
    svc = _initialized_svc(
        {"repository": {"labels": {"nodes": [{"name": "bug", "id": "LBL_1"}]}}}
    )
    result = await svc.get_label_ids("acme", "repo")
    assert result == {"bug": "LBL_1"}


@pytest.mark.asyncio
async def test_ensure_labels_exist_creates_missing_labels() -> None:
    """ensure_labels_exist creates any label that doesn't already exist."""
    svc = _initialized_svc(
        {"repository": {"id": "REPO_1"}},                           # get_repository_id
        {"repository": {"labels": {"nodes": []}}},                  # get_label_ids (none exist)
        {"createLabel": {"label": {"id": "LBL_H", "name": "handled"}}},  # create handled
        {"createLabel": {"label": {"id": "LBL_E", "name": "escalated"}}},  # create escalated
    )
    result = await svc.ensure_labels_exist("acme", "repo", "handled", "escalated")
    assert result["handled"] == "LBL_H"
    assert result["escalated"] == "LBL_E"


@pytest.mark.asyncio
async def test_ensure_labels_exist_skips_existing_labels() -> None:
    """ensure_labels_exist does not create labels that already exist."""
    svc = _initialized_svc(
        {"repository": {"id": "REPO_1"}},
        {"repository": {"labels": {"nodes": [
            {"name": "handled", "id": "LBL_EXISTING_H"},
            {"name": "escalated", "id": "LBL_EXISTING_E"},
        ]}}},
        # No createLabel calls expected
    )
    result = await svc.ensure_labels_exist("acme", "repo", "handled", "escalated")
    assert result["handled"] == "LBL_EXISTING_H"
    assert result["escalated"] == "LBL_EXISTING_E"


@pytest.mark.asyncio
async def test_ensure_labels_exist_handles_create_with_no_id() -> None:
    """createLabel response without an id field is silently ignored."""
    svc = _initialized_svc(
        {"repository": {"id": "REPO_1"}},
        {"repository": {"labels": {"nodes": []}}},
        {"createLabel": {"label": {}}},   # no "id" — should be ignored
        {"createLabel": {"label": {}}},   # same for second
    )
    result = await svc.ensure_labels_exist("acme", "repo", "handled", "escalated")
    # Labels not added to map since create returned no id
    assert "handled" not in result
    assert "escalated" not in result


@pytest.mark.asyncio
async def test_add_labels() -> None:
    svc = _initialized_svc({"addLabelsToLabelable": {"labelable": {"id": "ISSUE_1"}}})
    await svc.add_labels("ISSUE_1", ["LBL_1", "LBL_2"])  # should not raise


@pytest.mark.asyncio
async def test_list_open_issues_returns_list() -> None:
    svc = _initialized_svc(
        {"repository": {"issues": {"nodes": [{"id": "ISS_1", "title": "Bug"}]}}}
    )
    result = await svc.list_open_issues("acme", "repo")
    assert len(result) == 1
    assert result[0]["id"] == "ISS_1"


@pytest.mark.asyncio
async def test_list_open_issues_malformed_returns_empty() -> None:
    svc = _initialized_svc({"repository": {"issues": {"nodes": "bad"}}})
    result = await svc.list_open_issues("acme", "repo")
    assert result == []


@pytest.mark.asyncio
async def test_get_file_content_returns_text() -> None:
    svc = _initialized_svc(
        {"repository": {"object": {"text": "file contents here"}}}
    )
    result = await svc.get_file_content("acme", "repo", "README.md")
    assert result == "file contents here"


@pytest.mark.asyncio
async def test_get_file_content_returns_none_when_missing() -> None:
    svc = _initialized_svc({"repository": {"object": None}})
    result = await svc.get_file_content("acme", "repo", "missing.md")
    assert result is None


@pytest.mark.asyncio
async def test_poll_board_populates_issue_url_when_present() -> None:
    """Line 567: issue_urls[item_id] is set when content.url is non-empty."""
    item = {
        "id": "ITEM_U",
        "fieldValues": {"nodes": [{"name": "Todo"}]},
        "content": {
            "title": "Card with URL",
            "body": "",
            "number": 7,
            "id": "ISSUE_NODE_1",
            "url": "https://github.com/acme/repo/issues/7",
            "labels": {"nodes": []},
        },
    }
    svc = _initialized_svc(_poll_response([item]))
    result = await svc.poll_board()
    assert result.get("issue_urls", {}).get("ITEM_U") == "https://github.com/acme/repo/issues/7"


# ---------------------------------------------------------------------------
# 042 — find_pr_for_issue (recovery for missing pr_node_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_pr_for_issue_returns_open_pr() -> None:
    """find_pr_for_issue returns the open PR linked to the issue via Closes #N."""
    svc = _initialized_svc({
        "node": {
            "closedByPullRequestsReferences": {
                "nodes": [
                    {"id": "PR_kwDO_1", "url": "https://github.com/acme/repo/pull/42", "state": "OPEN"},
                ]
            }
        }
    })
    result = await svc.find_pr_for_issue("I_kwDO_issue")
    assert result == {
        "pr_node_id": "PR_kwDO_1",
        "pr_url": "https://github.com/acme/repo/pull/42",
    }


@pytest.mark.asyncio
async def test_find_pr_for_issue_skips_closed_prs() -> None:
    """Closed PRs must not be returned — only OPEN PRs are recovery targets."""
    svc = _initialized_svc({
        "node": {
            "closedByPullRequestsReferences": {
                "nodes": [
                    {"id": "PR_old", "url": "https://github.com/acme/repo/pull/1", "state": "MERGED"},
                    {"id": "PR_new", "url": "https://github.com/acme/repo/pull/2", "state": "OPEN"},
                ]
            }
        }
    })
    result = await svc.find_pr_for_issue("I_kwDO_issue")
    assert result == {"pr_node_id": "PR_new", "pr_url": "https://github.com/acme/repo/pull/2"}


@pytest.mark.asyncio
async def test_find_pr_for_issue_returns_none_when_no_open_pr() -> None:
    svc = _initialized_svc({
        "node": {
            "closedByPullRequestsReferences": {
                "nodes": [
                    {"id": "PR_old", "url": "https://github.com/acme/repo/pull/1", "state": "MERGED"},
                ]
            }
        }
    })
    assert await svc.find_pr_for_issue("I_kwDO_issue") is None


@pytest.mark.asyncio
async def test_find_pr_for_issue_returns_none_when_no_references() -> None:
    """Issue with no associated PRs → returns None, never raises."""
    svc = _initialized_svc({"node": {"closedByPullRequestsReferences": {"nodes": []}}})
    assert await svc.find_pr_for_issue("I_kwDO_issue") is None


@pytest.mark.asyncio
async def test_find_pr_for_issue_returns_none_when_node_missing() -> None:
    svc = _initialized_svc({"node": None})
    assert await svc.find_pr_for_issue("I_kwDO_issue") is None


@pytest.mark.asyncio
async def test_find_pr_for_issue_returns_none_for_empty_issue_id() -> None:
    """Defensive: empty issue_node_id short-circuits without an API call —
    avoids sending malformed queries during recovery."""
    svc = _initialized_svc()  # no responses queued; would fail if called
    assert await svc.find_pr_for_issue("") is None


@pytest.mark.asyncio
async def test_find_pr_for_issue_swallows_query_errors() -> None:
    """Recovery is best-effort — a transient GraphQL error must return None,
    not propagate, so the caller can decide how to handle absence."""
    class _Failing:
        def execute(self, _q, variable_values):
            raise RuntimeError("graphql transient")
    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc.project_id = "PVT_1"
    svc._client = _Failing()
    svc._last_token = "tok"
    assert await svc.find_pr_for_issue("I_kwDO_issue") is None


# ---------------------------------------------------------------------------
# 053 — count_closed_prs_for_issue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_closed_prs_for_issue_counts_closed_only() -> None:
    svc = _initialized_svc({
        "node": {
            "closedByPullRequestsReferences": {
                "nodes": [
                    {"state": "OPEN"},
                    {"state": "CLOSED"},
                    {"state": "MERGED"},
                    {"state": "CLOSED"},
                ]
            }
        }
    })
    assert await svc.count_closed_prs_for_issue("I_kwDO_issue") == 2


@pytest.mark.asyncio
async def test_count_closed_prs_for_issue_empty_issue_id_returns_zero() -> None:
    svc = _initialized_svc()  # no query should run
    assert await svc.count_closed_prs_for_issue("") == 0


@pytest.mark.asyncio
async def test_count_closed_prs_for_issue_swallows_query_errors() -> None:
    class _Failing:
        def execute(self, _q, variable_values):
            raise RuntimeError("graphql transient")
    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc.project_id = "PVT_1"
    svc._client = _Failing()
    svc._last_token = "tok"
    assert await svc.count_closed_prs_for_issue("I_kwDO_issue") == 0


# ---------------------------------------------------------------------------
# 042 — GraphQL error → permanent vs transient classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unprocessable_graphql_error_classified_as_permanent() -> None:
    """042 regression: a GraphQL UNPROCESSABLE error (e.g. branch ruleset
    rejection on mergePullRequest) is an application-layer issue.  It
    must be raised as PermanentGitHubError so callers can route to
    blocked rather than retry forever."""
    from gql.transport.exceptions import TransportQueryError

    class _ErroringClient:
        def execute(self, _q, variable_values):
            raise TransportQueryError(
                "merge rejected",
                errors=[{
                    "type": "UNPROCESSABLE",
                    "path": ["mergePullRequest"],
                    "message": "You're not authorized to push to this branch.",
                }],
            )

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc.project_id = "PVT_1"
    svc._client = _ErroringClient()
    svc._last_token = "tok"

    with pytest.raises(PermanentGitHubError):
        await svc._execute("query { x }", {})


@pytest.mark.asyncio
async def test_not_found_graphql_error_classified_as_permanent() -> None:
    """042: NOT_FOUND errors (e.g. stale pr_node_id) won't resolve on
    retry — classify as permanent."""
    from gql.transport.exceptions import TransportQueryError

    class _ErroringClient:
        def execute(self, _q, variable_values):
            raise TransportQueryError(
                "not found",
                errors=[{
                    "type": "NOT_FOUND",
                    "message": "Could not resolve to a node",
                }],
            )

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc.project_id = "PVT_1"
    svc._client = _ErroringClient()
    svc._last_token = "tok"

    with pytest.raises(PermanentGitHubError):
        await svc._execute("query { x }", {})


@pytest.mark.asyncio
async def test_forbidden_graphql_error_classified_as_permanent() -> None:
    from gql.transport.exceptions import TransportQueryError

    class _ErroringClient:
        def execute(self, _q, variable_values):
            raise TransportQueryError(
                "forbidden",
                errors=[{"type": "FORBIDDEN", "message": "missing scope"}],
            )

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc.project_id = "PVT_1"
    svc._client = _ErroringClient()
    svc._last_token = "tok"

    with pytest.raises(PermanentGitHubError):
        await svc._execute("query { x }", {})


@pytest.mark.asyncio
async def test_unknown_graphql_error_type_classified_as_transient() -> None:
    """042: GraphQL errors without a recognised permanent type (or with
    server-side error indicators) should be transient — retry might help."""
    from gql.transport.exceptions import TransportQueryError

    class _ErroringClient:
        def execute(self, _q, variable_values):
            raise TransportQueryError(
                "transient",
                errors=[{"type": "INTERNAL", "message": "server flaked"}],
            )

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc.project_id = "PVT_1"
    svc._client = _ErroringClient()
    svc._last_token = "tok"

    with pytest.raises(TransientGitHubError):
        await svc._execute("query { x }", {})


@pytest.mark.asyncio
async def test_permanent_error_does_not_trip_circuit_breaker() -> None:
    """042 end-to-end regression: a permanent GraphQL error must propagate
    through the breaker without counting as a failure — otherwise repeated
    permanent errors (e.g., the same blocked PR being re-attempted) starve
    every other GitHub call.  This is the actual symptom we hit on PR #88."""
    from gql.transport.exceptions import TransportQueryError

    from coordinare.resilience import CircuitBreaker, CircuitState

    cb = CircuitBreaker(
        service_name="github",
        failure_threshold=2,
        recovery_window=60.0,
        observation_window=300.0,
    )

    class _ErroringClient:
        def execute(self, _q, variable_values):
            raise TransportQueryError(
                "merge rejected",
                errors=[{"type": "UNPROCESSABLE", "message": "no auth"}],
            )

    svc = GitHubService(
        token="tok", org="acme", project_number=1, circuit_breaker=cb,
    )
    svc.project_id = "PVT_1"
    svc._client = _ErroringClient()
    svc._last_token = "tok"

    # Fire 5 permanent errors — would normally trip a 2-failure breaker
    for _ in range(5):
        with pytest.raises(PermanentGitHubError):
            await svc._guarded_execute("query { x }", {})

    # Breaker is still CLOSED because permanent errors don't count
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_transport_server_error_502_classified_as_transient() -> None:
    """065 Fix 23: gql raises TransportServerError for upstream 5xx
    (e.g. GitHub GraphQL returning 502 Bad Gateway).  These must be
    classified as TransientGitHubError so callers log a clean warning
    and apply backoff instead of bubbling an unhandled traceback."""
    from gql.transport.exceptions import TransportServerError

    class _ErroringClient:
        def execute(self, _q, variable_values):
            raise TransportServerError("502 Bad Gateway", code=502)

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc.project_id = "PVT_1"
    svc._client = _ErroringClient()
    svc._last_token = "tok"

    with pytest.raises(TransientGitHubError) as excinfo:
        await svc._execute("query { x }", {})
    assert "502" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 075: get_required_status_checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_required_status_checks_exact_pattern_match() -> None:
    """Exact pattern match against default_branch returns the configured contexts."""
    svc = _initialized_svc(
        {
            "repository": {
                "branchProtectionRules": {
                    "nodes": [
                        {
                            "pattern": "main",
                            "requiredStatusChecks": [
                                {"context": "lint"},
                                {"context": "unit-tests"},
                            ],
                        }
                    ]
                }
            }
        }
    )
    result = await svc.get_required_status_checks("acme", "repo", "main")
    assert result == {"lint", "unit-tests"}


@pytest.mark.asyncio
async def test_get_required_status_checks_glob_pattern_match() -> None:
    """Glob patterns like ``release/*`` match branches via fnmatch."""
    svc = _initialized_svc(
        {
            "repository": {
                "branchProtectionRules": {
                    "nodes": [
                        {
                            "pattern": "release/*",
                            "requiredStatusChecks": [{"context": "e2e"}],
                        }
                    ]
                }
            }
        }
    )
    result = await svc.get_required_status_checks("acme", "repo", "release/1.0")
    assert result == {"e2e"}


@pytest.mark.asyncio
async def test_get_required_status_checks_non_matching_pattern_skipped() -> None:
    """Rules whose pattern does not match the default branch are ignored."""
    svc = _initialized_svc(
        {
            "repository": {
                "branchProtectionRules": {
                    "nodes": [
                        {
                            "pattern": "develop",
                            "requiredStatusChecks": [{"context": "lint"}],
                        }
                    ]
                }
            }
        }
    )
    result = await svc.get_required_status_checks("acme", "repo", "main")
    assert result == set()


@pytest.mark.asyncio
async def test_get_required_status_checks_null_rules_returns_none() -> None:
    """A null branchProtectionRules block (token lacks admin:read) returns None
    so callers can fall through to the next resolver layer."""
    svc = _initialized_svc({"repository": {"branchProtectionRules": None}})
    result = await svc.get_required_status_checks("acme", "repo", "main")
    assert result is None


@pytest.mark.asyncio
async def test_get_required_status_checks_empty_rules_returns_empty_set() -> None:
    """Readable but empty rules list returns an empty set (distinct from None)."""
    svc = _initialized_svc(
        {"repository": {"branchProtectionRules": {"nodes": []}}}
    )
    result = await svc.get_required_status_checks("acme", "repo", "main")
    assert result == set()


@pytest.mark.asyncio
async def test_get_required_status_checks_query_failure_returns_none() -> None:
    """Any exception from the GQL call is swallowed → None (fall through)."""

    class _RaisingClient:
        def execute(self, _query: Any, variable_values: dict[str, Any]) -> Any:
            raise RuntimeError("boom")

    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc.project_id = "PVT_1"
    svc._client = _RaisingClient()
    svc._last_token = "tok"

    result = await svc.get_required_status_checks("acme", "repo", "main")
    assert result is None

