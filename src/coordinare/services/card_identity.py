"""Reconciling GitHub's two names for one card (spec 153, issue #232).

GitHub Projects addresses a card by node id everywhere coordinare touches it, and
by *issue number* in the REST comments API. That is two identifiers for one
thing, and it is the reason spec 149 could not put comment reading behind the
board protocol: a method promising a ``card_id`` would have been false on the
only implementation.

Polling already fetches the pairing for every card on the board, so remembering
it makes the translation free. This mixin is that memory. It lives apart from
``GitHubService`` because the bench's ``FakeGitHubService`` stands in for the
real one and must translate identically -- when it did not, comment routing in
the bench silently returned nothing while every unit test passed.

Anything above the provider layer should never see either identifier: callers
pass the card id coordinare already holds, and the translation happens here.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class CardIdentityMap:
    """Remembers ``card id -> issue number`` for the cards a board poll returned.

    Held per service instance, which is per symphony. That is not tidiness: two
    symphonies on different repositories can hold the same issue *number* for
    different cards, so one shared map would resolve one symphony's card to the
    other's issue.

    An entry can never become wrong -- a card does not change identity -- so
    there is no invalidation and no TTL. It can only be missing, which the
    fallback covers.
    """

    def _issue_number_cache(self) -> dict[str, int]:
        known: dict[str, int] | None = getattr(self, "_card_issue_numbers", None)
        if known is None:
            known = {}
            self._card_issue_numbers = known
        return known

    def _remember_issue_numbers(self, issue_numbers: dict[str, int]) -> None:
        """Record the pairing a board poll just produced. Zeroes are not pairings.

        The asymmetry with ``issue_number_for_card`` -- which *does* store a 0 -- is
        deliberate and protective. A poll reports 0 for anything it could not read a
        number from, including a transient partial response. Letting that overwrite a
        good entry would turn a blip into a card coordinare believes is unreadable.
        A lookup aimed at one card is evidence about that card; a poll's 0 is not.
        """
        self._issue_number_cache().update({k: v for k, v in issue_numbers.items() if v})

    async def get_issue_details(self, card_id: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def issue_number_for_card(self, card_id: str) -> int | None:
        """The issue number for a card id, without a request where possible.

        Polling supplies the pairing for every card on the board, so the common
        path costs nothing. A card read before the first poll of a fresh process,
        or one that has left the board, falls back to a details lookup -- and the
        result is remembered, because otherwise such a card would pay for that
        lookup on every cycle rather than once.

        Returns ``None`` rather than raising when the card cannot be resolved.
        Callers already treat "no comments" as the outcome of a failed read, and
        a board hiccup must not stall the cycle.
        """
        known = self._issue_number_cache()
        cached = known.get(card_id)
        if cached:
            return cached
        if card_id in known:
            # A remembered 0 means "this card has no issue number" -- a draft issue
            # in a project is the ordinary case. Without recording that, every cycle
            # would repeat the lookup for a card that will never resolve, which is
            # the per-cycle API cost this class exists to avoid. A later poll can
            # still overwrite it: if the draft is converted, the poll supplies a real
            # number and _remember_issue_numbers replaces the 0.
            return None
        try:
            details = await self.get_issue_details(card_id)
        except Exception as exc:
            # Remembered for the same reason a missing number is: comment routing runs
            # once per cycle, so a card whose lookup throws would otherwise retry the
            # failing call forever -- worst precisely when the API is already unhappy.
            # The next poll heals it: any card still on the board gets a real number,
            # which overwrites this.
            known[card_id] = 0
            logger.warning("issue_number_for_card.lookup_failed", card_id=card_id, error=str(exc))
            return None
        number = details.get("number") if isinstance(details, dict) else None
        if not isinstance(number, int) or not number:
            known[card_id] = 0
            return None
        known[card_id] = number
        return number
