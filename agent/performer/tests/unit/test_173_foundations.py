"""Spec 173 foundations: the two terminal statuses and Score.project_id.

The status test exists because of a specific trap: TERMINAL_STATUSES is derived
from the closed Literal, and handle_status's role cascade falls through to the
implementer tail that lints, pushes and opens a pull request. A role whose
status is missing from the Literal hangs; a role whose branch is missing opens
pull requests. See specs/173-advocate-curator-performers/research.md D4.
"""
from __future__ import annotations

import pytest

from performer.protocol import FAILURE_STATUSES, TERMINAL_STATUSES


@pytest.mark.parametrize("status", ["advocate_complete", "curation_complete"])
def test_the_new_role_statuses_terminate_a_job(status: str) -> None:
    assert status in TERMINAL_STATUSES, (
        "a status absent from the closed Literal never breaks the job poll loop"
    )


@pytest.mark.parametrize("status", ["advocate_complete", "curation_complete"])
def test_the_new_role_statuses_are_successes(status: str) -> None:
    assert status not in FAILURE_STATUSES


def test_score_carries_the_board_id() -> None:
    from performer.models import Score

    score = Score(
        title="t", repo_url="https://github.com/o/r", branch="b", project_id="PVT_123"
    )
    assert score.project_id == "PVT_123"


def test_score_defaults_the_board_id_to_empty() -> None:
    from performer.models import Score

    score = Score(title="t", repo_url="https://github.com/o/r", branch="b")
    assert score.project_id == ""


def test_the_board_id_survives_a_dispatch_payload_round_trip() -> None:
    """Score uses extra="ignore", so an undeclared field is dropped in silence.
    This is the guard that project_id is declared and not merely passed."""
    from performer.models import Score

    payload = {
        "title": "t", "repo_url": "https://github.com/o/r", "branch": "b",
        "project_id": "PVT_kwDO", "role": "curator",
    }
    assert Score(**payload).project_id == "PVT_kwDO"
