"""Spec 126 — implementer terminal-success floor (contract F1-F7) and dispute
routing (D1/D4/D5), through the full monitor_performer flow.

Contract: specs/126-terminal-success-floors/contracts/success-floors.md
"""
from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from coordinare.graph.nodes.monitor_performer import (
    _stamp_feedback_bounce,
    monitor_performer,
)
from coordinare.graph.state import initial_state


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


def _impl_state(
    response: dict,
    *,
    stamp: bool = True,
    origin: str = "origin000",
    raiser: str = "reviewing",
    retries: int = 0,
) -> dict:
    state = initial_state()
    svc = _Performer(response=response)
    state["performer_services"] = {"implementing": svc, "reviewing": _Performer({})}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_FLOOR", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    if stamp:
        _stamp_feedback_bounce(
            state,
            [{"body": "fix the margin"}, {"body": "rename helper"}],
            raiser=raiser,
            origin_sha=origin,
        )
        state["noop_success_retries"] = retries
    return state


def _success(head_after: str | None, dispositions: list[dict] | None = None) -> dict:
    resp: dict[str, Any] = {"status": "pr_opened", "pr_url": "https://x/pull/1"}
    if head_after is not None:
        resp["head_after"] = head_after
    if dispositions is not None:
        resp["feedback_dispositions"] = dispositions
    return resp


# ---------------------------------------------------------------------------
# F1 — no stamped round: today's behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_pass_success_accepts_without_floor() -> None:
    state = _impl_state(_success("newhead1"), stamp=False)

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# F2 — head moved: accept, dispositions applied, origin cleared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moved_head_accepts_and_applies_dispositions() -> None:
    state = _impl_state(
        _success(
            "newhead1",
            [{"id": "fb-1", "disposition": "addressed", "reason": "done"}],
        ),
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    by_id = {r["id"]: r for r in result["feedback_ledger"]}
    assert by_id["fb-1"]["disposition"] == "addressed"
    assert by_id["fb-2"]["disposition"] == "open"  # missing disposition rides forward
    assert result["feedback_origin_sha"] is None
    assert result["noop_success_retries"] == 0


# ---------------------------------------------------------------------------
# F4/F5 — unmoved head, nothing disputed: one strengthened retry, then hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmoved_head_first_trip_strengthened_redispatch() -> None:
    state = _impl_state(_success("origin000"))
    assert state["content_feedback_cycles"] == 0

    with capture_logs() as logs:
        result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result["noop_success_retries"] == 1
    # Strengthened directive names the outstanding item ids.
    relay_blob = " ".join(str(r.get("body", "")) for r in result["relay_feedback"])
    assert "fb-1" in relay_blob and "fb-2" in relay_blob
    # Budgets untouched (I2).
    assert result["content_feedback_cycles"] == 0
    assert result["transient_error_cycles"] == 0
    assert [e for e in logs if e.get("event") == "monitor_performer.success_floor_retry"]


@pytest.mark.asyncio
async def test_unmoved_head_second_trip_holds_for_operator() -> None:
    state = _impl_state(_success("origin000"), retries=1)

    with capture_logs() as logs:
        result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    blob = " ".join(result["open_questions"])
    assert "origin000" in blob and "fb-1" in blob
    assert result["content_feedback_cycles"] == 0
    assert [e for e in logs if e.get("event") == "monitor_performer.success_floor_hold"]


@pytest.mark.asyncio
async def test_cross_raiser_bounce_at_same_head_does_not_reset_retry_budget() -> None:
    """Adversarial-review fix: a different raiser bouncing at the SAME origin
    head must not re-arm the F4 retry — the second no-op completion still
    holds (F5), it cannot escape via the cross-raiser reset."""
    state = _impl_state(_success("origin000"), retries=1)
    # A second raiser (qa) bounces at the same head between completions.
    _stamp_feedback_bounce(
        state, [{"body": "qa also unhappy"}], raiser="qa", origin_sha="origin000",
    )
    assert state["noop_success_retries"] == 1  # budget preserved

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_new_origin_head_resets_retry_budget() -> None:
    """Control: a bounce at a genuinely NEW head re-arms the retry budget."""
    state = _impl_state(_success("newhead1"), retries=1)
    _stamp_feedback_bounce(
        state, [{"body": "new round"}], raiser="qa", origin_sha="newhead1",
    )
    assert state["noop_success_retries"] == 0


# ---------------------------------------------------------------------------
# F3 — unmoved head with a dispute: accept; addressed claims stay open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmoved_head_with_dispute_accepts_and_queues() -> None:
    state = _impl_state(
        _success(
            "origin000",
            [
                {"id": "fb-1", "disposition": "disputed", "reason": "already correct"},
                {"id": "fb-2", "disposition": "addressed", "reason": "claim"},
            ],
        ),
    )

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    by_id = {r["id"]: r for r in result["feedback_ledger"]}
    assert by_id["fb-1"]["disposition"] == "disputed"
    # F3: an addressed claim on an unmoved head does NOT close the item.
    assert by_id["fb-2"]["disposition"] == "open"


# ---------------------------------------------------------------------------
# F6 — unresolvable completion head: accept (fail-open)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_completion_head_accepts() -> None:
    state = _impl_state(_success(None))

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"


# ---------------------------------------------------------------------------
# D4 — second dispute of a re-raised round holds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_of_re_raised_round_holds() -> None:
    state = _impl_state(_success("origin000"), stamp=False)
    # Round 1 disputed and rejected; round 2 stamped re_raised.
    _stamp_feedback_bounce(state, [{"body": "a"}], raiser="reviewing", origin_sha="origin000")
    from coordinare.graph.nodes.monitor_performer import (
        _apply_feedback_dispositions,
        _resolve_dispute_round,
    )
    _apply_feedback_dispositions(
        state, [{"id": "fb-1", "disposition": "disputed", "reason": "no"}],
    )
    _resolve_dispute_round(state, raiser_stage="reviewing", passed=False)
    _stamp_feedback_bounce(state, [{"body": "a again"}], raiser="reviewing", origin_sha="origin000")

    state["performer_services"]["implementing"] = _Performer(
        _success(
            "origin000",
            [{"id": "fb-2", "disposition": "disputed", "reason": "still no"}],
        ),
    )

    with capture_logs() as logs:
        result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert [e for e in logs if e.get("event") == "monitor_performer.success_floor_hold"]


# ---------------------------------------------------------------------------
# D5 — disputing a CI-raised item holds directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_of_ci_item_holds() -> None:
    state = _impl_state(
        _success(
            "origin000",
            [{"id": "fb-1", "disposition": "disputed", "reason": "check is flaky"}],
        ),
        raiser="ci",
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"


# ---------------------------------------------------------------------------
# F7 — verdict roles exempt from the floor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_success_never_floor_evaluated() -> None:
    state = initial_state()
    svc = _Performer({"status": "approved", "head_after": "origin000"})
    state["performer_services"] = {"reviewing": svc, "security": _Performer({})}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["reviewing", "security"]
    state["current_card"] = {"id": "ITEM_R", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    # A stamped round exists (e.g. QA bounced earlier) but the reviewer's own
    # zero-head-delta success must advance untouched.
    _stamp_feedback_bounce(state, [{"body": "x"}], raiser="qa", origin_sha="origin000")

    result = await monitor_performer(state)

    assert result["performer_stage"] == "security"
    assert result["phase"] == "dispatching"
