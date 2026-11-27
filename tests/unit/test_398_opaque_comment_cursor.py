"""Issue #398: the work item comment cursor is opaque.

A comment id, and so the cursor coordinare keeps per card, is whatever the
provider issued: GitHub's integer comment id, a tracker's string key, a timestamp
or a page token. Coordinare stores it, hands it back and compares it for equality.
It never sorts it, takes a maximum of it or reads it as a number; the provider's
own order (oldest first) is the order comments are handled in.

GitHub keeps its integer ids, so a snapshot written before this change loads
unchanged and a GitHub symphony persists exactly what it did before.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from coordinare.daemon import _persist_active_sessions, _restored_session_dict
from coordinare.graph.nodes import route_issue_comments as router_module
from coordinare.graph.nodes.route_issue_comments import route_issue_comments
from coordinare.graph.state import initial_state
from coordinare.services.board_provider import GitHubProjectsBoardProvider
from coordinare.services.card_identity import CardIdentityMap
from coordinare.services.in_memory_board import InMemoryBoardProvider
from coordinare.services.issue_comment_service import fetch_new_issue_comments
from coordinare.state_store import WorkflowSnapshot


@pytest.fixture(autouse=True)
def _keyword_classifier(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route with the keyword classifier, recording which bodies were classified."""
    seen: list[str] = []

    async def no_model(body: str, *args: Any, **kwargs: Any) -> None:
        seen.append(body)

    monkeypatch.setattr(router_module, "classify_issue_comment_ai", no_model)
    return seen


def _timestamps() -> Any:
    """Comment ids that are timestamps, as some trackers page by."""
    return lambda n: f"2026-11-27T10:{n:02d}:00.000Z"


def _tokens_out_of_lexical_order() -> Any:
    """Opaque tokens whose sort order is not their creation order."""
    tokens = ["zz-9f", "aa-01", "mm-77", "bb-3c", "yy-00"]
    return lambda n: tokens[n - 1]


def _state_for(provider: Any, card: str) -> Any:
    state = initial_state()
    state["board_provider"] = provider
    state["current_card"] = {"id": card}
    return state


# --- the router ----------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "comment_ids",
    [None, _timestamps(), _tokens_out_of_lexical_order()],
    ids=["default-token", "timestamp", "unordered-token"],
)
async def test_a_provider_cursor_drives_routing_with_no_caller_change(
    comment_ids: Any, _keyword_classifier: list[str],
) -> None:
    provider = InMemoryBoardProvider(project_key="PAY", comment_ids=comment_ids)
    card = provider.add_item("One")
    first = provider.person_comments(card, author="pat", body="what about the 404 case?")
    second = provider.person_comments(card, author="pat", body="and the 410 case?")
    state = _state_for(provider, card)

    await route_issue_comments(state)

    assert _keyword_classifier == ["what about the 404 case?", "and the 410 case?"]
    assert state["last_issue_comment_id"] == second, "the cursor is the provider's own id, as issued"
    assert state["processed_issue_comment_ids"] == {first, second}
    assert [c["comment_id"] for c in state["card_clarifications"]] == [first, second]

    await route_issue_comments(state)
    assert len(_keyword_classifier) == 2, "nothing after the cursor, so nothing is routed again"

    third = provider.person_comments(card, author="pat", body="one more thing?")
    await route_issue_comments(state)
    assert _keyword_classifier[2:] == ["one more thing?"]
    assert state["last_issue_comment_id"] == third


@pytest.mark.asyncio
async def test_a_bounded_backlog_follows_the_providers_order_not_the_ids(
    monkeypatch: pytest.MonkeyPatch, _keyword_classifier: list[str],
) -> None:
    """Deferred comments are delivered later, in order, and none is skipped."""
    monkeypatch.setattr(router_module, "_COMMENTS_PER_TICK", 2)
    provider = InMemoryBoardProvider(project_key="PAY", comment_ids=_tokens_out_of_lexical_order())
    card = provider.add_item("One")
    ids = [provider.person_comments(card, author="pat", body=f"question {n}?") for n in range(1, 6)]
    state = _state_for(provider, card)

    await route_issue_comments(state)
    assert state["last_issue_comment_id"] == ids[1]
    await route_issue_comments(state)
    assert state["last_issue_comment_id"] == ids[3]
    await route_issue_comments(state)
    assert state["last_issue_comment_id"] == ids[4]

    assert _keyword_classifier == [f"question {n}?" for n in range(1, 6)]
    assert [c["comment_id"] for c in state["card_clarifications"]] == ids


# --- the reader ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reader_keeps_each_id_as_the_provider_issued_it() -> None:
    class Board:
        async def get_card_comments(self, card_id: str, since_id: Any = None) -> list[dict[str, Any]]:
            return [
                {"id": "PAY-c-7f", "author": "a", "body": "x", "created_at": "t"},
                {"id": 4100, "author": "a", "body": "y", "created_at": "t"},
            ]

    events = await fetch_new_issue_comments("PAY-1", None, Board())

    assert [e.comment_id for e in events] == ["PAY-c-7f", 4100]


@pytest.mark.asyncio
async def test_a_comment_with_no_usable_id_is_skipped_not_fatal() -> None:
    class Board:
        async def get_card_comments(self, card_id: str, since_id: Any = None) -> list[dict[str, Any]]:
            return [
                {"author": "a", "body": "no id", "created_at": "t"},
                {"id": "", "author": "a", "body": "empty id", "created_at": "t"},
                {"id": True, "author": "a", "body": "bool id", "created_at": "t"},
                {"id": "c-1", "author": "a", "body": "kept", "created_at": "t"},
            ]

    events = await fetch_new_issue_comments("PAY-1", None, Board())

    assert [e.body for e in events] == ["kept"]


# --- the GitHub adapter --------------------------------------------------------------------------


class _GitHub(CardIdentityMap):
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self._comments = comments
        self.since: list[Any] = []
        self._remember_issue_numbers({"ITEM_1": 42})

    async def get_issue_comments(self, issue_number: int, since_id: int | None = None) -> list[dict[str, Any]]:
        self.since.append(since_id)
        return [c for c in self._comments if since_id is None or c["id"] > since_id]


@pytest.mark.asyncio
async def test_github_hands_back_its_comments_oldest_first() -> None:
    github = _GitHub([{"id": i, "author": "a", "body": str(i), "created_at": "t"} for i in (5, 1, 4)])

    comments = await GitHubProjectsBoardProvider(github).get_card_comments("ITEM_1")

    assert [c["id"] for c in comments] == [1, 4, 5]


@pytest.mark.asyncio
async def test_github_accepts_its_own_cursor_back_as_text() -> None:
    """A cursor that went through text (a log, a JSON field) still names the same comment."""
    github = _GitHub([{"id": i, "author": "a", "body": str(i), "created_at": "t"} for i in (10, 11, 12)])

    comments = await GitHubProjectsBoardProvider(github).get_card_comments("ITEM_1", since_id="11")

    assert github.since == [11]
    assert [c["id"] for c in comments] == [12]


@pytest.mark.asyncio
async def test_github_given_a_cursor_it_never_issued_replays_rather_than_loses() -> None:
    github = _GitHub([{"id": i, "author": "a", "body": str(i), "created_at": "t"} for i in (10, 11)])

    comments = await GitHubProjectsBoardProvider(github).get_card_comments("ITEM_1", since_id="PAY-c-7f")

    assert github.since == [None]
    assert [c["id"] for c in comments] == [10, 11]


# --- persistence ----------------------------------------------------------------------------------


def _round_trip(sessions: dict[str, Any]) -> WorkflowSnapshot:
    snapshot = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC), phase="idle", active_sessions=_persist_active_sessions(sessions),
    )
    return WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())


def test_string_cursors_survive_a_snapshot_round_trip() -> None:
    loaded = _round_trip({
        "PAY-1": {
            "processed_issue_comment_ids": {"2026-11-27T10:01:00.000Z", "2026-11-27T10:02:00.000Z"},
            "last_issue_comment_id": "2026-11-27T10:02:00.000Z",
        },
    })

    session = loaded.active_sessions["PAY-1"]
    assert session.last_issue_comment_id == "2026-11-27T10:02:00.000Z"
    assert session.processed_issue_comment_ids == ["2026-11-27T10:01:00.000Z", "2026-11-27T10:02:00.000Z"]


def test_integer_cursors_stay_integers_so_github_persists_what_it_did() -> None:
    loaded = _round_trip({
        "card-1": {"processed_issue_comment_ids": {3001, 3005}, "last_issue_comment_id": 3005},
    })

    session = loaded.active_sessions["card-1"]
    assert session.last_issue_comment_id == 3005
    assert session.processed_issue_comment_ids == [3001, 3005]
    assert all(type(cid) is int for cid in session.processed_issue_comment_ids)


def test_a_numeric_looking_string_is_not_turned_into_a_number() -> None:
    """A tracker that issued "10001" gets "10001" back, never 10001."""
    loaded = _round_trip({
        "PAY-1": {"processed_issue_comment_ids": {"10001"}, "last_issue_comment_id": "10001"},
    })

    session = loaded.active_sessions["PAY-1"]
    assert session.last_issue_comment_id == "10001"
    assert session.processed_issue_comment_ids == ["10001"]


def test_the_processed_list_stays_bounded_with_string_ids() -> None:
    ids = {f"c-{n:05d}" for n in range(2500)}
    out = _persist_active_sessions({"PAY-1": {"processed_issue_comment_ids": ids}})

    kept = out["PAY-1"].processed_issue_comment_ids
    assert len(kept) == 2000
    assert set(kept) <= ids


@pytest.mark.asyncio
async def test_an_older_snapshot_loads_and_no_comment_is_processed_twice(
    _keyword_classifier: list[str],
) -> None:
    """A snapshot written before #398 (integer cursor, schema v40) restores as it was."""
    raw = {
        "schema_version": 40,
        "snapshot_at": "2026-11-26T09:00:00+00:00",
        "phase": "idle",
        "active_card_id": "ITEM_1",
        "active_sessions": {
            "ITEM_1": {
                "card_id": "ITEM_1",
                "processed_issue_comment_ids": [10, 11],
                "last_issue_comment_id": 11,
            },
        },
    }
    snapshot = WorkflowSnapshot.model_validate_json(json.dumps(raw))
    restored = _restored_session_dict("ITEM_1", snapshot.active_sessions["ITEM_1"], snapshot, None)
    assert restored["last_issue_comment_id"] == 11
    assert restored["processed_issue_comment_ids"] == {10, 11}

    github = _GitHub([
        {"id": i, "author": "pat", "body": f"comment {i}?", "created_at": "t"} for i in (10, 11, 12)
    ])
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    state["last_issue_comment_id"] = restored["last_issue_comment_id"]
    state["processed_issue_comment_ids"] = restored["processed_issue_comment_ids"]

    await route_issue_comments(state)

    assert _keyword_classifier == ["comment 12?"]
    assert state["last_issue_comment_id"] == 12


def test_a_snapshot_with_string_cursors_meets_the_snapshot_contract() -> None:
    from pathlib import Path

    import jsonschema

    contract = json.loads(
        (Path(__file__).resolve().parents[2]
         / "specs/003-state-persistence/contracts/workflow-snapshot.schema.json").read_text(),
    )
    snapshot = _round_trip({
        "PAY-1": {"processed_issue_comment_ids": {"c-1a", "c-2b"}, "last_issue_comment_id": "c-2b"},
        "card-1": {"processed_issue_comment_ids": {3001}, "last_issue_comment_id": 3001},
    })

    jsonschema.validate(instance=json.loads(snapshot.model_dump_json()), schema=contract)
