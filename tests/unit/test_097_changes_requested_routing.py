"""#97: a human CHANGES_REQUESTED reaches the implementer the first time.

Observed on conductor-e2e-sample PR 69: the first CHANGES_REQUESTED was
routed by the wording of its body to the reviewing and qa stages, which
re-ran on unchanged code and re-approved it. Only a second request reached
the implementer.
"""
from __future__ import annotations

from typing import Any

import pytest
import structlog.testing

from coordinare.graph.nodes.classify_human_feedback import (
    _human_requested_changes,
    classify_human_feedback,
)
from coordinare.graph.state import initial_state

_LIFECYCLE = ["assessing", "architecting", "implementing", "reviewing", "security", "qa", "documenting", "closing_review"]

# The review body from the incident reads as a QA and review concern.
_CASEFOLD = "The tests do not pin the casefold contract; coverage of the naming convention is missing."


def _review(state: str, author_type: str, body: str = _CASEFOLD, review_id: str = "RVW_1") -> dict[str, Any]:
    return {"id": review_id, "state": state, "author_type": author_type, "author_login": "jason",
            "body": body, "comments": []}


async def _route(reviews: list[dict[str, Any]], lifecycle: list[str] | None = None) -> tuple[str, list[dict]]:
    state = initial_state()
    state["pending_reviews"] = reviews
    state["lifecycle_sequence"] = list(lifecycle or _LIFECYCLE)
    state["current_card"] = {"id": "CARD_97", "status": "IN_REVIEW"}
    with structlog.testing.capture_logs() as logs:
        result = await classify_human_feedback(state)
    return str(result.get("performer_stage")), [e for e in logs if e["event"] == "classify_human_feedback.routing"]


@pytest.mark.asyncio
async def test_human_changes_requested_goes_to_the_implementer_even_when_it_reads_like_qa() -> None:
    stage, logs = await _route([_review("CHANGES_REQUESTED", "HUMAN")])
    assert stage == "implementing"
    assert logs[0]["human_changes_requested"] is True
    # The wording still classified as later-stage concerns; the state overrode it.
    assert "implementation" not in logs[0]["concerns"]


@pytest.mark.asyncio
async def test_the_same_wording_as_a_plain_comment_is_still_routed_by_concern() -> None:
    """Only the CHANGES_REQUESTED state forces the implementer."""
    stage, logs = await _route([_review("COMMENTED", "HUMAN")])
    assert stage in {"reviewing", "qa"}
    assert logs[0]["human_changes_requested"] is False


@pytest.mark.asyncio
async def test_a_trusted_bots_change_request_does_not_force_the_implementer() -> None:
    stage, logs = await _route([_review("CHANGES_REQUESTED", "TRUSTED_BOT")])
    assert stage in {"reviewing", "qa"}
    assert logs[0]["human_changes_requested"] is False


@pytest.mark.asyncio
async def test_an_architecture_concern_can_still_restart_earlier_than_the_implementer() -> None:
    body = "The module design has too much coupling; rethink the abstraction."
    stage, _ = await _route([_review("CHANGES_REQUESTED", "HUMAN", body)])
    assert stage == "architecting"


@pytest.mark.asyncio
async def test_one_change_request_among_comments_is_enough() -> None:
    stage, _ = await _route([
        _review("COMMENTED", "HUMAN", "Docs: the README needs the new flag.", "RVW_A"),
        _review("CHANGES_REQUESTED", "HUMAN", "Docs: the README needs the new flag.", "RVW_B"),
    ])
    assert stage == "implementing"


@pytest.mark.asyncio
async def test_lifecycle_without_an_implementer_is_left_to_the_classifier() -> None:
    stage, _ = await _route([_review("CHANGES_REQUESTED", "HUMAN")], lifecycle=["reviewing", "qa"])
    assert stage in {"reviewing", "qa"}


@pytest.mark.parametrize(("reviews", "expected"), [
    ([], False),
    ([{"state": "CHANGES_REQUESTED", "author_type": "HUMAN"}], True),
    ([{"state": "CHANGES_REQUESTED", "author_type": "TRUSTED_BOT"}], False),
    ([{"state": "CHANGES_REQUESTED"}], False),
    ([{"state": "APPROVED", "author_type": "HUMAN"}], False),
    ([{"state": "COMMENTED", "author_type": "HUMAN"}, {"state": "CHANGES_REQUESTED", "author_type": "HUMAN"}], True),
])
def test_human_requested_changes(reviews, expected) -> None:
    assert _human_requested_changes(reviews) is expected
