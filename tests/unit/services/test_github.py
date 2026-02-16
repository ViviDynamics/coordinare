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

    def _build_client(self):
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
