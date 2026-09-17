"""Spec 126 — feedback-ledger stamping (contract L1-L3) + disposition
application (I4) + round resolution (D2/D3).

Contract: specs/126-terminal-success-floors/contracts/success-floors.md
"""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_performer import (
    _apply_feedback_dispositions,
    _resolve_dispute_round,
    _stamp_feedback_bounce,
    monitor_performer,
)
from coordinare.graph.state import initial_state


def _state() -> dict:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_L", "status": "IN_PROGRESS"}
    return state


# ---------------------------------------------------------------------------
# L1 — stamping assigns ids/raiser/origin and enriches relay entries
# ---------------------------------------------------------------------------


def test_stamp_assigns_monotonic_ids_and_enriches_items() -> None:
    state = _state()
    enriched = _stamp_feedback_bounce(
        state,
        [{"body": "fix margin", "author_login": "rev"},
         {"body": "rename var", "author_login": "rev"}],
        raiser="reviewing",
        origin_sha="abc123",
    )

    assert [e["id"] for e in enriched] == ["fb-1", "fb-2"]
    assert all(e["raiser"] == "reviewing" for e in enriched)
    assert all(e["re_raised"] is False for e in enriched)
    ledger = state["feedback_ledger"]
    assert [r["id"] for r in ledger] == ["fb-1", "fb-2"]
    assert all(r["origin_sha"] == "abc123" for r in ledger)
    assert all(r["round_status"] == "current" for r in ledger)
    assert state["feedback_origin_sha"] == "abc123"
    assert state["noop_success_retries"] == 0


def test_stamp_digests_bodies_to_200_chars() -> None:
    state = _state()
    _stamp_feedback_bounce(
        state, [{"body": "x" * 500}], raiser="qa", origin_sha="abc",
    )
    assert len(state["feedback_ledger"][0]["body_digest"]) == 200


def test_ids_continue_across_rounds() -> None:
    state = _state()
    _stamp_feedback_bounce(state, [{"body": "a"}], raiser="reviewing", origin_sha="s1")
    enriched = _stamp_feedback_bounce(
        state, [{"body": "b"}], raiser="qa", origin_sha="s2",
    )
    assert enriched[0]["id"] == "fb-2"


# ---------------------------------------------------------------------------
# L2 — round rolling: current -> previous, older pruned, retries reset
# ---------------------------------------------------------------------------


def test_new_round_rolls_and_prunes_prior_rounds() -> None:
    state = _state()
    _stamp_feedback_bounce(state, [{"body": "r1"}], raiser="reviewing", origin_sha="s1")
    _stamp_feedback_bounce(state, [{"body": "r2"}], raiser="reviewing", origin_sha="s2")
    state["noop_success_retries"] = 1
    _stamp_feedback_bounce(state, [{"body": "r3"}], raiser="reviewing", origin_sha="s3")

    ledger = state["feedback_ledger"]
    # r1's record pruned; r2 rolled to previous (superseded); r3 current.
    digests = {r["body_digest"]: r["round_status"] for r in ledger}
    assert "r1" not in digests
    assert digests["r2"] == "previous"
    assert digests["r3"] == "current"
    assert state["noop_success_retries"] == 0
    assert state["feedback_origin_sha"] == "s3"


# ---------------------------------------------------------------------------
# L3 — unresolvable origin head stamps "" (floor will fail open)
# ---------------------------------------------------------------------------


def test_empty_origin_sha_stamped_as_empty() -> None:
    state = _state()
    _stamp_feedback_bounce(state, [{"body": "x"}], raiser="qa", origin_sha="")
    assert state["feedback_ledger"][0]["origin_sha"] == ""
    assert state["feedback_origin_sha"] is None


# ---------------------------------------------------------------------------
# I4 — disposition application: unknown ids ignored, one disposition per item
# ---------------------------------------------------------------------------


def test_apply_dispositions_updates_open_items() -> None:
    state = _state()
    _stamp_feedback_bounce(
        state, [{"body": "a"}, {"body": "b"}], raiser="reviewing", origin_sha="s1",
    )

    disputed = _apply_feedback_dispositions(
        state,
        [
            {"id": "fb-1", "disposition": "addressed", "reason": "done in c1"},
            {"id": "fb-2", "disposition": "disputed", "reason": "already handled"},
            {"id": "fb-99", "disposition": "addressed"},  # unknown -> ignored
            {"id": "fb-2", "disposition": "addressed"},  # second disposition -> ignored
        ],
    )

    by_id = {r["id"]: r for r in state["feedback_ledger"]}
    assert by_id["fb-1"]["disposition"] == "addressed"
    assert by_id["fb-2"]["disposition"] == "disputed"
    assert by_id["fb-2"]["dispute_reason"] == "already handled"
    assert [d["id"] for d in disputed] == ["fb-2"]


# ---------------------------------------------------------------------------
# D2/D3 — round resolution on the raiser's next verdict
# ---------------------------------------------------------------------------


def test_raiser_pass_accepts_pending_disputes() -> None:
    state = _state()
    _stamp_feedback_bounce(state, [{"body": "a"}], raiser="reviewing", origin_sha="s1")
    _apply_feedback_dispositions(
        state, [{"id": "fb-1", "disposition": "disputed", "reason": "wrong"}],
    )

    _resolve_dispute_round(state, raiser_stage="reviewing", passed=True)

    assert state["feedback_ledger"][0]["disposition"] == "dispute_accepted"


def test_raiser_bounce_rejects_disputes_and_marks_new_round_re_raised() -> None:
    state = _state()
    _stamp_feedback_bounce(state, [{"body": "a"}], raiser="reviewing", origin_sha="s1")
    _apply_feedback_dispositions(
        state, [{"id": "fb-1", "disposition": "disputed", "reason": "wrong"}],
    )

    _resolve_dispute_round(state, raiser_stage="reviewing", passed=False)
    by_id = {r["id"]: r for r in state["feedback_ledger"]}
    assert by_id["fb-1"]["disposition"] == "dispute_rejected"

    enriched = _stamp_feedback_bounce(
        state, [{"body": "a again"}], raiser="reviewing", origin_sha="s1",
    )
    assert enriched[0]["re_raised"] is True


def test_other_raisers_disputes_untouched_by_resolution() -> None:
    state = _state()
    _stamp_feedback_bounce(state, [{"body": "a"}], raiser="qa", origin_sha="s1")
    _apply_feedback_dispositions(
        state, [{"id": "fb-1", "disposition": "disputed", "reason": "flaky"}],
    )

    _resolve_dispute_round(state, raiser_stage="reviewing", passed=True)

    assert state["feedback_ledger"][0]["disposition"] == "disputed"


# ---------------------------------------------------------------------------
# L1 flow — the changes_requested handler stamps before relaying
# ---------------------------------------------------------------------------


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


@pytest.mark.asyncio
async def test_changes_requested_flow_stamps_round_and_enriches_relay() -> None:
    svc = _Performer(response={
        "status": "changes_requested",
        "comments": [{"body": "fix the margin", "author_login": "rev-bot"}],
        "head_after": "feedbackhead1",
    })
    state = initial_state()
    state["performer_services"] = {"reviewing": svc}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_F", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    relay = result["relay_feedback"]
    assert relay[0]["id"] == "fb-1"
    assert relay[0]["raiser"] == "reviewing"
    assert result["feedback_origin_sha"] == "feedbackhead1"
    ledger = result["feedback_ledger"]
    assert ledger[0]["raiser"] == "reviewing"
    assert ledger[0]["origin_sha"] == "feedbackhead1"


# ---------------------------------------------------------------------------
# Copilot r2 regressions
# ---------------------------------------------------------------------------


def test_stamp_prefixes_id_into_description_when_no_body() -> None:
    """Copilot r2 (finding 5): security-finding-shaped items carry
    ``description`` not ``body`` — the fb-id must be prefixed into
    ``description`` so the backend prompt surfaces it."""
    state = _state()
    enriched = _stamp_feedback_bounce(
        state,
        [{"description": "sql injection in login", "routing": "implementer"}],
        raiser="security",
        origin_sha="s1",
    )
    assert enriched[0]["description"].startswith("[fb-1] ")
    assert "sql injection" in enriched[0]["description"]


def test_resolve_dispute_round_ignores_previous_round() -> None:
    """Copilot r2 (finding 2): a stale previous-round disputed entry must not
    be re-adjudicated by a later verdict."""
    state = _state()
    # Round 1 (will roll to previous), disputed.
    _stamp_feedback_bounce(state, [{"body": "a"}], raiser="reviewing", origin_sha="s1")
    _apply_feedback_dispositions(
        state, [{"id": "fb-1", "disposition": "disputed", "reason": "x"}],
    )
    # A NEW round rolls fb-1 to previous (superseded), so it's no longer current.
    _stamp_feedback_bounce(state, [{"body": "b"}], raiser="reviewing", origin_sha="s2")
    prev = next(r for r in state["feedback_ledger"] if r["id"] == "fb-1")
    assert prev["round_status"] == "previous"
    before = prev["disposition"]

    _resolve_dispute_round(state, raiser_stage="reviewing", passed=True)

    after = next(r for r in state["feedback_ledger"] if r["id"] == "fb-1")["disposition"]
    assert after == before  # previous-round entry untouched
