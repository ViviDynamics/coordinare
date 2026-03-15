from __future__ import annotations

from typing import Any

import pytest

from coordinare.services.github import GitHubService


class _FakeClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses

    async def execute(self, query: object, variable_values: dict[str, Any]) -> dict[str, Any]:
        _ = (query, variable_values)
        return self._responses.pop(0)


class _TestGitHubService(GitHubService):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(token="tok", org="acme", project_number=1)
        self._responses = responses

    def _build_client(self, token: str = ""):
        return _FakeClient(self._responses)


@pytest.mark.asyncio
async def test_poll_board_returns_grouped_snapshot() -> None:
    service = _TestGitHubService(
        [
            {"organization": {"projectV2": {"id": "P1", "title": "Board"}}},
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "status-field",
                                "name": "Status",
                                "options": [
                                    {"id": "todo-opt", "name": "ToDo / Backlog"},
                                    {"id": "prog-opt", "name": "In Progress"},
                                ],
                            }
                        ]
                    }
                }
            },
            {
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": "ITEM_1",
                                "fieldValues": {"nodes": [{"name": "In Progress"}]},
                                "content": {
                                    "id": "ISSUE_1",
                                    "number": 1,
                                    "title": "Work",
                                    "body": "Do work",
                                },
                            }
                        ]
                    }
                }
            },
        ]
    )
    await service.initialize()

    board = await service.poll_board()

    assert board["snapshot"]["IN_PROGRESS"] == ["ITEM_1"]


@pytest.mark.asyncio
async def test_move_card_rejects_unknown_status_option() -> None:
    service = _TestGitHubService(
        [
            {"organization": {"projectV2": {"id": "P1", "title": "Board"}}},
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "status-field",
                                "name": "Status",
                                "options": [{"id": "done-opt", "name": "Done"}],
                            }
                        ]
                    }
                }
            },
        ]
    )
    await service.initialize()

    with pytest.raises(ValueError, match="Unknown status option"):
        await service.move_card("ITEM_1", "IN_PROGRESS")


@pytest.mark.asyncio
async def test_mergeability_requires_mergeable_and_approved() -> None:
    service = _TestGitHubService(
        [
            {
                "node": {
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "reviewDecision": "APPROVED",
                }
            }
        ]
    )

    result = await service.check_mergeability("PR_1")

    assert result["mergeable"] is True


@pytest.mark.asyncio
async def test_get_issue_details_returns_issue_node() -> None:
    service = _TestGitHubService(
        [{"node": {"id": "ISSUE_1", "number": 42, "title": "Bug", "body": "Fix it"}}]
    )
    service.project_id = "P1"
    service.field_cache = {"status_field_id": "F1", "status_option_ids": {}}

    result = await service.get_issue_details("ISSUE_1")

    assert result["title"] == "Bug"


@pytest.mark.asyncio
async def test_get_issue_details_returns_empty_on_missing_node() -> None:
    service = _TestGitHubService([{"node": None}])
    service.project_id = "P1"
    service.field_cache = {"status_field_id": "F1", "status_option_ids": {}}

    result = await service.get_issue_details("ISSUE_MISSING")

    assert result == {}


@pytest.mark.asyncio
async def test_get_pr_reviews_parses_review_list() -> None:
    service = _TestGitHubService(
        [
            {
                "node": {
                    "reviews": {
                        "nodes": [
                            {
                                "id": "RVW_1",
                                "author": {"login": "alice", "__typename": "User"},
                                "state": "APPROVED",
                                "body": "LGTM",
                                "submittedAt": "2026-02-25T12:00:00Z",
                            }
                        ]
                    },
                    "reviewDecision": "APPROVED",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                }
            }
        ]
    )
    service.project_id = "P1"
    service.field_cache = {"status_field_id": "F1", "status_option_ids": {}}

    reviews = await service.get_pr_reviews("PR_1")

    assert len(reviews) == 1
    assert reviews[0]["author_login"] == "alice"
    assert reviews[0]["state"] == "APPROVED"


@pytest.mark.asyncio
async def test_squash_merge_returns_merge_result() -> None:
    service = _TestGitHubService(
        [
            {
                "mergePullRequest": {
                    "pullRequest": {
                        "id": "PR_1",
                        "merged": True,
                        "mergeCommit": {"oid": "abc1234", "messageHeadline": "Fix bug"},
                    }
                }
            }
        ]
    )
    service.project_id = "P1"
    service.field_cache = {"status_field_id": "F1", "status_option_ids": {}}

    result = await service.squash_merge("PR_1")

    assert result["merged"] is True
    assert result["merge_commit"]["oid"] == "abc1234"


@pytest.mark.asyncio
async def test_add_comment_returns_comment_node() -> None:
    service = _TestGitHubService(
        [
            {
                "addComment": {
                    "commentEdge": {
                        "node": {"id": "C_1", "body": "hello", "createdAt": "2026-02-25T12:00:00Z"}
                    }
                }
            }
        ]
    )
    service.project_id = "P1"
    service.field_cache = {"status_field_id": "F1", "status_option_ids": {}}

    result = await service.add_comment("ISSUE_1", "hello")

    assert result["id"] == "C_1"


@pytest.mark.asyncio
async def test_move_card_succeeds_with_valid_status() -> None:
    service = _TestGitHubService(
        [
            {"organization": {"projectV2": {"id": "P1", "title": "Board"}}},
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "status-field",
                                "name": "Status",
                                "options": [
                                    {"id": "prog-opt", "name": "In Progress"},
                                    {"id": "done-opt", "name": "Done"},
                                ],
                            }
                        ]
                    }
                }
            },
            {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}},
        ]
    )
    await service.initialize()

    await service.move_card("ITEM_1", "DONE")


@pytest.mark.asyncio
async def test_poll_board_handles_multiple_status_columns() -> None:
    service = _TestGitHubService(
        [
            {"organization": {"projectV2": {"id": "P1", "title": "Board"}}},
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "status-field",
                                "name": "Status",
                                "options": [
                                    {"id": "todo-opt", "name": "ToDo / Backlog"},
                                    {"id": "blocked-opt", "name": "Blocked"},
                                    {"id": "review-opt", "name": "In Review"},
                                    {"id": "done-opt", "name": "Done"},
                                ],
                            }
                        ]
                    }
                }
            },
            {
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": "ITEM_1",
                                "fieldValues": {"nodes": [{"name": "Blocked"}]},
                                "content": {"id": "I1", "number": 1, "title": "A", "body": ""},
                            },
                            {
                                "id": "ITEM_2",
                                "fieldValues": {"nodes": [{"name": "In Review"}]},
                                "content": {"id": "I2", "number": 2, "title": "B", "body": ""},
                            },
                            {
                                "id": "ITEM_3",
                                "fieldValues": {"nodes": [{"name": "Done"}]},
                                "content": {"id": "I3", "number": 3, "title": "C", "body": ""},
                            },
                            {
                                "id": "ITEM_4",
                                "fieldValues": {"nodes": [{"name": "ToDo / Backlog"}]},
                                "content": {"id": "I4", "number": 4, "title": "D", "body": ""},
                            },
                        ]
                    }
                }
            },
        ]
    )
    await service.initialize()

    board = await service.poll_board()

    assert "ITEM_1" in board["snapshot"]["BLOCKED"]
    assert "ITEM_2" in board["snapshot"]["IN_REVIEW"]
    assert "ITEM_3" in board["snapshot"]["DONE"]
    assert "ITEM_4" in board["snapshot"]["TODO"]


@pytest.mark.asyncio
async def test_ensure_initialized_raises_when_not_initialized() -> None:
    service = _TestGitHubService([])

    with pytest.raises(RuntimeError, match="not initialized"):
        service._ensure_initialized()
