"""Tests for AttemptLog — spec 141 attempt telemetry."""
from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from coordinare.attempt_log import AttemptLog, map_verdict


@pytest.fixture()
def log(tmp_path: Path) -> AttemptLog:
    """Return an AttemptLog writing to a temporary directory."""
    return AttemptLog(log_dir=tmp_path)


def _read_rows(tmp_path: Path) -> list[dict[str, Any]]:
    """Load all JSONL rows written under tmp_path, across all daily files in sorted order."""
    files = sorted(tmp_path.glob("*.jsonl"))
    assert files, "No JSONL file was written"
    rows = []
    for f in files:
        for line in f.read_text().splitlines():
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# open_attempt — start row
# ---------------------------------------------------------------------------


def test_open_attempt_writes_a_row(log: AttemptLog, tmp_path: Path) -> None:
    """open_attempt writes exactly one row to the JSONL file."""
    log.open_attempt(task_id="card-1", routing_reason="default_policy")
    rows = _read_rows(tmp_path)
    assert len(rows) == 1


def test_open_attempt_row_has_required_fields(log: AttemptLog, tmp_path: Path) -> None:
    """Start row contains all required decision-time fields."""
    log.open_attempt(task_id="card-1", routing_reason="default_policy")
    row = _read_rows(tmp_path)[0]
    assert row["schema_version"] == 1
    assert row["task_id"] == "card-1"
    assert row["routing_reason"] == "default_policy"
    assert row["source"] == "live"
    assert "attempt_id" in row
    assert "started_at" in row


def test_open_attempt_outcome_fields_are_null(log: AttemptLog, tmp_path: Path) -> None:
    """Start row has null outcome fields — nothing has happened yet."""
    log.open_attempt(task_id="card-1", routing_reason="default_policy")
    row = _read_rows(tmp_path)[0]
    assert row["ended_at"] is None
    assert row["verdict"] is None
    assert row["terminal_state"] is None
    assert row["wall_time_s"] is None


def test_open_attempt_schema_version_is_always_1(log: AttemptLog, tmp_path: Path) -> None:
    """schema_version is always 1 for this spec."""
    log.open_attempt(task_id="card-1", routing_reason="default_policy")
    row = _read_rows(tmp_path)[0]
    assert row["schema_version"] == 1


def test_open_attempt_parent_attempt_id_set_on_bounce(log: AttemptLog, tmp_path: Path) -> None:
    """parent_attempt_id is populated when a previous attempt ID is provided."""
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    log.open_attempt(task_id="card-1", routing_reason="default_policy", parent_attempt_id=attempt_id)
    rows = _read_rows(tmp_path)
    assert rows[1]["parent_attempt_id"] == attempt_id


def test_open_attempt_parent_attempt_id_null_on_first_attempt(log: AttemptLog, tmp_path: Path) -> None:
    """parent_attempt_id is null when there is no previous attempt."""
    log.open_attempt(task_id="card-1", routing_reason="default_policy")
    row = _read_rows(tmp_path)[0]
    assert row["parent_attempt_id"] is None


def test_open_attempt_writes_spec_fields_to_row(log: AttemptLog, tmp_path: Path) -> None:
    """spec_* kwargs are written to the start row."""
    log.open_attempt(
        task_id="card-1",
        routing_reason="default_policy",
        spec_word_count=120,
        spec_has_acceptance_tests=True,
    )
    row = _read_rows(tmp_path)[0]
    assert row["spec_word_count"] == 120
    assert row["spec_has_acceptance_tests"] is True


# ---------------------------------------------------------------------------
# close_attempt — end row
# ---------------------------------------------------------------------------


def test_close_attempt_writes_a_second_row(log: AttemptLog, tmp_path: Path) -> None:
    """close_attempt appends an end row — two rows total per attempt."""
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    log.close_attempt(attempt_id=attempt_id, verdict="pass", verdict_source="qa_role")
    rows = _read_rows(tmp_path)
    assert len(rows) == 2


def test_close_attempt_shares_attempt_id_with_start_row(log: AttemptLog, tmp_path: Path) -> None:
    """End row has the same attempt_id as the start row."""
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    log.close_attempt(attempt_id=attempt_id, verdict="pass", verdict_source="qa_role")
    rows = _read_rows(tmp_path)
    assert rows[0]["attempt_id"] == rows[1]["attempt_id"]


def test_close_attempt_uses_path_from_open_not_current_date(tmp_path: Path) -> None:
    """End row goes to the same file as the start row even if the date changes at midnight."""
    from datetime import datetime
    log = AttemptLog(log_dir=tmp_path)
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    tomorrow = datetime(2099, 1, 1, tzinfo=UTC)
    with patch("coordinare.attempt_log.datetime") as fake_dt:
        fake_dt.now.return_value = tomorrow
        fake_dt.fromisoformat = datetime.fromisoformat
        log.close_attempt(attempt_id=attempt_id, verdict="pass", verdict_source="qa_role")
    assert not (tmp_path / "2099-01-01.jsonl").exists()
    assert len(_read_rows(tmp_path)) == 2


def test_close_attempt_task_id_lookup_is_keyed_by_attempt_id(log: AttemptLog, tmp_path: Path) -> None:
    """Closing one attempt writes that attempt's task_id, not another open attempt's."""
    attempt_a = log.open_attempt(task_id="card-A", routing_reason="default_policy")
    log.open_attempt(task_id="card-B", routing_reason="default_policy")
    log.close_attempt(attempt_id=attempt_a, verdict="pass", verdict_source="qa_role")
    rows = _read_rows(tmp_path)
    end_row = next(r for r in rows if r.get("verdict") == "pass")
    assert end_row["task_id"] == "card-A"
    assert end_row["task_id"] != "card-B"
    assert end_row["started_at"] is not None


def test_close_attempt_populates_outcome_fields(log: AttemptLog, tmp_path: Path) -> None:
    """End row has verdict, verdict_source, ended_at, and wall_time_s populated."""
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    log.close_attempt(attempt_id=attempt_id, verdict="pass", verdict_source="qa_role")
    end_row = _read_rows(tmp_path)[1]
    assert end_row["verdict"] == "pass"
    assert end_row["verdict_source"] == "qa_role"
    assert end_row["ended_at"] is not None
    assert end_row["wall_time_s"] is not None


def test_close_attempt_terminal_state_merged(log: AttemptLog, tmp_path: Path) -> None:
    """terminal_state is written correctly when a card merges."""
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    log.close_attempt(attempt_id=attempt_id, verdict="pass", verdict_source="qa_role", terminal_state="merged")
    end_row = _read_rows(tmp_path)[1]
    assert end_row["terminal_state"] == "merged"


def test_close_attempt_maps_raw_marker_at_boundary(log: AttemptLog, tmp_path: Path) -> None:
    """close_attempt normalises a raw marker so FR-007 holds even if the caller skips map_verdict."""
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    log.close_attempt(attempt_id=attempt_id, verdict="qa_passed", verdict_source="qa_role")  # type: ignore[arg-type]
    end_row = _read_rows(tmp_path)[1]
    assert end_row["verdict"] == "pass"


def test_close_attempt_terminal_state_blocked(log: AttemptLog, tmp_path: Path) -> None:
    """terminal_state is written correctly when a card is blocked."""
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    log.close_attempt(attempt_id=attempt_id, verdict="fail", verdict_source="qa_role", terminal_state="blocked")
    end_row = _read_rows(tmp_path)[1]
    assert end_row["terminal_state"] == "blocked"


# ---------------------------------------------------------------------------
# map_verdict — StageVerdict taxonomy enforcement
# ---------------------------------------------------------------------------


def test_map_verdict_known_values() -> None:
    """Real monitor_performer stage markers map to the correct taxonomy."""
    # pass
    assert map_verdict("approved") == "pass"
    assert map_verdict("qa_passed") == "pass"
    assert map_verdict("security_passed") == "pass"
    assert map_verdict("docs_committed") == "pass"
    # fail
    assert map_verdict("changes_requested") == "fail"
    assert map_verdict("qa_failed") == "fail"
    assert map_verdict("security_failed") == "fail"
    # error
    assert map_verdict("error") == "error"
    assert map_verdict("blocked") == "error"
    assert map_verdict("env_blocked") == "error"
    assert map_verdict("qa_env_blocked") == "error"
    # timeout
    assert map_verdict("session_expired") == "timeout"
    assert map_verdict("token_limit") == "timeout"
    assert map_verdict("idle_timeout") == "timeout"


def test_map_verdict_is_idempotent() -> None:
    """Applying map_verdict to an already-mapped value returns the same value."""
    for v in ("pass", "fail", "error", "timeout"):
        assert map_verdict(v) == v


def test_map_verdict_unknown_maps_to_error_not_fail() -> None:
    """Unrecognised values must map to 'error', never 'fail'."""
    assert map_verdict("banana") == "error"
    assert map_verdict("") == "error"
    assert map_verdict("qa_passed_v2") == "error"
    # non-terminal markers must never produce a verdict other than error
    assert map_verdict("partial_progress") == "error"
    assert map_verdict("working") == "error"


def test_map_verdict_strips_whitespace_and_lowercases() -> None:
    """Whitespace and casing in raw marker values are normalised before mapping."""
    assert map_verdict("  QA_PASSED  ") == "pass"
    assert map_verdict("CHANGES_REQUESTED") == "fail"


# ---------------------------------------------------------------------------
# JSONL integrity
# ---------------------------------------------------------------------------



def test_open_without_close_leaves_readable_incomplete_row(log: AttemptLog, tmp_path: Path) -> None:
    """A start row with no matching end row (daemon restart) loads cleanly."""
    log.open_attempt(task_id="card-1", routing_reason="default_policy")
    rows = _read_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["ended_at"] is None
    assert rows[0]["verdict"] is None


# ---------------------------------------------------------------------------
# Unknown attempt_id — daemon restart gap (A-008)
# ---------------------------------------------------------------------------


def test_close_unknown_attempt_logs_warning(tmp_path: Path) -> None:
    """Closing an unknown attempt_id logs a warning rather than failing silently."""
    import structlog.testing
    log = AttemptLog(log_dir=tmp_path)
    with structlog.testing.capture_logs() as captured:
        log.close_attempt(attempt_id="no-such-id", verdict="error", verdict_source="qa_role")
    assert any(
        r["event"] == "attempt_log.close_unknown_attempt" and r["log_level"] == "warning"
        for r in captured
    )


# ---------------------------------------------------------------------------
# Error handling — write failures must never crash card processing
# ---------------------------------------------------------------------------


def test_write_failure_does_not_raise(tmp_path: Path) -> None:
    """A disk write failure is caught and logged as a structlog warning (FR-010)."""
    import structlog.testing
    log = AttemptLog(log_dir=tmp_path)
    with structlog.testing.capture_logs() as captured, patch("builtins.open", side_effect=OSError("disk full")):
        log.open_attempt(task_id="card-1", routing_reason="default_policy")
    assert any(r["event"] == "attempt_log.write_failed" and r["log_level"] == "warning" for r in captured)


def test_close_write_failure_does_not_raise(log: AttemptLog, tmp_path: Path) -> None:
    """A disk write failure in close_attempt is caught and logged as a structlog warning (FR-010)."""
    import structlog.testing
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    with structlog.testing.capture_logs() as captured, patch("builtins.open", side_effect=OSError("disk full")):
        log.close_attempt(attempt_id=attempt_id, verdict="pass", verdict_source="qa_role")
    assert any(r["event"] == "attempt_log.write_failed" and r["log_level"] == "warning" for r in captured)


def test_log_dir_created_on_first_write(tmp_path: Path) -> None:
    """log_dir is created automatically if it doesn't exist yet (A-003)."""
    log_dir = tmp_path / "nested" / "dir"
    log = AttemptLog(log_dir=log_dir)
    log.open_attempt(task_id="card-1", routing_reason="default_policy")
    assert log_dir.exists()


def test_each_row_is_newline_terminated(log: AttemptLog, tmp_path: Path) -> None:
    """Every row ends with a newline so appending never corrupts the previous row (FR-003)."""
    attempt_id = log.open_attempt(task_id="card-1", routing_reason="default_policy")
    log.close_attempt(attempt_id=attempt_id, verdict="pass", verdict_source="qa_role")
    files = list(tmp_path.glob("*.jsonl"))
    assert files[0].read_text().endswith("\n")
