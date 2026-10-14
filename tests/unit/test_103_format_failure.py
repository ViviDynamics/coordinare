"""103: a BACKEND_FORMAT_ERROR retry carries the failure forward, and an
exhausted identical failure names its class on the card."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.nodes.dispatch_performer import inject_observer_correction
from coordinare.graph.nodes.handle_system_error import handle_system_error
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.services.assessor_failure import (
    describe_format_exhaustion,
    format_failure_excerpt,
    format_failure_shape,
    format_repair_hint,
)
from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

_HEAD_CUT_REASON = (
    "BACKEND_FORMAT_ERROR: Backend assessment output could not be parsed as a JSON object "
    "after 2 attempts. Output length 55 chars, shape head_cut, capture whole. Last output: "
    '\'ufficient": true, "questions": [], "dependencies": []}\''
)
_PLAIN_REASON = "BACKEND_FORMAT_ERROR: Failed to build 'issue.md'"


def test_shape_token_is_read_from_a_performer_stamped_reason() -> None:
    assert format_failure_shape(_HEAD_CUT_REASON) == "head_cut"


def test_shape_is_none_when_the_reason_carries_no_shape() -> None:
    assert format_failure_shape(_PLAIN_REASON) is None
    assert format_failure_shape(None) is None


def test_excerpt_is_the_captured_output_after_the_marker() -> None:
    excerpt = format_failure_excerpt(_HEAD_CUT_REASON)
    assert excerpt is not None
    assert excerpt.startswith("'ufficient")
    assert format_failure_excerpt(_PLAIN_REASON) is None


def test_repair_hint_names_the_lost_head_and_quotes_the_capture() -> None:
    hint = format_repair_hint(_HEAD_CUT_REASON, "assessment")
    assert hint is not None
    assert "opening brace" in hint
    assert "ufficient" in hint
    assert "assessment" in hint


def test_no_repair_hint_without_a_shape_token() -> None:
    assert format_repair_hint(_PLAIN_REASON, "assessment") is None


def test_exhaustion_names_the_class_when_every_attempt_was_identical() -> None:
    described = describe_format_exhaustion(
        _HEAD_CUT_REASON,
        stage="assessing",
        count=3,
        identical=True,
    )
    assert described.startswith("BACKEND_FORMAT_ERROR: ")
    assert "assessment output unparseable 3 times, same failure" in described
    assert "head_cut" in described
    assert "Last output:" in described


def test_exhaustion_wording_drops_same_failure_when_attempts_differed() -> None:
    described = describe_format_exhaustion(
        _HEAD_CUT_REASON,
        stage="assessing",
        count=3,
        identical=False,
    )
    assert "unparseable 3 times" in described
    assert "same failure" not in described


@pytest.mark.asyncio
async def test_handle_system_error_names_the_class_in_the_blocked_record() -> None:
    notification = MagicMock()
    notification.dispatch = AsyncMock()
    state = {
        "phase": "system_error",
        "performer_stage": "assessing",
        "current_card": {"id": "card-65", "title": "format_phone"},
        "system_error_count": 3,
        "system_error_identical_count": 3,
        "system_error_reason": _HEAD_CUT_REASON,
        "notification_service": notification,
    }
    result = await handle_system_error(state)
    assert result["phase"] == "idle"
    assert result["system_error_notified"] is True
    assert "same failure" in result["system_error_reason"]
    assert result["system_error_reason"].startswith("BACKEND_FORMAT_ERROR:")
    event = notification.dispatch.await_args.args[0]
    assert "same failure" in event.payload["error"]
    assert "head_cut" in event.payload["error"]


@pytest.mark.asyncio
async def test_handle_system_error_keeps_generic_wording_for_mixed_failures() -> None:
    notification = MagicMock()
    notification.dispatch = AsyncMock()
    state = {
        "phase": "system_error",
        "performer_stage": "assessing",
        "current_card": {"id": "card-65", "title": "format_phone"},
        "system_error_count": 3,
        "system_error_identical_count": 1,
        "system_error_reason": _HEAD_CUT_REASON,
        "notification_service": notification,
    }
    await handle_system_error(state)
    event = notification.dispatch.await_args.args[0]
    assert "same failure" not in event.payload["error"]
    assert "unparseable 3 times" in event.payload["error"]


@pytest.mark.asyncio
async def test_format_failure_retry_sets_a_repair_hint_for_the_next_dispatch() -> None:
    state = _make_state(
        service=_Performer({"status": "error", "reason": _HEAD_CUT_REASON}), stage="assessing"
    )
    result = await monitor_performer(state)
    assert result["phase"] == "system_error"
    correction = result["observer_correction"]
    assert correction["author_login"] == "coordinare"
    assert "opening brace" in correction["body"]
    assert result["system_error_identical_count"] == 1


@pytest.mark.asyncio
async def test_identical_consecutive_format_failures_increment_the_streak() -> None:
    state = _make_state(
        service=_Performer({"status": "error", "reason": _HEAD_CUT_REASON}), stage="assessing"
    )
    state["system_error_reason"] = _HEAD_CUT_REASON
    state["system_error_identical_count"] = 2
    result = await monitor_performer(state)
    assert result["system_error_identical_count"] == 3


@pytest.mark.asyncio
async def test_a_different_failure_resets_the_streak() -> None:
    state = _make_state(
        service=_Performer({"status": "error", "reason": _HEAD_CUT_REASON}), stage="assessing"
    )
    state["system_error_reason"] = _PLAIN_REASON
    state["system_error_identical_count"] = 2
    result = await monitor_performer(state)
    assert result["system_error_identical_count"] == 1


def test_repair_hint_rides_the_existing_observer_correction_channel() -> None:
    card_context: dict = {}
    state = {"observer_correction": {"body": "repair me", "author_login": "coordinare"}}
    inject_observer_correction(card_context, state)
    assert card_context["relay_feedback"] == [{"body": "repair me", "author_login": "coordinare"}]
