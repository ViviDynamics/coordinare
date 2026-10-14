"""148: tool rows say what ran, narration and lifecycle rows stand apart.

The daemon turns each performer event into one activity entry. These tests pin
that mapping, that secrets in a command are redacted before the entry (and so
the snapshot and the browser) can hold them, the summary cap, and the shape.
"""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.graph.nodes.monitor.activity import _feed_fields, _record_activity_batch
from coordinare.services.activity_log import MAX_SUMMARY, MAX_TEXT, ActivityLog

_GH_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_CARD = {"id": "PVTI_148", "issue_number": 148, "title": "Activity detail"}


def _tool_event(**fields: Any) -> dict[str, Any]:
    return {"type": "tool_use", "text": "Bash", "tool": "Bash", "arg": "", "status": "", **fields}


def _state(log: ActivityLog) -> dict[str, Any]:
    return {
        "activity_log": log,
        "current_card": dict(_CARD),
        "agent_dispatch": {"session_id": "sess-1", "performer_id": "implementer"},
    }


def _snapshot_text(log: ActivityLog) -> str:
    return repr(log.snapshot())


# --- the mapping from backend event to feed entry ---------------------------------


def test_a_tool_call_becomes_a_tool_use_entry_with_its_summary() -> None:
    fields = _feed_fields(_tool_event(arg="uv run pytest -q"))
    assert fields == {"activity_type": "tool_use", "summary": "Bash · uv run pytest -q"}


def test_a_tool_call_without_an_argument_keeps_just_its_name() -> None:
    assert _feed_fields(_tool_event())["summary"] == "Bash"


def test_a_legacy_backend_event_uses_its_text_as_the_summary() -> None:
    legacy = {"type": "tool_use", "text": "bash: ls -la", "detail": "bash"}
    assert _feed_fields(legacy) == {"activity_type": "tool_use", "summary": "bash: ls -la"}


def test_a_tool_result_becomes_a_tool_result_entry_with_its_status() -> None:
    ok = _feed_fields({"type": "tool_use", "text": "result: 3 passed", "status": "ok"})
    failed = _feed_fields({"type": "tool_use", "text": "result: boom", "status": "error"})
    assert ok == {"activity_type": "tool_result", "summary": "result ok", "status": "ok"}
    assert failed == {"activity_type": "tool_result", "summary": "result error", "status": "error"}


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [("progress", "progress"), ("thinking", "thinking"), ("error", "error"), ("cost", "cost")],
)
def test_narration_and_other_kinds_keep_their_138_type(event_type: str, expected: str) -> None:
    assert _feed_fields({"type": event_type, "text": "words"}) == {"activity_type": expected}


# --- recorded through the monitor, end to end --------------------------------------


def test_a_batch_records_tool_rows_with_summaries_and_results_with_status() -> None:
    log = ActivityLog()
    _record_activity_batch(
        _state(log),
        [
            _tool_event(arg="git status", timestamp="2026-10-13T09:00:00+00:00"),
            {"type": "tool_use", "text": "result: clean", "status": "ok",
             "timestamp": "2026-10-13T09:00:01+00:00"},
            {"type": "progress", "text": "Tests are green, opening the PR.",
             "timestamp": "2026-10-13T09:00:02+00:00"},
        ],
        card_id="PVTI_148",
        stage="implementing",
    )
    tool, result, narration = log.snapshot()
    assert (tool["activity_type"], tool["summary"], tool["status"]) == ("tool_use", "Bash · git status", "")
    assert (result["activity_type"], result["status"], result["summary"]) == ("tool_result", "ok", "result ok")
    assert narration["activity_type"] == "progress" and narration["summary"] == ""
    assert narration["text"] == "Tests are green, opening the PR."


def test_the_summary_is_redacted_before_it_is_stored() -> None:
    log = ActivityLog()
    _record_activity_batch(
        _state(log),
        [_tool_event(arg=f"GITHUB_TOKEN={_GH_TOKEN} uv run pytest -q", timestamp="t1")],
        card_id="PVTI_148",
        stage="implementing",
    )
    (entry,) = log.snapshot()
    assert _GH_TOKEN not in _snapshot_text(log)
    assert entry["summary"] == "Bash · GITHUB_TOKEN=[REDACTED] uv run pytest -q"


def test_the_event_text_is_redacted_before_it_is_stored_too() -> None:
    log = ActivityLog()
    _record_activity_batch(
        _state(log),
        [{"type": "tool_use", "text": f"curl -H 'Authorization: Bearer {_GH_TOKEN}' x", "timestamp": "t1"}],
        card_id="PVTI_148",
        stage="implementing",
    )
    assert _GH_TOKEN not in _snapshot_text(log)


# --- redaction of commands in the log ---------------------------------------------


@pytest.mark.parametrize(
    ("command", "leaked"),
    [
        ("GITHUB_TOKEN=ghp_short123 gh pr list", "ghp_short123"),
        ("API_KEY=\"hunter two\" ./deploy.sh", "hunter two"),
        ("psql --password hunter2 -c 'select 1'", "hunter2"),
        ("curl -H \"Authorization: Bearer shorttoken\" https://api", "shorttoken"),
        ("curl -H 'Authorization: token abc123' https://api", "abc123"),
        (f"curl -H \"Authorization: Bearer {_GH_TOKEN}\" https://api", _GH_TOKEN),
    ],
)
def test_secret_forms_in_a_command_are_redacted(command: str, leaked: str) -> None:
    log = ActivityLog()
    log.record(activity_type="tool_use", card_id="PVTI_148", text="Bash", summary=f"Bash · {command}")
    (entry,) = log.snapshot()
    assert leaked not in entry["summary"]
    assert "[REDACTED]" in entry["summary"]


def test_ordinary_commands_are_not_mangled() -> None:
    log = ActivityLog()
    log.record(activity_type="tool_use", card_id="PVTI_148", text="Bash",
               summary="Bash · uv run pytest tests/unit/test_148_activity_detail.py -q")
    assert log.snapshot()[0]["summary"] == "Bash · uv run pytest tests/unit/test_148_activity_detail.py -q"


# --- the cap and the shape ---------------------------------------------------------


def test_the_summary_is_capped_at_the_named_limit() -> None:
    assert MAX_SUMMARY == 160
    log = ActivityLog()
    log.record(activity_type="tool_use", card_id="PVTI_148", text="Bash",
               summary="Bash · " + "x" * 400)
    (entry,) = log.snapshot()
    assert len(entry["summary"]) == MAX_SUMMARY
    assert entry["truncated"] is False  # the text cap is separate from the summary cap


def test_a_summary_that_is_cut_is_still_redacted_first() -> None:
    # The secret starts inside the cap; cutting before redaction would leak its prefix.
    padding = "x" * (MAX_SUMMARY - 10)
    log = ActivityLog()
    log.record(activity_type="tool_use", card_id="PVTI_148", text="Bash",
               summary=f"{padding} TOKEN=abcdefghijklmnop")
    assert "abcdefghijklmnop" not in log.snapshot()[0]["summary"]


def test_the_text_cap_is_unchanged() -> None:
    log = ActivityLog()
    log.record(activity_type="progress", card_id="PVTI_148", text="y" * (MAX_TEXT + 50))
    assert len(log.snapshot()[0]["text"]) == MAX_TEXT


def test_status_accepts_only_the_two_outcomes() -> None:
    log = ActivityLog()
    log.record(activity_type="tool_result", card_id="PVTI_148", text="r1", summary="result ok", status="ok")
    log.record(activity_type="tool_result", card_id="PVTI_148", text="r2", summary="result ?", status="weird")
    ok, weird = log.snapshot()
    assert ok["status"] == "ok"
    assert weird["status"] == ""


def test_entry_shape_gains_summary_and_status_and_keeps_the_rest() -> None:
    log = ActivityLog()
    log.record(activity_type="tool_use", card_id="PVTI_148", text="Bash", summary="Bash · ls")
    (entry,) = log.snapshot()
    assert {"summary", "status"} <= set(entry)
    assert entry["summary"] == "Bash · ls" and entry["status"] == ""
    for key in ("seq", "timestamp", "card_id", "stage", "activity_type", "text", "truncated"):
        assert key in entry


def test_tool_calls_with_different_commands_are_not_suppressed_as_duplicates() -> None:
    log = ActivityLog()
    log.record(activity_type="tool_use", card_id="PVTI_148", text="Bash", summary="Bash · ls")
    log.record(activity_type="tool_use", card_id="PVTI_148", text="Bash", summary="Bash · pwd")
    assert [e["summary"] for e in log.snapshot()] == ["Bash · ls", "Bash · pwd"]


def test_a_replayed_identical_tool_call_is_still_suppressed() -> None:
    log = ActivityLog()
    item = {"activity_type": "tool_use", "card_id": "PVTI_148", "text": "Bash", "summary": "Bash · ls",
            "source_event_id": "t9", "stream_id": ""}
    log.record_many([item])
    assert log.record_many([item]) == []
    assert len(log.snapshot()) == 1


def test_two_identical_empty_results_for_one_card_are_both_kept() -> None:
    """Regression (spec 138): `mkdir` and `git add` print nothing, so every result has the same text."""
    log = ActivityLog()
    first = {"activity_type": "tool_result", "card_id": "PVTI_148", "text": "", "summary": "result ok",
             "status": "ok", "source_event_id": "2026-10-13T09:00:01+00:00"}
    second = {**first, "source_event_id": "2026-10-13T09:00:04+00:00"}
    log.record_many([first, second])
    assert [e["activity_type"] for e in log.snapshot()] == ["tool_result", "tool_result"]


def test_a_replayed_identical_tool_result_is_still_suppressed() -> None:
    log = ActivityLog()
    item = {"activity_type": "tool_result", "card_id": "PVTI_148", "text": "", "summary": "result ok",
            "status": "ok", "source_event_id": "2026-10-13T09:00:01+00:00"}
    log.record_many([item])
    assert log.record_many([item]) == []
    assert len(log.snapshot()) == 1
