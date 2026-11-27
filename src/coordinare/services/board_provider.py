"""Where coordinare's work comes from, independent of GitHub (issue #203).

Coordinare reads cards from GitHub Projects and nowhere else, so a team whose work
lives in Jira cannot run it — even though dispatching performers, opening PRs and
driving reviews have nothing to do with where the board is.

**This is the board seam only.** A Jira board pairs with a git host: Jira can
replace where the work is tracked, never where the code lives. Pull requests,
reviews, mergeability, diffs and branches stay GitHub, and abstracting both at
once would produce a seam shaped by neither.

## Why the protocol is this small

Derived from what coordinare *calls*, not from what ``GitHubService`` offers. The
service has 48 methods and 26 are called externally; only these five are board
operations that another provider could meaningfully implement. Building the
protocol from the service's surface would have made it three times the size and
dragged the code host in with it.

## What is deliberately NOT here

``ensure_labels_exist``, ``add_labels``, ``list_open_issues`` and
``check_issue_state`` all read as board-ish and are not. Their signatures give
them away — ``(owner, repo, ...)``, GitHub node ids for labels. They are
operations on *a GitHub repository's issues*: the advocate scanning a repo to
nominate candidate work, and dependency checks on blocking issues. On Jira the
board itself is the source of work and there is no separate repo to scan, so the
equivalent is a different feature rather than a different implementation. Putting
them here would mean every future provider implementing something meaningless to
it.

Reading a card's comments *is* here, but it was not always, and the reason is
worth keeping. GitHub calls one card two things: a node id everywhere, and an
issue number for the comments API. Spec 149 removed the operation rather than
ship a ``card_id`` parameter that was false on the only implementation. Spec 153
(#232) closed it by noticing that ``poll_board`` already fetches the pairing, so
the adapter can translate for nothing -- which is why the translation lives in
the adapter and the map lives on the service, and why no caller above this file
has ever heard of an issue number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from coordinare.doctor import CheckResult
from coordinare.doctor_status import Status
from coordinare.services.card_identity import CardIdentityMap
from coordinare.services.github import BOARD_COLUMN_TO_STATUS
from coordinare.services.work_items import unlisted_text_blockers

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = structlog.get_logger(__name__)

#: A move target, not a column (#396): the item is not work and leaves the board's
#: queue for good. It is a state rather than a sixth operation so the protocol
#: keeps the surface specs 149 and 153 pinned. A provider lands it wherever its
#: tracker records "will not do": GitHub Projects closes the issue and moves the
#: item to DONE, and a tracker with no such state lists it under DONE. Never a
#: listing's status, so no listing reports it.
NOT_WORK = "NOT_WORK"

#: A comment id, and so the cursor coordinare keeps per work item (398). Opaque: it
#: is whatever the provider issued (GitHub's integer comment id, a tracker's string
#: key, a timestamp, a page token), and coordinare stores it, hands it back and
#: compares it for equality. It never sorts it, takes a maximum of it or reads it as
#: a number; the provider's order, oldest first, is the order comments are handled in.
type CommentCursor = int | str

#: The canonical states every provider must map (399). Without them coordinare
#: cannot take work (To Do), run it (In Progress) or finish it (Done).
REQUIRED_STATES: tuple[str, ...] = ("TODO", "IN_PROGRESS", "DONE")

#: The canonical states a provider may leave unmapped (399), and what then happens.
#: A move to one is refused (``MoveOutcome.refused``), logged, and the item stays
#: where it was:
#:
#: - In Review: the item stays in In Progress while its pull request is reviewed.
#:   Coordinare follows the review on the pull request, not on the board, so the
#:   run goes on to merge and Done as usual.
#: - Blocked: a blocked item stays in its current state while coordinare still
#:   holds it as blocked. A person cannot block an item from the tracker either,
#:   since coordinare reads a person's block from the Blocked state.
#:
#: Backlog is optional as well; nothing is ever moved there but a declined item.
#: GitHub Projects still requires all five columns, as it always has.
OPTIONAL_STATES: tuple[str, ...] = ("IN_REVIEW", "BLOCKED")

_STATE_LABELS = {
    "TODO": "To Do", "IN_PROGRESS": "In Progress", "DONE": "Done", "IN_REVIEW": "In Review", "BLOCKED": "Blocked",
}

_UNMAPPED_OPTIONAL = {
    "IN_REVIEW": (
        "No state maps to In Review: a work item stays in In Progress while its pull request "
        "is reviewed, and coordinare follows the review on the pull request.",
        "Optional: map a state to In Review to show review on the board.",
    ),
    "BLOCKED": (
        "No state maps to Blocked: a blocked work item stays in its current state while "
        "coordinare holds it as blocked, and a person cannot block an item from the tracker.",
        "Optional: map a state to Blocked so blocked work shows on the board and a person can block an item there.",
    ),
}


def state_checks(mapped: Iterable[str]) -> list[CheckResult]:
    """The start checks for the canonical states a provider maps (399).

    A provider's ``preflight`` can return these for its own state mapping: a
    failure for each required state it lacks, a warning saying what happens for
    each optional one, or one pass when every state is mapped.
    """

    have = set(mapped)
    missing = [state for state in REQUIRED_STATES if state not in have]
    results: list[CheckResult] = []
    if missing:
        names = ", ".join(_STATE_LABELS[state] for state in missing)
        results.append(CheckResult(
            "work item states",
            Status.FAIL,
            f"No state maps to {names}. Coordinare cannot take, run or finish work without "
            "To Do, In Progress and Done.",
            "In the tracker's workflow, map a state to each of To Do, In Progress and Done.",
        ))
    for state in OPTIONAL_STATES:
        if state not in have:
            detail, fix = _UNMAPPED_OPTIONAL[state]
            results.append(CheckResult("work item states", Status.WARN, detail, fix))
    return results or [CheckResult("work item states", Status.OK, "Every coordinare state maps to a state on the board.")]


@dataclass(frozen=True, slots=True)
class MoveOutcome:
    """The result of asking a board to move a card.

    A result rather than ``None`` because on some boards a move can legitimately
    fail. GitHub Projects always accepts one; Jira workflows can forbid a
    transition from the card's current status, and that is a normal answer about
    the board's rules — not a fault, not a network error, and not something that
    should stop a cycle.

    Designing for it now costs nothing. Retrofitting it would mean revisiting
    every call site, because today they all assume success by ignoring the return.
    """

    moved: bool
    reason: str = ""

    @classmethod
    def ok(cls) -> MoveOutcome:
        return cls(moved=True)

    @classmethod
    def refused(cls, reason: str) -> MoveOutcome:
        return cls(moved=False, reason=reason)


@runtime_checkable
class BoardProvider(Protocol):
    """The board operations coordinare performs. Implement these and it runs.

    Card ids are **opaque**. Nothing here may assume one addresses anything on the
    code host: on GitHub Projects a card is an issue in the PR's own repository,
    and on Jira a key like ``PROJ-123`` has no such relationship. Any provider is
    free to use whatever identifier it likes as long as it accepts its own back.

    Statuses crossing this boundary are coordinare's canonical ones
    (``CardStatus``), never a provider's native lane name. Translating between the
    two is the provider's job, which is what lets the graph reason about a board
    it knows nothing about.
    """

    async def poll_board(self) -> dict[str, Any]:
        """A snapshot of the board, keyed by canonical status.

        Returns at minimum ``{"snapshot": {status: [card_id, ...]}}``. Providers
        may include richer per-card data alongside it; consumers read what they
        need and tolerate its absence.
        """
        ...

    async def move_card(self, card_id: str, status: str) -> MoveOutcome:
        """Move a card to a canonical status.

        Returns an outcome rather than raising on refusal — see ``MoveOutcome``.
        Genuine failures (the board is unreachable, credentials are wrong) remain
        exceptions, because those are faults rather than answers.
        """
        ...

    async def get_card(self, card_id: str) -> dict[str, Any]:
        """The card's own content — title, body, and whatever else it carries."""
        ...

    async def add_card_comment(self, card_id: str, body: str) -> dict[str, Any]:
        """Post a comment on the card, in plain text.

        Plain text on purpose. Jira stores comments as ADF and Trello as Markdown;
        conversion belongs to the provider rather than to every caller.
        """
        ...

    async def get_card_comments(
        self, card_id: str, since_id: CommentCursor | None = None,
    ) -> list[dict[str, Any]]:
        """Comments on the card, oldest first, optionally only those after *since_id*.

        The watermark keeps coordinare from reprocessing feedback it has already
        acted on. It is a comment id this provider issued, handed back exactly as
        issued (``CommentCursor``); coordinare never reads meaning into it. A cursor
        the provider does not recognise should return every comment rather than
        none: coordinare keeps the ids it processed, so a replay is dropped, where
        a lost comment is gone.

        The cursor coordinare hands back is always the id of the last comment it
        handled, so a tracker whose native position is a page token or an
        "updated since" time maps that comment id back to its position itself.

        Each comment is ``{"id", "author", "body", "created_at"}`` -- four plain
        fields, not a host's payload. That is already the shape coordinare works
        in, so a provider normalises to it rather than coordinare learning a new
        one per board.

        ``"id"`` is an ``int`` or a non-empty ``str``; a timestamp goes as a string.
        A comment with any other id is skipped, since it can be neither
        deduplicated nor handed back as a cursor.

        Returns ``[]`` rather than raising when the card cannot be found or the
        read fails. Comment routing is how a human redirects work in flight; a
        board hiccup should delay that, not stall the cycle.
        """
        ...


@runtime_checkable
class ProviderPreflight(Protocol):
    """A provider that can say, before the daemon starts, whether it is ready (399).

    Separate from ``BoardProvider`` so the board operations stay the five specs
    149 and 153 pinned: ``init``, ``validate`` and ``doctor`` call this, the graph
    never does. A provider without it is reported as having no start checks.
    """

    async def preflight(self) -> list[CheckResult]:
        """Start checks in the doctor format: name, status, message and fix.

        A check's ``fix`` is shown with it, so a failure says what to change. Raise
        for a fault (the tracker is unreachable): coordinare retries transient
        faults within its probe budget and reports the rest, through
        ``preflight_fault`` when the provider has one.
        """
        ...


class GitHubProjectsBoardProvider:
    """Today's behaviour, reached through the protocol.

    An adapter, deliberately: it holds a ``GitHubService`` and forwards. Nothing
    is reimplemented, so the code that runs is the code that ran before, and
    "no behaviour change" is a property of the structure rather than a claim to
    be tested for.

    The renames live here. ``GitHubService`` keeps ``get_issue_details`` and
    friends — on GitHub Projects a card really *is* an issue, so those names are
    right in that context. The protocol speaks of cards because the next provider
    will not have issues, and the translation costs one line each.
    """

    def __init__(self, github: Any, repo: str = "") -> None:
        self._github = github
        self._repo = repo

    @property
    def repo(self) -> str:
        """The ``owner/name`` the board's ``#N`` references live in (396).

        The service's own (``GitHubService`` carries its symphony's org and
        repository), or else the one given, for a service that does not say.
        The service wins because a caller may hand in one symphony's service
        beside another's config. Empty when neither knows it, and then no
        blocker state is read.
        """
        org, name = getattr(self._github, "org", ""), getattr(self._github, "project_name", "")
        own = f"{org}/{name}" if isinstance(org, str) and isinstance(name, str) and org and name else ""
        return own or self._repo

    @property
    def github(self) -> Any:
        """The underlying service.

        Present because a caller may legitimately need the code host as well, and
        threading two references through every mixed call site at once would be a
        far larger change than this spec is for. Reaching through it for a *board*
        operation defeats the seam; reaching through it for a pull request does
        not.
        """
        return self._github

    async def preflight(self) -> list[CheckResult]:
        """Today's board column check, under its names and messages (399)."""

        await self._github.initialize()
        options = self._github.field_cache.get("status_option_ids", {})
        found = {BOARD_COLUMN_TO_STATUS.get(name) for name in options}
        # GitHub Projects requires every column, including the optional ones.
        missing = {*REQUIRED_STATES, *OPTIONAL_STATES} - found
        if missing:
            return [CheckResult(
                "board columns",
                Status.FAIL,
                f"The board is missing columns: {', '.join(sorted(missing))}.",
                "In the board's Status field add To Do (or Ready), In Progress, In Review, Blocked and Done options.",
            )]
        return [CheckResult("GitHub board", Status.OK, "Board is reachable and all lifecycle Status columns exist.")]

    def preflight_fault(self, status: Status) -> CheckResult:
        """What to report when the board cannot be read at all."""

        return CheckResult(
            "GitHub board",
            status,
            "Could not read the project board and its Status field after bounded retries.",
            "Correct github_org, github_project_number and github_graphql_url; grant Projects access and add a Status field with To Do, In Progress, In Review, Blocked and Done.",
        )

    async def poll_board(self) -> dict[str, Any]:
        board: dict[str, Any] = await self._github.poll_board()
        states = await self._blocker_states(board)
        return {**board, "blocker_states": states} if states else board

    async def _blocker_states(self, board: dict[str, Any]) -> dict[int, str]:
        """The state of each off-board issue a body names as a blocker (396).

        The listing carries blocker state, so the dependency check reads it from
        there rather than asking GitHub itself. The same call it made
        (``check_issue_state``), one per issue. A read that raises is reported as
        GitHub's own transient answer, so one bad issue cannot fail the poll.
        """
        check = getattr(self._github, "check_issue_state", None)
        repo = self.repo
        if not callable(check) or not repo:
            return {}
        states: dict[int, str] = {}
        for number in unlisted_text_blockers(board):
            try:
                states[number] = str(await check(repo, number))
            except Exception as exc:
                logger.warning("board.blocker_state_failed", issue_number=number, error=str(exc))
                states[number] = "api_error"
        return states

    async def move_card(self, card_id: str, status: str) -> MoveOutcome:
        # GitHub Projects has no workflow rules, so a move that reaches the API
        # succeeds. Anything else raises, and raising is right: that is a fault,
        # not the board declining.
        if status == NOT_WORK:
            await self._close_issue(card_id)
            # Closing the issue does not move the project item: without this the
            # next poll still sees the card in TODO and admits it again.
            status = "DONE"
        await self._github.move_card(card_id, status)
        return MoveOutcome.ok()

    async def _close_issue(self, card_id: str) -> None:
        """Close the card's issue, as the assessor's decline always has (410).

        A failed close is logged and the item still moves to DONE, so the card
        leaves the queue either way.
        """
        try:
            await self._github.close_issue(self._issue_id(card_id))
        except Exception as exc:
            logger.warning("board.not_work_close_failed", card_id=card_id, error=str(exc))

    async def get_card(self, card_id: str) -> dict[str, Any]:
        card: dict[str, Any] = await self._github.get_issue_details(self._issue_id(card_id))
        return card

    async def add_card_comment(self, card_id: str, body: str) -> dict[str, Any]:
        posted: dict[str, Any] = await self._github.add_comment(self._issue_id(card_id), body)
        return posted

    def _issue_id(self, card_id: str) -> str:
        """The issue node id behind a listed card id (#400).

        GitHub reads and comments on a card through its issue. The pairing comes
        from the last poll, so it costs nothing; an id the poll did not list (an
        issue node id a caller already holds) passes through unchanged.
        """
        if isinstance(self._github, CardIdentityMap):
            resolved = self._github.content_id_for_card(card_id)
            # A stand-in for the service (a spec'd mock) answers with a non-string;
            # the id the caller holds is then the best there is.
            if isinstance(resolved, str) and resolved:
                return resolved
        return card_id

    async def get_card_comments(
        self, card_id: str, since_id: CommentCursor | None = None,
    ) -> list[dict[str, Any]]:
        # The one place that knows GitHub calls a card two things. Callers pass the
        # same card id they use for every other operation; the translation happens
        # here and is free for any card the last poll saw.
        number = await self._github.issue_number_for_card(card_id)
        if number is None:
            logger.warning("board.card_not_resolvable", card_id=card_id)
            return []
        comments: list[dict[str, Any]] = await self._github.get_issue_comments(
            number, since_id=self._github_comment_id(card_id, since_id),
        )
        # 398: coordinare handles comments in the order a provider returns them.
        # GitHub comment ids rise with time, so id order is creation order.
        return sorted(comments, key=_github_comment_order)

    @staticmethod
    def _github_comment_id(card_id: str, cursor: CommentCursor | None) -> int | None:
        """The GitHub comment id a cursor names (398).

        GitHub issues integer ids. One that went through text (a log line, a JSON
        field) is read back as the same number. A cursor GitHub never issued names
        no comment here, so every comment is returned and coordinare's processed
        ids drop the replay: a lost comment could not be recovered.
        """
        if cursor is None:
            return None
        if isinstance(cursor, int) and not isinstance(cursor, bool):
            return cursor
        if isinstance(cursor, str) and cursor.isascii() and cursor.isdigit():
            return int(cursor)
        logger.warning("board.unknown_comment_cursor", card_id=card_id, cursor=str(cursor))
        return None


def _github_comment_order(comment: dict[str, Any]) -> tuple[int, int]:
    """Sort key for GitHub comments: by id, with any id that is not a number last, as given."""
    cid = comment.get("id")
    if isinstance(cid, int) and not isinstance(cid, bool):
        return (0, cid)
    return (1, 0)


def board_of(state: Any, service: Any = None) -> BoardProvider | None:
    """The board for this graph state, deriving one if none was set.

    Call sites use this rather than reading ``state["board_provider"]`` directly,
    because a state that carries only a ``github_service`` still has a board — it
    is that service. Without the fallback, every existing test that injects a fake
    GitHub service would have to be taught about a second key, and those tests are
    the evidence that this refactor changed no behaviour. Editing them to make the
    refactor pass would discard exactly the proof that it was safe.

    It is not only a migration shim. A deployment that configures no board
    provider genuinely has GitHub as its board, and saying so here is better than
    every call site coping with ``None``.

    Returns ``None`` only when there is no board at all, which callers already
    handle: they skip the update rather than failing the cycle.
    """
    provider: BoardProvider | None = state.get("board_provider")
    if provider is not None:
        return provider
    # *service* is for helpers that receive the GitHub service as an ARGUMENT
    # rather than reading it from state. Preferring it over the state key matters:
    # a caller that was handed a specific service meant that one, and quietly
    # substituting state's would change which board is written to. That is not
    # hypothetical — it is how this function's first version broke a recovery test
    # that passes its fake as a parameter.
    github = service if service is not None else state.get("github_service")
    if github is None:
        return None
    return GitHubProjectsBoardProvider(github, repo=_configured_repo(state.get("config")))


def _configured_repo(config: Any) -> str:
    """``owner/name`` from the symphony's config, as the dependency check builds it."""
    org, name = getattr(config, "github_org", ""), getattr(config, "project_name", "")
    return f"{org}/{name}" if isinstance(org, str) and isinstance(name, str) and org and name else ""


async def move_card_or_warn(
    provider: BoardProvider | None, card_id: str, status: str,
) -> bool:
    """Move the card, and say something when the board declines.

    Call sites use this rather than ``provider.move_card`` directly so that a
    refusal cannot pass silently. Every existing site already wraps the move in
    ``try/except`` and warns when it raises; a board that declines in an orderly
    way deserves the same visibility, and without this it would get none -- the
    call would return a ``MoveOutcome`` that nobody read, and coordinare would
    carry on believing the card had moved.

    Returns whether the card moved, for the caller that wants to branch on it.
    Faults still raise, and callers still catch them: a refusal is not a fault.

    A missing board raises rather than returning quietly. That is what the old
    ``await github.move_card(...)`` did when the service was ``None``, and every
    call site already has a handler for it -- some of which do more than log.
    Swallowing it here would skip that work without a word.
    """
    if provider is None:
        raise AttributeError("no board provider configured")
    outcome = await provider.move_card(card_id, status)
    if not outcome.moved:
        logger.warning(
            "board.move_refused",
            card_id=card_id,
            status=status,
            reason=outcome.reason,
        )
    return outcome.moved
