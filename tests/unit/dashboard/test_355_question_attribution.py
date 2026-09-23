"""355: Open Questions / Clarification History card attribution and lifecycle.

Regression gate for the dashboard serialization layer. The bug: questions
lived per-session but were serialised as flat unattributed strings that
outlived the blocked state that produced them (issue #355, observed live
2026-09-10 — the panel showed an env-blocker question while the board's
Blocked column had zero cards).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from coordinare.dashboard import DashboardStore
from coordinare.state_store import WorkflowSnapshot

QUESTION_106 = "Which calendar backend should the timesheet use?"
QUESTION_107 = "The implementer's local test gate hit an environment blocker."


def _snapshot(**overrides: Any) -> WorkflowSnapshot:
    fields: dict[str, Any] = {
        "snapshot_at": datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
        "phase": "blocked",
        "active_card_id": "card-abc",
        "active_card_title": "Weekly timesheet submission",
        "active_card_issue_number": 106,
        "active_card_issue_url": "https://github.com/org/repo/issues/106",
        "performer_stage": "implementing",
        "last_blocked_notified_at": datetime(2026, 9, 10, 8, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return WorkflowSnapshot(**fields)


def _metrics() -> MagicMock:
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": "2026-09-10T09:30:00+00:00",
    }
    return metrics


def _health() -> MagicMock:
    health = MagicMock()
    health.snapshot.return_value.probes = []
    return health


def _daemon(
    snapshot: WorkflowSnapshot | None,
    state: dict[str, Any] | None = None,
) -> MagicMock:
    daemon = MagicMock()
    daemon.state = state if state is not None else {"phase": "idle"}
    daemon.state_store.last_snapshot = snapshot
    return daemon


def _blocked_session(
    card_id: str,
    issue_number: int,
    questions: list[str],
    stage: str = "implementing",
) -> dict[str, Any]:
    return {
        "card_id": card_id,
        "current_card": {
            "issue_number": issue_number,
            "title": f"Card {issue_number}",
            "issue_url": f"https://github.com/org/repo/issues/{issue_number}",
        },
        "phase": "blocked",
        "open_questions": questions,
        "performer_stage": stage,
        "last_blocked_notified_at": datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
    }


def test_open_questions_attributed_while_session_blocked() -> None:
    """A blocked session's questions serialise with full card attribution."""
    store = DashboardStore()
    session = _blocked_session("card-abc", 106, [QUESTION_106])
    daemon = _daemon(_snapshot(), {"phase": "blocked", "active_sessions": {"card-abc": session}})

    snap = store.build_snapshot(daemon, _metrics(), _health())

    qs = snap["open_questions"]
    assert len(qs) == 1
    q = qs[0]
    assert q["card_id"] == "card-abc"
    assert q["card_number"] == 106
    assert q["card_title"] == "Card 106"
    assert q["stage"] == "implementing"
    assert q["text"] == QUESTION_106
    assert q["asked_at"] == "2026-09-10T09:00:00+00:00"
    assert q["issue_url"] == "https://github.com/org/repo/issues/106"


def test_open_questions_disappear_when_card_leaves_blocked_state() -> None:
    """The lifecycle rule: when the owning session's phase moves on, its
    questions leave the panel (they remain in Clarification History).

    The snapshot still carries the flat strings — pre-fix, that is exactly
    the stale payload #355 rendered — so this test fails against the old
    behaviour rather than passing vacuously."""
    store = DashboardStore()
    session = _blocked_session("card-abc", 106, [QUESTION_106])
    session["phase"] = "monitoring_performer"
    snapshot = _snapshot(open_questions=[QUESTION_106])
    daemon = _daemon(
        snapshot,
        {"phase": "monitoring_performer", "active_sessions": {"card-abc": session}},
    )

    snap = store.build_snapshot(daemon, _metrics(), _health())

    assert snap["open_questions"] == []


def test_open_questions_multi_card_unambiguous() -> None:
    """Two cards asking simultaneously are labelled unambiguously per card."""
    store = DashboardStore()
    sessions = {
        "card-106": _blocked_session("card-106", 106, [QUESTION_106], stage="implementing"),
        "card-107": _blocked_session("card-107", 107, [QUESTION_107], stage="assessing"),
    }
    daemon = _daemon(_snapshot(), {"phase": "blocked", "active_sessions": sessions})

    snap = store.build_snapshot(daemon, _metrics(), _health())

    qs = snap["open_questions"]
    assert {q["card_number"] for q in qs} == {106, 107}
    by_number = {q["card_number"]: q for q in qs}
    assert by_number[106]["text"] == QUESTION_106
    assert by_number[106]["stage"] == "implementing"
    assert by_number[107]["text"] == QUESTION_107
    assert by_number[107]["stage"] == "assessing"


def test_legacy_bare_strings_attributed_while_blocked() -> None:
    """Single-card mode: flat snapshot strings attribute to the top-level
    card while the daemon is still blocked."""
    store = DashboardStore()
    snapshot = _snapshot(open_questions=[QUESTION_106])
    daemon = _daemon(snapshot, {"phase": "blocked", "active_sessions": {}})

    snap = store.build_snapshot(daemon, _metrics(), _health())

    qs = snap["open_questions"]
    assert len(qs) == 1
    q = qs[0]
    assert q["text"] == QUESTION_106
    assert q["card_number"] == 106
    assert q["card_title"] == "Weekly timesheet submission"
    assert q["stage"] == "implementing"
    assert q["asked_at"] == "2026-09-10T08:00:00+00:00"
    assert q["issue_url"] == "https://github.com/org/repo/issues/106"


def test_legacy_bare_strings_hidden_when_not_blocked() -> None:
    """The live incident (#355): the panel must not outlive the blocked state.
    The snapshot still carries the questions, but the daemon's live phase has
    moved on, so the panel is empty."""
    store = DashboardStore()
    snapshot = _snapshot(open_questions=[QUESTION_107])
    daemon = _daemon(snapshot, {"phase": "idle", "active_sessions": {}})

    snap = store.build_snapshot(daemon, _metrics(), _health())

    assert snap["open_questions"] == []


def test_clarification_rounds_attributed_to_card_and_stage() -> None:
    """Each Clarification History round is labelled with the card and stage
    it belongs to."""
    store = DashboardStore()
    snapshot = _snapshot(
        card_clarifications=[{"questions": [QUESTION_106], "answer": "Use the ical backend."}],
    )
    daemon = _daemon(snapshot, {"phase": "idle", "active_sessions": {}})

    snap = store.build_snapshot(daemon, _metrics(), _health())

    rounds = snap["card_clarifications"]
    assert len(rounds) == 1
    rnd = rounds[0]
    assert rnd["questions"] == [QUESTION_106]
    assert rnd["answer"] == "Use the ical backend."
    assert rnd["card_number"] == 106
    assert rnd["card_title"] == "Weekly timesheet submission"
    assert rnd["stage"] == "implementing"
    assert rnd["issue_url"] == "https://github.com/org/repo/issues/106"
