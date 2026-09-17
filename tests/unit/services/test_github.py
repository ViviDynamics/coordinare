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
            {"repositoryOwner": {"projectV2": {"id": "P1", "title": "Board"}}},
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
                            },
                        ],
                    },
                },
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
                            },
                        ],
                    },
                },
            },
        ],
    )
    await service.initialize()

    board = await service.poll_board()

    assert board["snapshot"]["IN_PROGRESS"] == ["ITEM_1"]


@pytest.mark.asyncio
async def test_initialize_resolves_project_under_a_user_account() -> None:
    """A board owned by a personal User (not an Organization) must resolve.

    The owner-agnostic FindProject query returns the project under the
    ``repositoryOwner`` wrapper for both users and orgs; initialize() must
    accept that shape so personal GitHub accounts work, not just orgs.
    """
    service = _TestGitHubService(
        [
            {"repositoryOwner": {"projectV2": {"id": "P_USER", "title": "My Board"}}},
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "status-field",
                                "name": "Status",
                                "options": [{"id": "todo-opt", "name": "ToDo"}],
                            },
                        ],
                    },
                },
            },
        ],
    )
    await service.initialize()

    assert service.project_id == "P_USER"
    assert service.project_title == "My Board"


@pytest.mark.asyncio
async def test_initialize_raises_clear_error_when_owner_has_no_project() -> None:
    """An unknown/absent owner resolves ``repositoryOwner: null``; the error
    must name that it was checked as both user and organization."""
    service = _TestGitHubService([{"repositoryOwner": None}])

    with pytest.raises(ValueError, match="user and organization"):
        await service.initialize()


@pytest.mark.asyncio
async def test_move_card_rejects_unknown_status_option() -> None:
    service = _TestGitHubService(
        [
            {"repositoryOwner": {"projectV2": {"id": "P1", "title": "Board"}}},
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "status-field",
                                "name": "Status",
                                "options": [
                                    {"id": "ready-opt", "name": "Ready"},
                                    {"id": "done-opt", "name": "Done"},
                                ],
                            },
                        ],
                    },
                },
            },
        ],
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
                },
            },
        ],
    )

    result = await service.check_mergeability("PR_1")

    assert result["mergeable"] is True


@pytest.mark.asyncio
async def test_get_issue_details_returns_issue_node() -> None:
    service = _TestGitHubService(
        [{"node": {"id": "ISSUE_1", "number": 42, "title": "Bug", "body": "Fix it"}}],
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
                            },
                        ],
                    },
                    "reviewDecision": "APPROVED",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                },
            },
        ],
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
                    },
                },
            },
        ],
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
                        "node": {"id": "C_1", "body": "hello", "createdAt": "2026-02-25T12:00:00Z"},
                    },
                },
            },
        ],
    )
    service.project_id = "P1"
    service.field_cache = {"status_field_id": "F1", "status_option_ids": {}}

    result = await service.add_comment("ISSUE_1", "hello")

    assert result["id"] == "C_1"


@pytest.mark.asyncio
async def test_move_card_succeeds_with_valid_status() -> None:
    service = _TestGitHubService(
        [
            {"repositoryOwner": {"projectV2": {"id": "P1", "title": "Board"}}},
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "status-field",
                                "name": "Status",
                                "options": [
                                    {"id": "ready-opt", "name": "Ready"},
                                    {"id": "prog-opt", "name": "In Progress"},
                                    {"id": "done-opt", "name": "Done"},
                                ],
                            },
                        ],
                    },
                },
            },
            {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}},
        ],
    )
    await service.initialize()

    await service.move_card("ITEM_1", "DONE")


@pytest.mark.asyncio
async def test_poll_board_handles_multiple_status_columns() -> None:
    service = _TestGitHubService(
        [
            {"repositoryOwner": {"projectV2": {"id": "P1", "title": "Board"}}},
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
                            },
                        ],
                    },
                },
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
                        ],
                    },
                },
            },
        ],
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


def _poll_board_response_with_assignees(assignee_logins: list[str]) -> list[dict]:
    return [
        {"repositoryOwner": {"projectV2": {"id": "P1", "title": "Board"}}},
        {
            "node": {
                "fields": {
                    "nodes": [
                        {
                            "id": "status-field",
                            "name": "Status",
                            "options": [{"id": "todo-opt", "name": "ToDo"}],
                        },
                    ],
                },
            },
        },
        {
            "node": {
                "items": {
                    "nodes": [
                        {
                            "id": "ITEM_1",
                            "fieldValues": {"nodes": [{"name": "ToDo"}]},
                            "content": {
                                "id": "ISSUE_1",
                                "number": 1,
                                "title": "Fix bug",
                                "body": "",
                                "url": "https://github.com/acme/repo/issues/1",
                                "labels": {"nodes": []},
                                "assignees": {
                                    "nodes": [{"login": login} for login in assignee_logins],
                                },
                            },
                        },
                    ],
                },
            },
        },
    ]


@pytest.mark.asyncio
async def test_poll_board_item_assignees_populated() -> None:
    """issue with assignees returns lowercase logins in item_assignees."""
    service = _TestGitHubService(_poll_board_response_with_assignees(["Coordinare-Bot", "JSmith"]))
    await service.initialize()

    board = await service.poll_board()

    assert board["item_assignees"]["ITEM_1"] == ["coordinare-bot", "jsmith"]


@pytest.mark.asyncio
async def test_poll_board_item_assignees_empty_for_unassigned() -> None:
    """issue with no assignees returns empty list in item_assignees."""
    service = _TestGitHubService(_poll_board_response_with_assignees([]))
    await service.initialize()

    board = await service.poll_board()

    assert board["item_assignees"]["ITEM_1"] == []


@pytest.mark.asyncio
async def test_poll_board_item_assignees_empty_for_draft_issue() -> None:
    """DraftIssue content has no assignees field — returns empty list."""
    service = _TestGitHubService([
        {"repositoryOwner": {"projectV2": {"id": "P1", "title": "Board"}}},
        {"node": {"fields": {"nodes": [{"id": "sf", "name": "Status", "options": [{"id": "t", "name": "ToDo"}]}]}}},
        {
            "node": {
                "items": {
                    "nodes": [
                        {
                            "id": "DRAFT_1",
                            "fieldValues": {"nodes": [{"name": "ToDo"}]},
                            "content": {"title": "Draft card", "body": ""},
                        },
                    ],
                },
            },
        },
    ])
    await service.initialize()

    board = await service.poll_board()

    assert board["item_assignees"].get("DRAFT_1", []) == []


def _poll_board_response_with_timeline(timeline_events: list[dict]) -> list[dict]:
    return [
        {"repositoryOwner": {"projectV2": {"id": "P1", "title": "Board"}}},
        {"node": {"fields": {"nodes": [{"id": "sf", "name": "Status", "options": [{"id": "t", "name": "ToDo"}]}]}}},
        {
            "node": {
                "items": {
                    "nodes": [
                        {
                            "id": "ITEM_1",
                            "fieldValues": {"nodes": [{"name": "ToDo"}]},
                            "content": {
                                "id": "ISSUE_1",
                                "number": 5,
                                "title": "Linked",
                                "body": "",
                                "url": "https://github.com/acme/repo/issues/5",
                                "labels": {"nodes": []},
                                "assignees": {"nodes": []},
                                "timelineItems": {"nodes": timeline_events},
                            },
                        },
                    ],
                },
            },
        },
    ]


def _pr_event(url: str, state: str, merged: bool = False) -> dict:
    return {"source": {"url": url, "state": state, "merged": merged}}


@pytest.mark.asyncio
async def test_poll_board_pr_urls_prefers_open_over_merged_and_closed() -> None:
    """When an issue references multiple PRs, OPEN beats MERGED beats CLOSED."""
    service = _TestGitHubService(
        _poll_board_response_with_timeline([
            _pr_event("https://github.com/acme/repo/pull/10", "CLOSED"),
            _pr_event("https://github.com/acme/repo/pull/11", "MERGED", merged=True),
            _pr_event("https://github.com/acme/repo/pull/12", "OPEN"),
        ]),
    )
    await service.initialize()

    board = await service.poll_board()

    assert board["pr_urls"]["ITEM_1"] == "https://github.com/acme/repo/pull/12"


@pytest.mark.asyncio
async def test_poll_board_pr_urls_falls_back_to_merged() -> None:
    """No OPEN PR — pick the merged one over the closed one."""
    service = _TestGitHubService(
        _poll_board_response_with_timeline([
            _pr_event("https://github.com/acme/repo/pull/10", "CLOSED"),
            _pr_event("https://github.com/acme/repo/pull/11", "MERGED", merged=True),
        ]),
    )
    await service.initialize()

    board = await service.poll_board()

    assert board["pr_urls"]["ITEM_1"] == "https://github.com/acme/repo/pull/11"


@pytest.mark.asyncio
async def test_poll_board_pr_urls_empty_when_no_cross_references() -> None:
    """No timeline events — pr_urls has no entry for the item."""
    service = _TestGitHubService(_poll_board_response_with_timeline([]))
    await service.initialize()

    board = await service.poll_board()

    assert "ITEM_1" not in board["pr_urls"]


class _RecordingClient(_FakeClient):
    """Fake client that records the variables sent with each call."""

    def __init__(self, responses: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        super().__init__(responses)
        self._calls = calls

    async def execute(self, query: object, variable_values: dict[str, Any]) -> dict[str, Any]:
        self._calls.append(dict(variable_values or {}))
        return await super().execute(query, variable_values)


class _RecordingGitHubService(_TestGitHubService):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(responses)
        self.calls: list[dict[str, Any]] = []

    def _build_client(self, token: str = ""):
        return _RecordingClient(self._responses, self.calls)


# The Status options a brand-new GitHub project board ships with, plus Blocked,
# which has no GitHub default and is added by the operator.
_DEFAULT_BOARD_COLUMNS = [
    {"id": "backlog-opt", "name": "Backlog"},
    {"id": "ready-opt", "name": "Ready"},
    {"id": "blocked-opt", "name": "Blocked"},
    {"id": "progress-opt", "name": "In progress"},
    {"id": "review-opt", "name": "In review"},
    {"id": "done-opt", "name": "Done"},
]


def _init_responses(columns: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The two responses ``initialize()`` consumes for a board with ``columns``."""
    return [
        {"repositoryOwner": {"projectV2": {"id": "P1", "title": "Board"}}},
        {
            "node": {
                "fields": {
                    "nodes": [{"id": "status-field", "name": "Status", "options": columns}],
                },
            },
        },
    ]


@pytest.mark.asyncio
async def test_poll_board_maps_every_default_github_column() -> None:
    """Every column of a stock GitHub board must land in the snapshot.

    ``Ready`` is the live bug: it is GitHub's pick-up lane and was not in the
    recognised set, so cards a human dragged there were silently dropped and
    coordinare saw no work at all.
    """
    labels = ["Backlog", "Ready", "Blocked", "In progress", "In review", "Done"]
    items = [
        {
            "id": f"ITEM_{i}",
            "fieldValues": {"nodes": [{"name": label}]},
            "content": {"id": f"I{i}", "number": i, "title": label, "body": ""},
        }
        for i, label in enumerate(labels, start=1)
    ]
    service = _TestGitHubService(
        [*_init_responses(_DEFAULT_BOARD_COLUMNS), {"node": {"items": {"nodes": items}}}],
    )
    await service.initialize()

    snapshot = (await service.poll_board())["snapshot"]

    assert snapshot["BACKLOG"] == ["ITEM_1"]
    assert snapshot["TODO"] == ["ITEM_2"]
    assert snapshot["BLOCKED"] == ["ITEM_3"]
    assert snapshot["IN_PROGRESS"] == ["ITEM_4"]
    assert snapshot["IN_REVIEW"] == ["ITEM_5"]
    assert snapshot["DONE"] == ["ITEM_6"]


@pytest.mark.asyncio
async def test_move_card_to_todo_targets_ready_not_backlog() -> None:
    """TODO is the pick-up lane, so a returned card belongs in Ready."""
    service = _RecordingGitHubService(
        [
            *_init_responses(_DEFAULT_BOARD_COLUMNS),
            {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}},
        ],
    )
    await service.initialize()

    await service.move_card("ITEM_1", "TODO")

    assert service.calls[-1]["optionId"] == "ready-opt"


@pytest.mark.asyncio
async def test_move_card_to_todo_falls_back_to_legacy_column() -> None:
    """Hand-built boards with no Ready column still resolve via the aliases."""
    service = _RecordingGitHubService(
        [
            *_init_responses([{"id": "todo-opt", "name": "ToDo / Backlog"}]),
            {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}},
        ],
    )
    await service.initialize()

    await service.move_card("ITEM_1", "TODO")

    assert service.calls[-1]["optionId"] == "todo-opt"


@pytest.mark.asyncio
async def test_initialize_rejects_a_board_with_no_pickup_lane() -> None:
    """Backlog is a holding lane, not a pick-up lane.

    A board with no Ready/ToDo column has nowhere to hand work back to. That
    is a permanent, operator-fixable misconfiguration, so it is reported at
    startup naming the missing column rather than surfacing much later as a
    card stranded mid-cycle by a swallowed ``move_card`` failure.
    """
    service = _TestGitHubService(_init_responses([{"id": "backlog-opt", "name": "Backlog"}]))

    with pytest.raises(ValueError, match="no Status column that maps to TODO"):
        await service.initialize()


@pytest.mark.asyncio
async def test_move_card_refuses_to_dump_into_backlog() -> None:
    """``move_card`` itself still refuses Backlog, independent of the gate.

    The startup gate means a live board can no longer reach this state, so the
    cache is built directly here to keep the guard pinned: TODO resolves only
    through the pick-up aliases, never through ``backlog``.
    """
    service = _TestGitHubService([])
    service.project_id = "P1"
    service.field_cache = {
        "status_field_id": "status-field",
        "status_option_ids": {"backlog": "backlog-opt"},
    }

    with pytest.raises(ValueError, match="Unknown status option"):
        await service.move_card("ITEM_1", "TODO")


def test_parse_diff_paths_exact_split_for_paths_with_b_slash() -> None:
    """412 round 12: a path containing " b/" defeats the greedy header split
    ("a/docs/foo b/bar.md b/docs/foo b/bar.md" -> "bar.md"). Header sides are
    the same path outside renames, so the exact "a/<X> b/<X>" form wins; the
    greedy match stays the fallback for renames."""
    diff = (
        "diff --git a/docs/foo b/bar.md b/docs/foo b/bar.md\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "diff --git a/normal.py b/normal.py\n"
        "--- a/normal.py\n"
        "+++ b/normal.py\n"
        "diff --git a/old.md b/new docs/x b/bar.md\n"
        "similarity index 100%\n"
        "rename from old.md\n"
        "rename to new docs/x b/bar.md\n"
    )
    assert GitHubService._parse_diff_paths(diff) == [
        "docs/foo b/bar.md",
        "normal.py",
        "new docs/x b/bar.md",
    ]


def test_parse_diff_paths_rename_to_metadata_is_authoritative() -> None:
    """412 round 13: git does not quote spaces in the diff header, so a
    rename whose a-side contains " b/" is ambiguous in the header alone --
    the section's rename-to metadata is the authoritative post-image. This
    is real git output for a rename of "old b/name.py" -> "new b/name.py"."""
    diff = (
        "diff --git a/old b/name.py b/new b/name.py\n"
        "similarity index 100%\n"
        "rename from old b/name.py\n"
        "rename to new b/name.py\n"
    )
    assert GitHubService._parse_diff_paths(diff) == ["new b/name.py"]


def test_parse_diff_paths_copy_to_metadata_is_authoritative() -> None:
    """412 round 14: copy-only sections carry "copy to" instead of a "+++"
    line -- real git output for copying src-file.py to "copy b/dest.py"."""
    diff = (
        "diff --git a/src-file.py b/copy b/dest.py\n"
        "similarity index 100%\n"
        "copy from src-file.py\n"
        "copy to copy b/dest.py\n"
    )
    assert GitHubService._parse_diff_paths(diff) == ["copy b/dest.py"]


def test_parse_diff_paths_decodes_quoted_rename_to_metadata() -> None:
    """412 round 17: git quotes and escapes the rename-to target when the
    post-image path needs it -- the quotes must not just be stripped off the
    raw escape spelling."""
    diff = (
        "diff --git a/old.py b/old.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        'rename to "caf\\303\\251 2.py"\n'
    )
    assert GitHubService._parse_diff_paths(diff) == ["café 2.py"]


def test_parse_diff_paths_keeps_line_escapes_canonical_with_the_diff_parser() -> None:
    """412 round 42: the changed-file list carries the same escaped spelling
    as the performer diff parser and the sanitizer's one-line metadata --
    one representation for every consumer of a changed-path spelling.
    (Round 34 decoded this list to the real control character on the belief
    the scanner argv or reviewer matching consumed it; rounds 35-41
    established the list is classification-only, so the split representation
    served nothing and the consumers now agree.)"""
    diff = (
        'diff --git "a/we\\nird.py" "b/we\\nird.py"\n'
        "--- a/we\\nird.py\n"
        "+++ b/we\\nird.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-old\n"
        "+new\n"
    )
    assert GitHubService._parse_diff_paths(diff) == ["we\\nird.py"]


def test_changed_path_spelling_agrees_with_the_performer_diff_parser() -> None:
    """412 round 42 drift guard: the GitHub changed-path list and the
    performer's parsed diff must spell a newline filename identically --
    rounds 35-42 churned on this split. Both spell it escaped; a future
    change to either side alone fails here."""
    from performer.workflows.reviewer.diffparse import parse_unified_diff

    diff = (
        'diff --git "a/we\\nird.py" "b/we\\nird.py"\n'
        "--- a/we\\nird.py\n"
        "+++ b/we\\nird.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-old\n"
        "+new\n"
    )
    assert [changed.path for changed in parse_unified_diff(diff)] == GitHubService._parse_diff_paths(diff)


def test_parse_diff_paths_decodes_tab_escapes_in_quoted_headers() -> None:
    """412 round 16: git escapes a real tab as \\\\t inside the quoted
    payload -- the decode must yield the tab, not drop the backslash."""
    diff = (
        'diff --git "a/ta\\tb.py" "b/ta\\tb.py"\n'
        "old mode 100644\n"
        "new mode 100755\n"
    )
    assert GitHubService._parse_diff_paths(diff) == ["ta\tb.py"]


def test_parse_diff_paths_handles_git_quoted_headers() -> None:
    """412 round 14: git quotes header paths it cannot emit raw (non-ASCII,
    embedded quotes); each side is a quoted string and the b-side is the
    post-image."""
    diff = (
        'diff --git "a/путь/файл.py" "b/путь/файл.py"\n'
        "old mode 100644\n"
        "new mode 100755\n"
    )
    assert GitHubService._parse_diff_paths(diff) == ["путь/файл.py"]


def test_parse_diff_paths_decodes_escaped_quote_in_quoted_headers() -> None:
    """412 round 15: git escapes an embedded quote as \\" inside the quoted
    payload -- real output for a file literally named quo"te.py."""
    diff = (
        'diff --git "a/quo\\"te.py" "b/quo\\"te.py"\n'
        "new file mode 100644\n"
    )
    assert GitHubService._parse_diff_paths(diff) == ['quo"te.py']


def test_parse_diff_paths_decodes_octal_escapes_in_quoted_headers() -> None:
    """412 round 15: non-ASCII names are octal-escaped UTF-8 bytes under
    core.quotePath -- real output for путь/файл.py."""
    diff = (
        'diff --git "a/\\320\\277\\321\\203\\321\\202\\321\\214/\\321\\204\\320\\260\\320\\271\\320\\273.py" '
        '"b/\\320\\277\\321\\203\\321\\202\\321\\214/\\321\\204\\320\\260\\320\\271\\320\\273.py"\n'
        "old mode 100644\n"
        "new mode 100755\n"
    )
    assert GitHubService._parse_diff_paths(diff) == ["путь/файл.py"]
