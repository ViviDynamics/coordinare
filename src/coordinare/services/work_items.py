"""One typed record per listed work item (#395).

A board listing used to be a snapshot plus seven parallel dicts keyed by card id:
``issue_numbers``, ``content_node_ids``, ``issue_urls``, ``pr_urls``,
``item_labels``, ``item_assignees`` and ``native_blockers``. Every caller reached
into whichever it needed, and a tracker with no issue numbers and no node ids
could not supply half of them, so those callers could not run against it.

Callers now read ``work_items_of(board)``: one ``WorkItem`` per card. A provider
may put the records in the listing itself, under ``items``. The GitHub Projects
listing is translated here, from the shape ``GitHubService.poll_board`` returns,
so the translation sits with the provider rather than in each caller, and the
service and every existing fake keep their shape.

Fields a provider cannot supply stay empty. An empty ``owners``, ``labels`` or
``blockers`` is an ordinary value and no caller special-cases it.

``issue`` is the one GitHub-shaped field: the GitHub issue behind the item, set
only by the GitHub Projects translation. It is the code host's join key (the
issue a pull request closes, the issue whose comments the dependency check reads)
and only code-host paths read it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Matches "Depends on #N", "Blocked by #N", "After #N", "Requires #N"
#: (case-insensitive). The GitHub Projects text syntax for a blocker; the same
#: syntax ``services/dependency.parse_dependencies`` documents.
_DEP_PATTERN = re.compile(r"(?:depends\s+on|blocked\s+by|after|requires)\s+#(\d+)", re.IGNORECASE)

#: Blocker states a provider may report.
OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"
#: 396: the tracker says no such item exists, or could not be read this time.
MISSING, UNAVAILABLE = "missing", "unavailable"

#: GitHub's answers for an issue's state (``GitHubService.check_issue_state``),
#: as blocker states. Anything else is unknown.
_GITHUB_ISSUE_STATES = {
    "closed": CLOSED, "open": OPEN, "not_found": MISSING,
    "auth_error": UNAVAILABLE, "api_error": UNAVAILABLE, "transient": UNAVAILABLE,
}


def blocker_state_from_github(raw: object) -> str:
    """A GitHub issue state answer as a blocker state (396)."""
    return _GITHUB_ISSUE_STATES.get(str(raw), UNKNOWN)


@dataclass(frozen=True, slots=True)
class IssueLink:
    """The GitHub issue behind a work item, or behind a blocker.

    ``repo`` is the ``owner/name`` slug when the issue is known to live in a
    repository other than the board's; empty means the board's own.
    """

    number: int
    node_id: str = ""
    repo: str = ""


@dataclass(frozen=True, slots=True)
class Blocker:
    """Something a work item waits on.

    ``ref`` is the provider's own name for it (``#12``, ``acme/other#2``,
    ``PAY-3``). ``item_id`` is the blocking item's id when the provider knows
    it is on this board. ``state`` is what the provider knows of it when it is
    not: ``open``, ``closed`` or ``unknown``, and since 396 ``missing`` (no
    such item) or ``unavailable`` (it could not be read this time). ``native``
    is true for the tracker's own relation and false for text syntax in the
    body.
    """

    ref: str
    item_id: str = ""
    state: str = UNKNOWN
    native: bool = False
    issue: IssueLink | None = None


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One listed work item."""

    id: str
    key: str = ""
    url: str = ""
    title: str = ""
    body: str = ""
    #: The canonical status the listing put it under, ``""`` when unlisted.
    status: str = ""
    #: The tracker's own name for its state, when the provider reports one.
    native_status: str = ""
    owners: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    #: The pull request the tracker links to the item, when it knows one.
    pr_url: str = ""
    #: 397: how the code host names the item. ``reference`` is a short token safe
    #: in a branch name and a path; ``pr_line`` is the line a pull request body
    #: carries for it. GitHub Projects: the issue number and ``Closes #N``.
    reference: str = ""
    pr_line: str = ""
    #: GitHub only: the issue behind the item. See the module docstring.
    issue: IssueLink | None = None
    #: 393: the tracker's other fields by name (a priority field, say), as text.
    #: Read-only, like the tuples above. Empty when the provider reports none.
    fields: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}), hash=False)


def work_items_of(board: Any) -> dict[str, WorkItem]:
    """The listing as typed records, keyed by card id, in listing order.

    Reads ``board["items"]`` when the provider supplied records, and otherwise
    translates the GitHub Projects listing. Anything that is not a listing
    yields no items.
    """
    if not isinstance(board, dict):
        return {}
    items = board.get("items")
    if isinstance(items, dict):
        return {str(k): v for k, v in items.items() if isinstance(v, WorkItem)}
    return _from_github_listing(board)


_UNSAFE_IN_REF = re.compile(r"[^A-Za-z0-9._-]+")


def safe_reference(key: str) -> str:
    """*key* as one path segment (397): ``PAY-1`` stays ``PAY-1``.

    Also valid as one segment of a git ref: no ``..`` and no ``.lock`` ending.
    Not injective (``A B-1`` and ``A-B-1`` meet), so a provider whose keys can
    differ only in punctuation should supply its own reference.
    """
    token = _UNSAFE_IN_REF.sub("-", key).replace("..", "-").strip("-.")
    while token.endswith(".lock"):
        token = token.removesuffix(".lock").rstrip("-.")
    return token


def card_status_of(board: Any) -> dict[str, str]:
    """Card id -> canonical status, from the listing's snapshot."""
    snapshot = board.get("snapshot") if isinstance(board, dict) else None
    status: dict[str, str] = {}
    if isinstance(snapshot, dict):
        for column, ids in snapshot.items():
            for item_id in ids if isinstance(ids, list) else []:
                status.setdefault(str(item_id), str(column))
    return status


def parse_text_blockers(body: str) -> list[int]:
    """Issue numbers named by the GitHub text syntax in *body*, deduplicated, in order."""
    if not body:
        return []
    seen: set[int] = set()
    result: list[int] = []
    for match in _DEP_PATTERN.findall(body):
        number = int(match)
        if number > 0 and number not in seen:
            seen.add(number)
            result.append(number)
    return result


def _mapping(board: dict[str, Any], key: str) -> dict[str, Any]:
    value = board.get(key)
    return value if isinstance(value, dict) else {}


def _from_github_listing(board: dict[str, Any]) -> dict[str, WorkItem]:
    """Translate the GitHub Projects listing (snapshot plus side tables)."""
    titles = _mapping(board, "titles")
    descriptions = _mapping(board, "descriptions")
    issue_numbers = _mapping(board, "issue_numbers")
    issue_urls = _mapping(board, "issue_urls")
    content_node_ids = _mapping(board, "content_node_ids")
    pr_urls = _mapping(board, "pr_urls")
    labels = _mapping(board, "item_labels")
    assignees = _mapping(board, "item_assignees")
    native = _mapping(board, "native_blockers")
    field_values = _mapping(board, "item_field_values")
    # 396: the state of each off-board issue the text syntax names, read while listing.
    text_states = {str(k): v for k, v in _mapping(board, "blocker_states").items()}
    status = card_status_of(board)

    # Every id any part of the listing names. The descriptions come first: the
    # dependency check has always walked them in that order, and keeping it keeps
    # the order of the edges it builds.
    ids: dict[str, None] = {}
    for source in (descriptions, titles, issue_numbers, status, native, labels, assignees, content_node_ids):
        for item_id in source:
            ids.setdefault(str(item_id), None)

    number_to_item: dict[int, str] = {}
    for item_id, number in issue_numbers.items():
        if isinstance(number, int) and number > 0:
            number_to_item[number] = str(item_id)

    result: dict[str, WorkItem] = {}
    for item_id in ids:
        raw_number = issue_numbers.get(item_id, 0)
        number = raw_number if isinstance(raw_number, int) else 0
        node_id = str(content_node_ids.get(item_id, "") or "")
        body = str(descriptions.get(item_id, ""))
        blockers: list[Blocker] = [
            Blocker(
                ref=f"#{n}", item_id=number_to_item.get(n, ""), issue=IssueLink(number=n),
                state=blocker_state_from_github(text_states[str(n)]) if str(n) in text_states else UNKNOWN,
            )
            for n in parse_text_blockers(body)
        ]
        for raw in native.get(item_id, []) if isinstance(native.get(item_id), list) else []:
            if not isinstance(raw, dict):
                continue
            n = raw.get("number")
            if not isinstance(n, int) or n <= 0:
                continue
            repo = str(raw.get("repo", ""))
            blockers.append(Blocker(
                ref=f"{repo}#{n}" if repo else f"#{n}",
                item_id=number_to_item.get(n, ""),
                state=CLOSED if str(raw.get("state", "OPEN")).upper() == "CLOSED" else OPEN,
                native=True,
                issue=IssueLink(number=n, repo=repo),
            ))
        result[item_id] = WorkItem(
            id=item_id,
            key=f"#{number}" if number else "",
            url=str(issue_urls.get(item_id, "") or ""),
            title=str(titles.get(item_id, "")),
            body=body,
            status=status.get(item_id, ""),
            owners=tuple(str(a) for a in assignees.get(item_id, []) or []),
            labels=tuple(str(label) for label in labels.get(item_id, []) or []),
            blockers=tuple(blockers),
            pr_url=str(pr_urls.get(item_id, "") or ""),
            reference=str(number) if number else "",
            pr_line=f"Closes #{number}" if number else "",
            issue=IssueLink(number=number, node_id=node_id) if (number or node_id) else None,
            fields=field_text(field_values.get(item_id)),
        )
    return result


def field_text(raw: Any) -> Mapping[str, str]:
    """A provider's field values as a read-only name -> text map (393).

    A value the tracker leaves unset (``None``) is left out, as it always read:
    an item without a value sorts after those with one.
    """
    if not isinstance(raw, dict):
        return MappingProxyType({})
    return MappingProxyType({str(k): str(v) for k, v in raw.items() if v is not None})


def listed_text(board: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Card id -> title and card id -> body, exactly as the listing supplied them.

    Kept as maps, not read off the records, so a caller can tell a field the
    listing supplied empty (apply it empty) from one it did not supply (leave
    the card's value alone). Typed records supply both for every item; the
    GitHub Projects listing supplies what its ``titles`` and ``descriptions``
    hold. Moved here from ``check_board`` (393) so no graph node reads the
    GitHub listing's own keys.
    """
    if not isinstance(board, dict):
        return {}, {}
    if isinstance(board.get("items"), dict):
        listed = work_items_of(board)
        return {k: v.title for k, v in listed.items()}, {k: v.body for k, v in listed.items()}
    return _mapping(board, "titles"), _mapping(board, "descriptions")


def unlisted_text_blockers(board: dict[str, Any], status: str = "TODO") -> list[int]:
    """Issue numbers the GitHub text syntax names that no listed item has (396).

    Only in the bodies of items listed under *status*: To Do is where the
    dependency filter acts, so reading more would spend a REST call per poll
    for nothing. In the order the bodies name them, each once, never an item's
    own number. These are the blockers whose state the listing cannot see on
    the board.
    """
    issue_numbers = _mapping(board, "issue_numbers")
    columns = card_status_of(board)
    # An item in a column coordinare has no name for is not on the board for this
    # purpose: nothing says whether it is done, so its state is read like any other.
    listed = {n for item_id, n in issue_numbers.items() if isinstance(n, int) and n > 0 and item_id in columns}
    in_status = {item_id for item_id, column in columns.items() if column == status}
    result: dict[int, None] = {}
    for item_id, body in _mapping(board, "descriptions").items():
        if item_id not in in_status:
            continue
        own = issue_numbers.get(item_id, 0)
        for number in parse_text_blockers(str(body)):
            if number != own and number not in listed:
                result.setdefault(number, None)
    return list(result)


def board_metadata(board: Any) -> dict[str, dict[str, Any]]:
    """The per-card maps coordinare keeps in state for the dashboard and the prompts.

    Keyed by the state key each one is stored under. Derived from the typed
    listing, so every provider fills them the same way; a field the provider
    does not supply is simply absent from its map.
    """
    listed = work_items_of(board)
    return {
        "_board_titles": {item_id: wi.title for item_id, wi in listed.items()},
        "_board_issue_numbers": {
            item_id: wi.issue.number for item_id, wi in listed.items() if wi.issue is not None and wi.issue.number
        },
        "_board_issue_urls": {item_id: wi.url for item_id, wi in listed.items() if wi.url},
        "_board_pr_urls": {item_id: wi.pr_url for item_id, wi in listed.items() if wi.pr_url},
        "_board_item_labels": {item_id: list(wi.labels) for item_id, wi in listed.items()},
    }
