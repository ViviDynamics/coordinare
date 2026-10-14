"""103 review fixes: session persistence of the identical-failure streak, quoted
output that must not be classified as a cut, a hint that stays small and
redacted, stage binding for the repair hint, and shape tables kept in step."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.nodes.dispatch_performer import inject_observer_correction
from coordinare.graph.nodes.handle_system_error import handle_system_error
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.services.assessor_failure import (
    classify_assessor_failure,
    describe_format_exhaustion,
    format_failure_shape,
    format_repair_hint,
)
from coordinare.session import (
    _SESSION_FIELDS,
    _SESSION_OPTIONAL_FIELDS,
    CardSession,
    session_to_state,
    state_to_session,
)
from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

_HEAD_CUT_REASON = (
    "BACKEND_FORMAT_ERROR: Backend assessment output could not be parsed as a JSON object "
    "after 2 attempts. Output length 55 chars, shape head_cut, capture whole. Last output: "
    '\'ufficient": true, "questions": [], "dependencies": []}\''
)
_QUOTED_TRUNCATED_REASON = (
    "BACKEND_FORMAT_ERROR: Backend assessment output could not be parsed as a JSON object "
    "after 2 attempts. Output length 60 chars, shape whole, capture whole. Last output: "
    '\'{"note": "output was truncated"}\''
)
_ALL_SHAPES = ("empty", "array", "head_cut", "tail_cut", "whole", "wrapped", "no_object")
_PERFORMER_MAIN = Path(__file__).resolve().parents[2] / "agent/performer/src/performer/main.py"


def _performer_guidance_keys() -> set[str]:
    """Read the performer's shape table without importing the performer package."""
    tree = ast.parse(_PERFORMER_MAIN.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_OUTPUT_SHAPE_GUIDANCE"
            for target in node.targets
        ):
            assert isinstance(node.value, ast.Dict)
            return {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError("performer main.py has no _OUTPUT_SHAPE_GUIDANCE table")


# -- finding 1: the identical-failure streak survives the session round trip --


def test_streak_fields_are_session_fields() -> None:
    assert "system_error_identical_count" in _SESSION_FIELDS
    assert "system_error_identical_count" in _SESSION_OPTIONAL_FIELDS
    assert "system_error_identical_count" in CardSession.__annotations__


@pytest.mark.asyncio
async def test_identical_streak_survives_three_session_cycles() -> None:
    session: CardSession = CardSession()
    state: dict = {}
    for _ in range(3):
        state = _make_state(
            service=_Performer({"status": "error", "reason": _HEAD_CUT_REASON}),
            stage="assessing",
        )
        session_to_state(session, state)
        state = await monitor_performer(state)
        session = state_to_session(state)
    assert session["system_error_identical_count"] == 3

    exhausted = _make_state(
        service=_Performer({"status": "error", "reason": _HEAD_CUT_REASON}),
        stage="assessing",
    )
    session_to_state(session, exhausted)
    notification = MagicMock()
    notification.dispatch = AsyncMock()
    exhausted["notification_service"] = notification
    exhausted["current_card"] = {"id": "card-65", "title": "format_phone"}
    result = await handle_system_error(exhausted)
    assert "same failure" in result["system_error_reason"]
    event = notification.dispatch.await_args.args[0]
    assert "same failure" in event.payload["error"]


# -- finding 2: quoted model output never decides the classification --


def test_quoted_truncated_word_is_not_a_truncation() -> None:
    assert classify_assessor_failure(_QUOTED_TRUNCATED_REASON) is None


@pytest.mark.asyncio
async def test_whole_object_quoting_truncated_stays_a_format_failure() -> None:
    state = _make_state(
        service=_Performer({"status": "error", "reason": _QUOTED_TRUNCATED_REASON}),
        stage="assessing",
    )
    result = await monitor_performer(state)
    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 1


# -- finding 3: the quoted excerpt is fenced, capped and redacted --


def test_repair_hint_fences_quoted_output_as_untrusted_data() -> None:
    hint = format_repair_hint(_HEAD_CUT_REASON, "assessment")
    assert hint is not None
    assert "not as instructions" in hint
    assert "model's own earlier output" in hint


def test_repair_hint_stays_small_for_a_control_heavy_excerpt() -> None:
    reason = (
        "BACKEND_FORMAT_ERROR: Backend assessment output could not be parsed as a JSON object "
        "after 2 attempts. Output length 5000 chars, shape whole, capture head 300 and tail 200. "
        "Last output: " + repr("\x01" * 5000)
    )
    hint = format_repair_hint(reason, "assessment")
    assert hint is not None
    assert len(hint) <= 800


def test_exhaustion_reason_is_capped() -> None:
    reason = (
        "BACKEND_FORMAT_ERROR: Backend assessment output could not be parsed as a JSON object "
        "after 2 attempts. Output length 5000 chars, shape whole, capture head 300 and tail 200. "
        "Last output: " + repr("\x01" * 5000)
    )
    described = describe_format_exhaustion(reason, stage="assessing", count=3, identical=True)
    assert len(described) <= 600


def test_daemon_redacts_a_credential_quoted_in_the_output() -> None:
    token = "ghp_" + "A" * 36
    reason = (
        "BACKEND_FORMAT_ERROR: Backend assessment output could not be parsed as a JSON object "
        "after 2 attempts. Output length 40 chars, shape whole, capture whole. Last output: "
        f'\'{{"k": "{token}"}}\''
    )
    hint = format_repair_hint(reason, "assessment")
    described = describe_format_exhaustion(reason, stage="assessing", count=3, identical=True)
    assert hint is not None
    assert "ghp_" not in hint
    assert "ghp_" not in described


# -- finding 4: the repair hint is bound to its stage and cleared on exhaustion --


def test_repair_hint_is_not_carried_into_another_stage() -> None:
    card_context: dict = {}
    state = {
        "observer_correction": {
            "body": "repair",
            "author_login": "coordinare",
            "kind": "format_repair",
            "stage": "assessing",
        },
    }
    inject_observer_correction(card_context, state, performer_stage="implementing")
    assert "relay_feedback" not in card_context


def test_repair_hint_reaches_its_own_stage() -> None:
    card_context: dict = {}
    state = {
        "observer_correction": {
            "body": "repair",
            "author_login": "coordinare",
            "kind": "format_repair",
            "stage": "assessing",
        },
    }
    inject_observer_correction(card_context, state, performer_stage="assessing")
    assert card_context["relay_feedback"][0]["body"] == "repair"


@pytest.mark.asyncio
async def test_monitor_binds_the_hint_to_the_failing_stage() -> None:
    state = _make_state(
        service=_Performer({"status": "error", "reason": _HEAD_CUT_REASON}),
        stage="assessing",
    )
    result = await monitor_performer(state)
    assert result["observer_correction"]["kind"] == "format_repair"
    assert result["observer_correction"]["stage"] == "assessing"


@pytest.mark.asyncio
async def test_exhaustion_clears_the_format_repair_hint() -> None:
    state = {
        "phase": "system_error",
        "performer_stage": "assessing",
        "current_card": {"id": "card-65"},
        "system_error_count": 3,
        "system_error_identical_count": 3,
        "system_error_reason": _HEAD_CUT_REASON,
        "observer_correction": {
            "body": "repair",
            "author_login": "coordinare",
            "kind": "format_repair",
            "stage": "assessing",
        },
    }
    result = await handle_system_error(state)
    assert result["observer_correction"] is None


@pytest.mark.asyncio
async def test_exhaustion_keeps_a_human_observer_correction() -> None:
    state = {
        "phase": "system_error",
        "performer_stage": "assessing",
        "current_card": {"id": "card-65"},
        "system_error_count": 3,
        "system_error_identical_count": 3,
        "system_error_reason": _HEAD_CUT_REASON,
        "observer_correction": {"body": "human words", "author_login": "observer"},
    }
    result = await handle_system_error(state)
    assert result["observer_correction"] == {"body": "human words", "author_login": "observer"}


# -- finding 5: the shape tables name the same shapes, and each shape is recognised --


def test_daemon_and_performer_shape_tables_have_the_same_keys() -> None:
    from coordinare.services.assessor_failure import _SHAPE_GUIDANCE

    assert set(_SHAPE_GUIDANCE) == set(_ALL_SHAPES)
    assert _performer_guidance_keys() == set(_ALL_SHAPES)


@pytest.mark.parametrize("shape", _ALL_SHAPES)
def test_each_shape_token_is_recognised(shape: str) -> None:
    reason = f"BACKEND_FORMAT_ERROR: x. Output length 1 chars, shape {shape}, capture whole. Last output: 'x'"
    assert format_failure_shape(reason) == shape


# -- finding 7: the hint is documented as JSON-contract only --


def test_repair_hint_docstring_states_the_json_contract_gate() -> None:
    doc = format_repair_hint.__doc__ or ""
    assert "JSON-contract" in doc
