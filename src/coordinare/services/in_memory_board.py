"""A work item provider held in memory, with nothing GitHub-shaped about it (#400).

Two jobs. It is the second provider the conformance suite runs against, so "every
provider passes the same suite" means something with only one real provider; and
it lets the board bench drive a work item that has no issue number, no node id and
no GitHub URL from To Do to Done.

It behaves like a tracker, not like a test double:

- Ids are project keys (``PAY-1``), not node ids.
- It has its own state names. ``DEFAULT_STATES`` maps them to coordinare's
  canonical statuses, and an item in a state with no mapping is not listed.
- Its workflow can forbid a transition, which ``move_card`` reports as a refused
  ``MoveOutcome`` and leaves the item where it was.
- Comment ids, and therefore comment cursors, are strings.
- A person can move an item or comment on it at any time, through methods the
  protocol does not have, because coordinare must never be the one doing that.

Nothing here persists. A daemon restart against it starts from an empty board.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from coordinare.services.board_provider import MoveOutcome

logger = structlog.get_logger(__name__)

#: Native state name -> canonical status. Order matters: the first native state for
#: a canonical status is the one a move to that status lands in.
DEFAULT_STATES: dict[str, str] = {
    "Backlog": "BACKLOG",
    "To Do": "TODO",
    "Blocked": "BLOCKED",
    "Doing": "IN_PROGRESS",
    "Review": "IN_REVIEW",
    "Done": "DONE",
}

#: The canonical statuses a listing reports, in board order.
CANONICAL_STATUSES: tuple[str, ...] = ("BACKLOG", "TODO", "BLOCKED", "IN_PROGRESS", "IN_REVIEW", "DONE")


@dataclass
class InMemoryWorkItem:
    """One work item as the tracker holds it."""

    id: str
    title: str
    body: str
    native_state: str
    url: str
    comments: list[dict[str, Any]] = field(default_factory=list)
    #: Each transition as ``(from native state, to native state, actor)``.
    history: list[tuple[str, str, str]] = field(default_factory=list)


class InMemoryBoardProvider:
    """A ``BoardProvider`` whose tracker lives in this object."""

    def __init__(
        self,
        *,
        project_key: str = "ITEM",
        states: dict[str, str] | None = None,
        base_url: str = "memory://work-items",
        login: str = "coordinare",
    ) -> None:
        self._key = project_key
        self._states = dict(states if states is not None else DEFAULT_STATES)
        unknown = set(self._states.values()) - set(CANONICAL_STATUSES)
        if unknown:
            msg = f"states map to statuses coordinare does not have: {sorted(unknown)}"
            raise ValueError(msg)
        self._base_url = base_url.rstrip("/")
        self._login = login
        self._items: dict[str, InMemoryWorkItem] = {}
        self._forbidden: set[tuple[str, str]] = set()
        self._next_item = 0
        self._next_comment = 0

    # ---- the tracker's own side: what a person (or a test) does --------------------

    def add_item(self, title: str, *, body: str = "", status: str = "TODO") -> str:
        """Create an item in the native state that *status* maps to. Returns its key."""
        self._next_item += 1
        item_id = f"{self._key}-{self._next_item}"
        self._items[item_id] = InMemoryWorkItem(
            id=item_id,
            title=title,
            body=body,
            native_state=self._native_for(status),
            url=f"{self._base_url}/{item_id}",
        )
        return item_id

    def person_moves(self, item_id: str, status: str) -> None:
        """A person moves the item to the native state for a canonical *status*."""
        self.person_moves_to_native(item_id, self._native_for(status))

    def person_moves_to_native(self, item_id: str, native_state: str) -> None:
        """A person moves the item to any native state, mapped or not."""
        item = self._item(item_id)
        item.history.append((item.native_state, native_state, "person"))
        item.native_state = native_state

    def person_comments(self, item_id: str, *, author: str, body: str) -> str:
        """A person comments on the item. Returns the comment id."""
        return self._append_comment(self._item(item_id), author, body)

    def forbid(self, *, from_status: str, to_status: str) -> None:
        """The tracker's workflow forbids moving from one canonical status to another."""
        self._forbidden.add((from_status, to_status))

    def status_of(self, item_id: str) -> str:
        """The canonical status of an item, or ``""`` when its state has no mapping."""
        return self._states.get(self._item(item_id).native_state, "")

    def item(self, item_id: str) -> InMemoryWorkItem:
        return self._item(item_id)

    # ---- BoardProvider ---------------------------------------------------------------

    async def poll_board(self) -> dict[str, Any]:
        snapshot: dict[str, list[str]] = {status: [] for status in CANONICAL_STATUSES}
        titles: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        for item_id, item in self._items.items():
            status = self._states.get(item.native_state)
            if status is None:
                continue  # a state coordinare has no name for is not its work
            snapshot[status].append(item_id)
            titles[item_id] = item.title
            descriptions[item_id] = item.body
        return {"snapshot": snapshot, "titles": titles, "descriptions": descriptions}

    async def move_card(self, card_id: str, status: str) -> MoveOutcome:
        item = self._item(card_id)
        current = self._states.get(item.native_state, "")
        if current == status:
            return MoveOutcome.ok()  # already there: nothing to write
        if (current, status) in self._forbidden:
            return MoveOutcome.refused(f"workflow forbids {current or item.native_state} -> {status}")
        target = self._native_or_none(status)
        if target is None:
            return MoveOutcome.refused(f"no state on this board maps to {status}")
        item.history.append((item.native_state, target, "coordinare"))
        item.native_state = target
        return MoveOutcome.ok()

    async def get_card(self, card_id: str) -> dict[str, Any]:
        item = self._items.get(card_id)
        if item is None:
            return {}
        return {
            "id": item.id,
            "title": item.title,
            "body": item.body,
            "url": item.url,
            "state": item.native_state,
        }

    async def add_card_comment(self, card_id: str, body: str) -> dict[str, Any]:
        item = self._item(card_id)
        comment_id = self._append_comment(item, self._login, body)
        return dict(next(c for c in item.comments if c["id"] == comment_id))

    async def get_card_comments(
        self, card_id: str, since_id: str | int | None = None,
    ) -> list[dict[str, Any]]:
        item = self._items.get(card_id)
        if item is None:
            return []
        comments = item.comments
        if since_id is not None:
            ids = [c["id"] for c in comments]
            cursor = str(since_id)
            if cursor in ids:
                comments = comments[ids.index(cursor) + 1:]
            else:
                # A cursor this tracker never issued. Returning everything risks a
                # replay; returning nothing risks a lost comment. Replay is the one
                # coordinare already guards against (it keeps processed ids), so it
                # is the safer of the two.
                logger.warning("in_memory_board.unknown_comment_cursor", card_id=card_id, cursor=cursor)
        return [dict(c) for c in comments]

    # ---- helpers -----------------------------------------------------------------------

    def _item(self, item_id: str) -> InMemoryWorkItem:
        try:
            return self._items[item_id]
        except KeyError:
            msg = f"no work item {item_id!r} on this board"
            raise LookupError(msg) from None

    def _native_or_none(self, status: str) -> str | None:
        return next((native for native, mapped in self._states.items() if mapped == status), None)

    def _native_for(self, status: str) -> str:
        native = self._native_or_none(status)
        if native is None:
            msg = f"no state on this board maps to {status!r}"
            raise ValueError(msg)
        return native

    def _append_comment(self, item: InMemoryWorkItem, author: str, body: str) -> str:
        self._next_comment += 1
        comment_id = f"{item.id}#c{self._next_comment}"
        item.comments.append({
            "id": comment_id,
            "author": author,
            "body": body,
            "created_at": datetime.now(UTC).isoformat(),
        })
        return comment_id
