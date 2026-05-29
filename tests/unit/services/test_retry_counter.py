"""Spec 076 T086 — retry_counter unit tests.

Covers FR-019 (idle-timeout retry budget, default 2, rolling 24h window).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coordinare.services.retry_counter import (
    attempts_in_window,
    record_idle_timeout,
    reset_if_window_expired,
    should_block,
)


def _state_with_session(card_id: str = "PVTI_X") -> dict:
    return {
        "active_sessions": {
            card_id: {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "idle_timeout_retries": {},
            },
        },
    }


def test_first_idle_timeout_returns_retry() -> None:
    state = _state_with_session()
    decision = record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    assert decision == "retry"
    assert attempts_in_window(state, "PVTI_X", "implementing") == 1


def test_second_idle_timeout_returns_retry() -> None:
    state = _state_with_session()
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    decision = record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    assert decision == "retry"
    assert attempts_in_window(state, "PVTI_X", "implementing") == 2


def test_third_idle_timeout_returns_block() -> None:
    """Clarification Q3: 2 retries per (card, stage) → BLOCKED on the 3rd."""
    state = _state_with_session()
    for _ in range(2):
        record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    decision = record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    assert decision == "block"
    assert attempts_in_window(state, "PVTI_X", "implementing") == 3


def test_separate_cards_have_separate_counters() -> None:
    state = {
        "active_sessions": {
            "PVTI_A": {"idle_timeout_retries": {}},
            "PVTI_B": {"idle_timeout_retries": {}},
        },
    }
    for _ in range(3):
        record_idle_timeout(state, "PVTI_A", "implementing", budget=2)
    decision_b = record_idle_timeout(state, "PVTI_B", "implementing", budget=2)
    # B is still on its first attempt despite A being blocked
    assert decision_b == "retry"


def test_separate_stages_for_same_card_have_separate_counters() -> None:
    """FR-019 + data-model §4: counter keyed on (card_id, stage)."""
    state = _state_with_session()
    for _ in range(3):
        record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    decision_review = record_idle_timeout(state, "PVTI_X", "reviewing", budget=2)
    assert decision_review == "retry"


def test_window_expiration_resets_counter() -> None:
    """If the rolling window has elapsed, the counter MUST reset to 1
    so an idle-timeout 25h after the last one doesn't get blocked."""
    state = _state_with_session()
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    # Backdate the window_start_at to 25h ago
    retries = state["active_sessions"]["PVTI_X"]["idle_timeout_retries"]
    key = "PVTI_X:implementing"
    retries[key]["window_start_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()

    decision = record_idle_timeout(state, "PVTI_X", "implementing", budget=2, window_hours=24)
    assert decision == "retry"  # new window
    assert retries[key]["attempt_count"] == 1


def test_should_block_returns_true_when_at_budget() -> None:
    state = _state_with_session()
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    # At budget — next call would trigger block
    assert should_block(state, "PVTI_X", "implementing", budget=2)


def test_should_block_returns_false_before_budget() -> None:
    state = _state_with_session()
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    assert not should_block(state, "PVTI_X", "implementing", budget=2)


def test_reset_if_window_expired_clears_old_record() -> None:
    state = _state_with_session()
    record_idle_timeout(state, "PVTI_X", "implementing")
    retries = state["active_sessions"]["PVTI_X"]["idle_timeout_retries"]
    key = "PVTI_X:implementing"
    retries[key]["window_start_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    reset_if_window_expired(state, "PVTI_X", "implementing", window_hours=24)
    assert key not in retries


def test_attempts_in_window_empty_returns_zero() -> None:
    """No record → 0 attempts."""
    state = _state_with_session()
    from coordinare.services.retry_counter import attempts_in_window
    assert attempts_in_window(state, "PVTI_X", "implementing") == 0


def test_attempts_in_window_after_expiry_returns_zero() -> None:
    """Expired window → 0 attempts (effectively reset)."""
    state = _state_with_session()
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    retries = state["active_sessions"]["PVTI_X"]["idle_timeout_retries"]
    retries["PVTI_X:implementing"]["window_start_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    from coordinare.services.retry_counter import attempts_in_window
    assert attempts_in_window(state, "PVTI_X", "implementing", window_hours=24) == 0


def test_attempts_in_window_malformed_window_start_returns_zero() -> None:
    """A record with no parseable window_start_at → 0 attempts."""
    state = _state_with_session()
    state["active_sessions"]["PVTI_X"]["idle_timeout_retries"]["PVTI_X:implementing"] = {
        "card_id": "PVTI_X",
        "performer_stage": "implementing",
        "window_start_at": "not a timestamp",
        "attempt_count": 1,
    }
    from coordinare.services.retry_counter import attempts_in_window
    assert attempts_in_window(state, "PVTI_X", "implementing") == 0


def test_reset_if_window_expired_no_record() -> None:
    """Calling reset on a non-existent record is a no-op."""
    state = _state_with_session()
    from coordinare.services.retry_counter import reset_if_window_expired
    reset_if_window_expired(state, "PVTI_X", "implementing")
    assert state["active_sessions"]["PVTI_X"]["idle_timeout_retries"] == {}


def test_reset_if_window_expired_malformed_window_pops() -> None:
    """A record with unparseable window_start_at is popped."""
    state = _state_with_session()
    state["active_sessions"]["PVTI_X"]["idle_timeout_retries"]["PVTI_X:implementing"] = {
        "window_start_at": "garbage",
        "attempt_count": 1,
    }
    from coordinare.services.retry_counter import reset_if_window_expired
    reset_if_window_expired(state, "PVTI_X", "implementing")
    assert state["active_sessions"]["PVTI_X"]["idle_timeout_retries"] == {}


def test_get_retries_dict_falls_back_to_state_root_for_legacy_shape() -> None:
    """A state with no active_sessions for the card falls back to the
    state-root idle_timeout_retries dict (legacy single-card shape)."""
    state: dict = {"active_sessions": {}}
    record_idle_timeout(state, "PVTI_LEGACY", "implementing")
    # Wrote to state root, not active_sessions[card]
    assert "idle_timeout_retries" in state
    assert "PVTI_LEGACY:implementing" in state["idle_timeout_retries"]


def test_parse_dt_handles_datetime_input() -> None:
    """_parse_dt accepts datetime objects directly (not just strings)."""
    from coordinare.services.retry_counter import _parse_dt
    naive = datetime(2026, 5, 28, 22, 0, 0)
    aware = _parse_dt(naive)
    assert aware is not None
    assert aware.tzinfo is not None  # naive → UTC
    aware_in = datetime(2026, 5, 28, 22, 0, 0, tzinfo=UTC)
    assert _parse_dt(aware_in) == aware_in


def test_parse_dt_rejects_garbage() -> None:
    """Non-parseable input → None (not exception)."""
    from coordinare.services.retry_counter import _parse_dt
    assert _parse_dt(None) is None
    assert _parse_dt("not a timestamp") is None
    assert _parse_dt(12345) is None


def test_empty_output_first_call_retries_then_blocks() -> None:
    """T171: empty-output default budget is 1 — first call retries,
    second call (>budget) blocks."""
    from coordinare.services.retry_counter import record_empty_output

    state = _state_with_session()
    assert record_empty_output(state, "PVTI_X", "architecting", budget=1) == "retry"
    assert record_empty_output(state, "PVTI_X", "architecting", budget=1) == "block"


def test_empty_output_and_idle_timeout_counters_are_independent() -> None:
    """T171: empty-output uses a namespaced key, so it must NOT share a
    counter with idle-timeout for the same (card, stage)."""
    from coordinare.services.retry_counter import record_empty_output

    state = _state_with_session()
    # Exhaust idle-timeout (budget 2 → 3rd blocks)
    record_idle_timeout(state, "PVTI_X", "architecting", budget=2)
    record_idle_timeout(state, "PVTI_X", "architecting", budget=2)
    record_idle_timeout(state, "PVTI_X", "architecting", budget=2)
    # Empty-output for the SAME (card, stage) starts fresh
    assert record_empty_output(state, "PVTI_X", "architecting", budget=1) == "retry"
    # Both records coexist under distinct keys
    retries = state["active_sessions"]["PVTI_X"]["idle_timeout_retries"]
    assert "PVTI_X:architecting" in retries          # idle-timeout (bare key)
    assert "PVTI_X:architecting:empty_output" in retries  # empty-output (namespaced)


def test_empty_output_budget_zero_blocks_immediately() -> None:
    """With budget=0, the very first empty-output blocks (no retry) —
    the most aggressive fail-fast setting for a known-deterministic card."""
    from coordinare.services.retry_counter import record_empty_output

    state = _state_with_session()
    assert record_empty_output(state, "PVTI_X", "architecting", budget=0) == "block"


def test_counter_survives_session_serialization() -> None:
    """FR-019 final sentence: the retry counter MUST persist across daemon
    restarts.  Round-trip through the session ↔ state transition.

    The session-to-state path is exercised in test_session_076_round_trips;
    here we just confirm the in-memory dict is a plain JSON-serialisable
    shape (no datetime objects that the snapshot writer would choke on)."""
    state = _state_with_session()
    record_idle_timeout(state, "PVTI_X", "implementing")
    retries = state["active_sessions"]["PVTI_X"]["idle_timeout_retries"]
    record = retries["PVTI_X:implementing"]
    # All timestamps stored as ISO strings, not datetime objects
    assert isinstance(record["window_start_at"], str)
    assert isinstance(record["last_at"], str)
    # JSON-roundtrip-safe: try the obvious
    import json
    json_blob = json.dumps(record)
    re_loaded = json.loads(json_blob)
    assert re_loaded["attempt_count"] == 1
