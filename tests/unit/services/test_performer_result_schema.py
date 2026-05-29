"""Spec 076 T072 — PerformerSuccessResult schema unit tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.services.performer_result_schema import PerformerSuccessResult


def test_minimal_done_outcome_validates() -> None:
    r = PerformerSuccessResult(outcome="done")
    assert r.outcome == "done"
    assert r.pushed_branch is None
    assert r.pr_url is None


def test_done_with_full_artefact_set_validates() -> None:
    r = PerformerSuccessResult(
        outcome="done",
        pushed_branch="coordinare/PVTI_X/feat-test",
        pr_url="https://github.com/x/y/pull/148",
        pr_node_id="PR_kwDO_148",
        pr_number=148,
        head_sha="2b6569b7520db31f41ab2aca829ecd020e03ff05",
    )
    assert r.pr_number == 148
    assert r.head_sha.startswith("2b6569b7")


def test_partial_progress_with_next_focus() -> None:
    r = PerformerSuccessResult(outcome="partial_progress", next_focus="finish migrations")
    assert r.next_focus == "finish migrations"


def test_idle_timeout_outcome_validates() -> None:
    """076 FR-019: idle_timeout is one of the four recognised outcomes."""
    r = PerformerSuccessResult(outcome="idle_timeout")
    assert r.outcome == "idle_timeout"


def test_blocked_with_comment() -> None:
    r = PerformerSuccessResult(outcome="blocked", comment="needs human review")
    assert r.outcome == "blocked"
    assert r.comment == "needs human review"


def test_invalid_outcome_rejected() -> None:
    with pytest.raises(ValidationError):
        PerformerSuccessResult(outcome="explosion")  # type: ignore[arg-type]


def test_pr_number_must_be_int() -> None:
    with pytest.raises(ValidationError):
        PerformerSuccessResult(outcome="done", pr_number="not a number")  # type: ignore[arg-type]


def test_extra_fields_are_tolerated() -> None:
    """ConfigDict(extra='ignore') — extra fields on the wire don't reject
    the message.  Lets older performers send extra status fields without
    breaking the validator."""
    r = PerformerSuccessResult.model_validate({
        "outcome": "done",
        "pushed_branch": "x",
        "internal_telemetry_field": "anything",
    })
    assert r.outcome == "done"
    assert r.pushed_branch == "x"
