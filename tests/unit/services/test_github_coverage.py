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
    return svc


def _async_svc(*responses: Any) -> GitHubService:
    svc = GitHubService(token="tok", org="acme", project_number=1)
    svc._client = _AsyncFakeClient(list(responses))
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
    return svc


# ---------------------------------------------------------------------------
# _build_client
# ---------------------------------------------------------------------------


def test_build_client_returns_gql_client() -> None:
    """_build_client() produces a usable GQL Client object."""
    svc = GitHubService(token="tok", org="acme", project_number=1)
    client = svc._build_client()
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

    with pytest.raises(PermanentGitHubError):
        await svc._execute("query { __typename }", {})


# ---------------------------------------------------------------------------
# initialize — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_raises_when_project_not_found() -> None:
    svc = _svc({"organization": {"projectV2": None}})
    with pytest.raises(ValueError, match="Project not found"):
        await svc.initialize()


@pytest.mark.asyncio
async def test_initialize_raises_when_fields_malformed() -> None:
    svc = _svc(
        {"organization": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
        {"node": {"fields": {"nodes": "not-a-list"}}},
    )
    with pytest.raises(ValueError, match="malformed"):
        await svc.initialize()


@pytest.mark.asyncio
async def test_initialize_handles_options_not_a_list() -> None:
    """When a Status field's options is not a list, it is skipped gracefully."""
    svc = _svc(
        {"organization": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
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
        {"organization": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
        {"node": {"fields": {"nodes": [{"id": "FLD_X", "name": "Priority", "options": []}]}}},
    )
    with pytest.raises(ValueError, match="Status field not found"):
        await svc.initialize()


@pytest.mark.asyncio
async def test_initialize_skips_non_dict_nodes_and_options() -> None:
    """Non-dict nodes and options are silently skipped."""
    svc = _svc(
        {"organization": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
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
    """Non-list items node returns empty snapshot."""
    svc = _initialized_svc({"node": {"items": {"nodes": "bad"}}})
    result = await svc.poll_board()
    assert result == {"snapshot": {}}


@pytest.mark.asyncio
async def test_poll_board_item_without_id_is_skipped() -> None:
    """Item with no id (or empty string id) is skipped."""
    svc = _initialized_svc(_poll_response([{"id": "", "fieldValues": {"nodes": []}}]))
    result = await svc.poll_board()
    assert result["snapshot"] == {
        "TODO": [], "BLOCKED": [], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": []
    }


@pytest.mark.asyncio
async def test_poll_board_non_dict_item_is_skipped() -> None:
    """Non-dict entries in items list are skipped."""
    svc = _initialized_svc(_poll_response(["not-a-dict"]))
    result = await svc.poll_board()
    assert result["snapshot"]["TODO"] == []


@pytest.mark.asyncio
async def test_poll_board_field_values_not_a_list() -> None:
    """When fieldValues.nodes is not a list, status defaults to TODO."""
    svc = _initialized_svc(
        _poll_response([{"id": "ITEM_1", "fieldValues": {"nodes": "bad"}, "content": {}}])
    )
    result = await svc.poll_board()
    assert "ITEM_1" in result["snapshot"]["TODO"]


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
async def test_poll_board_unknown_status_name_leaves_todo() -> None:
    """A field value with an unrecognized name falls through all elif branches (status stays TODO)."""
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
    assert "ITEM_1" in result["snapshot"]["TODO"]


@pytest.mark.asyncio
async def test_poll_board_content_not_dict() -> None:
    """Non-dict content is gracefully ignored."""
    svc = _initialized_svc(
        _poll_response([{"id": "ITEM_1", "fieldValues": {"nodes": []}, "content": "bad"}])
    )
    result = await svc.poll_board()
    assert "ITEM_1" in result["snapshot"]["TODO"]
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
