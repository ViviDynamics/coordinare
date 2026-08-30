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

Reading a card's comments is not here either, and that one is a genuine gap
rather than a category error. Coordinare addresses comment reads by GitHub *issue
number* (``get_issue_comments(issue_number)``) while it addresses every other
card operation by node id. A protocol method taking ``card_id`` would therefore
be a lie on GitHub unless the adapter resolved node id to number, which costs an
API call per cycle -- a behaviour change, and this extraction's whole warrant is
that it makes none. So comment routing stays on the code host for now, and a
non-GitHub board cannot yet route issue comments. Tracked separately; the fix is
to give cards a single id model, not to widen this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)


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

    def __init__(self, github: Any) -> None:
        self._github = github

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

    async def poll_board(self) -> dict[str, Any]:
        board: dict[str, Any] = await self._github.poll_board()
        return board

    async def move_card(self, card_id: str, status: str) -> MoveOutcome:
        # GitHub Projects has no workflow rules, so a move that reaches the API
        # succeeds. Anything else raises, and raising is right: that is a fault,
        # not the board declining.
        await self._github.move_card(card_id, status)
        return MoveOutcome.ok()

    async def get_card(self, card_id: str) -> dict[str, Any]:
        card: dict[str, Any] = await self._github.get_issue_details(card_id)
        return card

    async def add_card_comment(self, card_id: str, body: str) -> dict[str, Any]:
        posted: dict[str, Any] = await self._github.add_comment(card_id, body)
        return posted


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
    return GitHubProjectsBoardProvider(github)


async def move_card_or_warn(
    provider: BoardProvider | None, card_id: str, status: str
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
