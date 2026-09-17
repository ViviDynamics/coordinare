"""Which cards on the board are this coordinare's to work (spec 160).

Eligibility is a **union of two opt-ins**:

* ``assignee_filter`` opts in cards assigned to the login it names.
* ``include_unassigned`` opts in cards carrying no assignee at all.

Set neither and every card is eligible -- what every deployment had before spec
050, unchanged. Set either and only their union is: a card assigned to somebody
else is never in it, which is the whole point.

The union matters because a GitHub App **cannot be assigned to an issue**.
Verified against the live repo rather than assumed: ``vivi-coordinare[bot]`` is a
real Bot user, and ``addAssigneesToAssignable`` on a real issue answers
``FORBIDDEN: Could not assign agent: vivi-coordinare[bot] cannot be assigned to
issues or pull requests`` even with an admin PAT holding every scope. GitHub
allows bot assignees only for agents it enables itself. So a deployment
authenticating as an App has no login to name, and ``include_unassigned`` alone
is how it says "work what nobody has claimed, leave what a human has claimed" --
which makes assigning a card to yourself the way to take it off coordinare.

Spec 050 introduced the filter but applied it in one place -- TODO pickup -- so a
card a human had already dragged into *In progress*, or one sitting in *Blocked*,
was adopted regardless of who it was assigned to. This module is the single
answer to "is this card mine", so every adoption path in ``check_board`` asks the
same question.

Not eligible is not hidden. Only *adoption* consults this: the board snapshot
still carries every card, the dashboard still counts them, and dependency
resolution can still read across them. Coordinare sees other people's cards and
does not act on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class OwnershipPolicy:
    """Resolved card-ownership rules for one symphony.

    ``login`` is already lowercased, matching how ``poll_board`` normalises the
    assignee logins it returns; comparing anywhere else would make the match
    case-sensitive by accident.
    """

    login: str = ""
    include_unassigned: bool = False

    @property
    def active(self) -> bool:
        """Whether the policy narrows the board at all.

        Either opt-in activates it. With neither, every card is eligible.

        ``include_unassigned`` alone is a complete policy rather than a modifier:
        the eligible set is ``{assigned to login} | {unassigned}``, and with no
        login the first half is empty, leaving "unassigned only". That is the
        shape a deployment authenticating as a GitHub App needs, because it has
        no assignable login to name.
        """
        return bool(self.login) or self.include_unassigned


def ownership_policy(config: Any) -> OwnershipPolicy:
    """Read the policy off a symphony's effective config.

    ``getattr`` rather than attribute access because ``state["config"]`` is
    typed loosely and is ``None`` on some early cycles.
    """
    if config is None:
        return OwnershipPolicy()
    raw_login = getattr(config, "assignee_filter", None)
    login = str(raw_login).strip().lower() if isinstance(raw_login, str) else ""
    return OwnershipPolicy(
        login=login,
        include_unassigned=bool(getattr(config, "include_unassigned", False)),
    )


def owns_card(
    policy: OwnershipPolicy, card_id: str, item_assignees: dict[str, list[str]],
) -> bool:
    """Whether coordinare may adopt *card_id*.

    A card the last poll did not report assignees for is treated as unassigned
    rather than as a match: an absent entry is the same fact as an empty one,
    and guessing the other way would let an unknown card through the filter.
    """
    if not policy.active:
        return True
    assignees = item_assignees.get(card_id) or []
    # Guarded on a non-empty login: with ``include_unassigned`` alone the login
    # is "", and an unguarded ``"" in assignees`` would match any card whose
    # assignee list carried an empty string -- admitting somebody else's card
    # through the half of the union that is supposed to be empty.
    if policy.login and policy.login in assignees:
        return True
    return policy.include_unassigned and not assignees


def filter_owned(
    policy: OwnershipPolicy,
    card_ids: list[str],
    item_assignees: dict[str, list[str]],
    *,
    context: str = "",
) -> list[str]:
    """The subset of *card_ids* coordinare owns, order preserved.

    Logs once per call with the number dropped, rather than once per card: a
    board with fifty other people's cards on it should not produce fifty lines
    every poll.
    """
    if not policy.active:
        return list(card_ids)
    owned = [cid for cid in card_ids if owns_card(policy, cid, item_assignees)]
    skipped = len(card_ids) - len(owned)
    if skipped:
        logger.info(
            "card_ownership.filtered",
            context=context,
            skipped=skipped,
            assignee_filter=policy.login,
            include_unassigned=policy.include_unassigned,
        )
    return owned
