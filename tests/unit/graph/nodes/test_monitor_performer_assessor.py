"""098: assessor (junie) resilience — monitor_performer routing tests.

US1: an assessor parse/empty failure routes into the existing bounded
system-error retry instead of the default terminal block. A genuine
model-capability (prose) failure is NOT reclassified (it still blocks). The
assessor-shape routing is scoped to the ``assessing`` stage (FR-007).

US3: at retry-budget exhaustion, an ``empty_body`` (overloaded/down upstream)
surfaces as ENV_BLOCKED (infra), while a ``malformed_body`` blocks normally.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import structlog

from coordinare.graph.nodes.handle_system_error import _MAX_RETRIES, handle_system_error
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


class _GitHub:
    def __init__(self) -> None:
        self.move_calls: list[tuple[str, str]] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))


def _assessing_state(*, service: object, card: dict | None = None) -> dict:
    state = initial_state()
    state["performer_services"] = {"assessing": service}
    state["performer_stage"] = "assessing"
    state["lifecycle_sequence"] = ["assessing", "implementing"]
    state["current_card"] = card or {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    return state


# --- T004 US1 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessor_malformed_response_retries_not_blocks() -> None:
    """(a) A junie 'Failed to build issue.md' failure routes to the system-error
    retry branch — NOT the default terminal block."""
    service = _Performer({
        "status": "error",
        "reason": "Junie failed with the message: Failed to build 'issue.md.junie_standalone'",
    })
    state = _assessing_state(service=service)
    result = await monitor_performer(state)
    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 1
    assert result["performer_stage"] == "assessing"  # not advanced, not blocked


@pytest.mark.asyncio
async def test_assessor_prose_failure_still_blocks() -> None:
    """(b) A genuine model-capability (prose) failure is NOT reclassified as
    retryable — it must still block (no infinite retry of a real failure)."""
    service = _Performer({
        "status": "error",
        "reason": "Assessment failed: the plan omits the required database migration.",
    })
    state = _assessing_state(service=service)
    result = await monitor_performer(state)
    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_assessor_shape_routing_scoped_to_assessing_stage() -> None:
    """(c) FR-007: the assessor-shape reclassification must NOT leak to other
    stages. The same reason at a non-assessing stage follows the existing path
    (default block — it is not a recognised transient backend error)."""
    service = _Performer({
        "status": "error",
        "reason": "Failed to build 'issue.md.junie_standalone'",
    })
    state = initial_state()
    state["performer_services"] = {"reviewing": service}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    result = await monitor_performer(state)
    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_clean_assessor_response_resets_error_counter() -> None:
    """Contract invariant 2 (FR-005): the retry budget is CONSECUTIVE failures —
    a clean assessor response (assessment_complete) after prior flaky errors must
    reset system_error_count to 0, so a stale count is not carried forward."""
    service = _Performer({"status": "assessment_complete"})
    state = _assessing_state(service=service)
    state["system_error_count"] = 2  # two prior flaky attempts
    state["system_error_reason"] = "BACKEND_FORMAT_ERROR: Failed to build 'issue.md'"
    result = await monitor_performer(state)
    assert result.get("system_error_count") == 0
    assert result.get("performer_stage") != "assessing"  # advanced past assessing


@pytest.mark.asyncio
async def test_clean_non_assessing_success_does_not_reset_counter() -> None:
    """FR-007: the reset is scoped to the assessing stage — a clean success at a
    non-assessing stage leaves the counter untouched (other stages unchanged)."""
    service = _Performer({"status": "approved"})
    state = initial_state()
    state["performer_services"] = {"reviewing": service}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["reviewing", "implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["system_error_count"] = 2
    result = await monitor_performer(state)
    assert result.get("system_error_count") == 2  # unchanged


@pytest.mark.asyncio
async def test_assessor_empty_body_routes_to_retry() -> None:
    """An empty-body (overloaded upstream) assessor failure is retryable too."""
    service = _Performer({"status": "error", "reason": "upstream returned an empty response body"})
    state = _assessing_state(service=service)
    result = await monitor_performer(state)
    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 1


# --- T012 observability -----------------------------------------------------


@pytest.mark.asyncio
async def test_assessor_parse_failure_emits_secret_free_record() -> None:
    """FR-004/FR-008: a parse failure emits a shape-tagged record carrying only
    shape + attempt — never raw model output or tokens."""
    raw_secret = "SECRET-MODEL-OUTPUT-tok-abc123 the answer is 42"
    service = _Performer({
        "status": "error",
        "reason": f"Failed to build 'issue.md.junie_standalone' :: {raw_secret}",
    })
    state = _assessing_state(service=service)
    with structlog.testing.capture_logs() as cap:
        await monitor_performer(state)
    records = [e for e in cap if e.get("event") == "assessor.parse_failure"]
    assert len(records) == 1
    rec = records[0]
    assert rec["shape"] == "malformed_body"
    assert rec["stage"] == "assessing"
    assert rec["attempt"] == 1
    # The record carries ONLY shape/stage/attempt/card_id — no reason text/tokens.
    assert "reason" not in rec
    assert raw_secret not in repr(rec)


# --- T010 US3 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_empty_body_surfaces_env_blocked() -> None:
    """(a) When the system-error budget exhausts and the assessor failures were
    empty-body, the card is surfaced as ENV_BLOCKED (infra), not a generic
    card-fault block."""
    github = _GitHub()
    state = initial_state()
    state["performer_stage"] = "assessing"
    state["current_card"] = {"id": "ITEM_1", "title": "T", "status": "IN_PROGRESS"}
    state["github_service"] = github
    state["system_error_count"] = _MAX_RETRIES
    state["system_error_last_at"] = datetime.now(UTC)
    state["system_error_reason"] = "BACKEND_FORMAT_ERROR: upstream returned an empty response body"
    result = await handle_system_error(state)
    env = result.get("env_blocked")
    assert env is not None
    assert env.get("pattern_id") == "assessor_model_unavailable"
    assert github.move_calls == [("ITEM_1", "BLOCKED")]


@pytest.mark.asyncio
async def test_exhausted_malformed_blocks_normally() -> None:
    """(b) A malformed-body exhaustion blocks normally — NOT ENV_BLOCKED."""
    github = _GitHub()
    state = initial_state()
    state["performer_stage"] = "assessing"
    state["current_card"] = {"id": "ITEM_1", "title": "T", "status": "IN_PROGRESS"}
    state["github_service"] = github
    state["system_error_count"] = _MAX_RETRIES
    state["system_error_last_at"] = datetime.now(UTC)
    state["system_error_reason"] = "BACKEND_FORMAT_ERROR: Failed to build 'issue.md.junie_standalone'"
    result = await handle_system_error(state)
    assert result.get("env_blocked") is None
    assert github.move_calls == [("ITEM_1", "BLOCKED")]


# --- 123 US3 (T011): split bounce budget — content vs transient ------------


@pytest.mark.asyncio
async def test_transient_budget_exhaustion_surfaces_env_blocked() -> None:
    """123 FR-008: after transient_error_cycles reaches its limit (3), a
    definitive infra failure surfaces the card as ENV_BLOCKED even when the
    reason is not the assessor empty-body case."""
    from coordinare.graph.nodes.handle_system_error import _TRANSIENT_ERROR_CYCLE_LIMIT

    github = _GitHub()
    env_result = None
    for cycle in range(_TRANSIENT_ERROR_CYCLE_LIMIT):
        state = initial_state()
        state["performer_stage"] = "implementing"
        state["current_card"] = {"id": "ITEM_1", "title": "T", "status": "IN_PROGRESS"}
        state["github_service"] = github
        state["system_error_count"] = _MAX_RETRIES
        state["system_error_last_at"] = datetime.now(UTC)
        state["system_error_reason"] = "some generic backend failure"
        # carry the accumulating per-card counter forward across episodes
        state["transient_error_cycles"] = cycle
        env_result = await handle_system_error(state)

    assert env_result is not None
    assert env_result["transient_error_cycles"] == _TRANSIENT_ERROR_CYCLE_LIMIT
    env = env_result.get("env_blocked")
    assert env is not None
    assert env.get("pattern_id") == "transient_error_budget_exhausted"


@pytest.mark.asyncio
async def test_transient_budget_first_episode_blocks_normally() -> None:
    """123 FR-008: the first infra-failure episode (transient_error_cycles=1)
    is below the limit — it blocks normally, not ENV_BLOCKED."""
    github = _GitHub()
    state = initial_state()
    state["performer_stage"] = "implementing"
    state["current_card"] = {"id": "ITEM_1", "title": "T", "status": "IN_PROGRESS"}
    state["github_service"] = github
    state["system_error_count"] = _MAX_RETRIES
    state["system_error_last_at"] = datetime.now(UTC)
    state["system_error_reason"] = "some generic backend failure"
    result = await handle_system_error(state)
    assert result["transient_error_cycles"] == 1
    assert result.get("env_blocked") is None


@pytest.mark.asyncio
async def test_transient_failures_do_not_touch_content_budget() -> None:
    """123 US3 independent test: infra failures increment transient_error_cycles
    and never content_feedback_cycles (the counters don't cross-trigger)."""
    github = _GitHub()
    state = initial_state()
    state["performer_stage"] = "implementing"
    state["current_card"] = {"id": "ITEM_1", "title": "T", "status": "IN_PROGRESS"}
    state["github_service"] = github
    state["system_error_count"] = _MAX_RETRIES
    state["system_error_last_at"] = datetime.now(UTC)
    state["system_error_reason"] = "some generic backend failure"
    state["content_feedback_cycles"] = 2
    result = await handle_system_error(state)
    assert result["transient_error_cycles"] == 1
    assert result["content_feedback_cycles"] == 2  # untouched


# --- 123 US4 (T014): assessor open_questions persisted from result ----------


@pytest.mark.asyncio
async def test_assessor_questions_persisted_as_assessor_open_questions() -> None:
    """123 FR-010: when the assessor blocks with open questions, they are
    persisted as assessor_open_questions ({"question","answer"} carry-forward)
    for injection on the next dispatch."""
    service = _Performer({"status": "blocked", "questions": ["Use OAuth?", "Which DB?"]})
    state = _assessing_state(service=service)
    result = await monitor_performer(state)
    assert result["phase"] == "blocked"
    qa = result.get("assessor_open_questions")
    assert qa == [
        {"question": "Use OAuth?", "answer": ""},
        {"question": "Which DB?", "answer": ""},
    ]
    # The blocked-diagnostic surface (open_questions: list[str]) is unaffected.
    assert result.get("open_questions") == ["Use OAuth?", "Which DB?"]


@pytest.mark.asyncio
async def test_non_assessing_blocked_questions_not_persisted_as_assessor_qa() -> None:
    """123: the assessor Q&A carry-forward is scoped to the assessing stage —
    a non-assessing blocked-with-questions does not populate it."""
    service = _Performer({"status": "blocked", "questions": ["Q?"]})
    state = initial_state()
    state["performer_services"] = {"reviewing": service}
    state["performer_stage"] = "reviewing"
    state["lifecycle_sequence"] = ["reviewing", "implementing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    result = await monitor_performer(state)
    assert result.get("assessor_open_questions") in (None, [])
