"""065 US3 — integration tests exploiting the stranded-performer kick-back path.

Background
----------
When a card is manually moved from ``IN_PROGRESS`` back to ``TODO`` while its
session is still present in ``daemon.state["active_sessions"]`` (the
``phase`` of the session is still ``monitoring_performer``), the daemon's
per-cycle eligibility check (``_compute_eligibility``) only inspects the
``BLOCKED`` column and the dependency graph.  A kicked-back card is in
neither ``BLOCKED`` nor dependency-blocked, so the session is reported as
``eligible=True`` and the cycle proceeds as if the operator never moved the
card.  The performer container that owns the session keeps running, its
results land in a state that the daemon will discard, and no log record
explains the situation — a silent strand (spec 065 US3, FR-009).

What these tests assert
-----------------------
- ``_compute_eligibility`` MUST return ``eligible=False`` with
  ``reason="kicked_back"`` when a session is in the ``monitoring_performer``
  phase but the card's ``content_id`` is NOT in the ``IN_PROGRESS`` column
  on the latest board snapshot (and is present in some other column —
  TODO/BACKLOG/etc.).  On current ``main`` the function returns
  ``eligible=True`` and is the root cause of the strand.
- A separate test pins the skip-loop log contract (FR-009 option b): when
  the eligibility result carries ``reason="kicked_back"``, the daemon
  MUST emit a ``dispatcher.performer_orphaned`` structured log carrying
  ``{performer_id, card_id, reason}``.  The performer_id is derived from
  the session's ``performer_stage`` (the daemon does not store a separate
  performer_id on the session; the stage uniquely identifies the
  responsible performer pool slot).
- Controls confirm IN_PROGRESS cards remain eligible (no false positives)
  and that sessions whose phase is not ``monitoring_performer`` (e.g.
  ``dispatching``) are not flagged as orphaned — only an actively-running
  performer can be orphaned.
"""
from __future__ import annotations

import structlog

from coordinare.daemon import _compute_eligibility


def _session(
    *,
    content_id: str = "PVTI_A",
    phase: str = "monitoring_performer",
    performer_stage: str = "implementing",
    title: str = "Fix the auth bug",
) -> dict:
    return {
        "current_card": {
            "id": content_id,
            "content_id": content_id,
            "title": title,
        },
        "phase": phase,
        "performer_stage": performer_stage,
    }


# ---------------------------------------------------------------------------
# Exploit: eligibility silently passes for kicked-back cards
# ---------------------------------------------------------------------------


def test_compute_eligibility_flags_kicked_back_card_in_todo() -> None:
    """Exploit: card A is in ``monitoring_performer`` phase, but the latest
    board snapshot shows its content_id has been moved to ``TODO``.  The
    eligibility check must recognise this as a kick-back and return
    ``eligible=False, reason="kicked_back"`` so the surrounding skip-log
    loop can emit ``dispatcher.performer_orphaned`` for it.

    On current ``main``, ``_compute_eligibility`` only inspects ``BLOCKED``
    and the dep graph — a TODO-column card is treated as eligible, the
    performer keeps running, and no log fires.  This test pins the missing
    detection.
    """
    session = _session(content_id="PVTI_A")
    board_snapshot = {
        "TODO": ["PVTI_A"],
        "IN_PROGRESS": [],
        "BLOCKED": [],
    }
    elig = _compute_eligibility("PVTI_A", session, board_snapshot, None)
    assert elig.eligible is False, (
        f"kicked-back card silently treated as eligible: {elig!r}. "
        "_compute_eligibility must recognise that a session in "
        "monitoring_performer phase whose content_id is no longer in "
        "IN_PROGRESS has been kicked back."
    )
    assert elig.reason == "kicked_back", (
        f"expected reason='kicked_back', got {elig.reason!r}"
    )


def test_compute_eligibility_flags_kicked_back_card_in_backlog() -> None:
    """Variant: BACKLOG is also a valid kick-back destination — the operator
    might park the card without committing to redo it.  The contract is the
    same: not in IN_PROGRESS while session is monitoring_performer → orphan."""
    session = _session(content_id="PVTI_B")
    board_snapshot = {
        "TODO": [],
        "IN_PROGRESS": [],
        "BACKLOG": ["PVTI_B"],
        "BLOCKED": [],
    }
    elig = _compute_eligibility("PVTI_B", session, board_snapshot, None)
    assert elig.eligible is False
    assert elig.reason == "kicked_back"


# ---------------------------------------------------------------------------
# Controls — no false positives
# ---------------------------------------------------------------------------


def test_compute_eligibility_passes_when_card_still_in_progress() -> None:
    """Control: a card actively in IN_PROGRESS with no dep blockers and
    not in BLOCKED is eligible.  Guards against the kick-back detection
    firing on the happy path."""
    session = _session(content_id="PVTI_A")
    board_snapshot = {
        "TODO": [],
        "IN_PROGRESS": ["PVTI_A"],
        "BLOCKED": [],
    }
    elig = _compute_eligibility("PVTI_A", session, board_snapshot, None)
    assert elig.eligible is True
    assert elig.reason == "eligible"


def test_compute_eligibility_does_not_flag_kickback_for_non_performer_phase() -> None:
    """Control: a session that is NOT in ``monitoring_performer`` (e.g.
    still in ``dispatching`` or already in ``monitoring_pr``) is not an
    orphaned-performer candidate even if the card has shifted columns —
    only a running performer can be stranded.  Guards against the
    kick-back rule misfiring during legitimate phase transitions where
    the daemon itself has moved the card."""
    session = _session(content_id="PVTI_A", phase="dispatching")
    board_snapshot = {
        "TODO": ["PVTI_A"],
        "IN_PROGRESS": [],
        "BLOCKED": [],
    }
    elig = _compute_eligibility("PVTI_A", session, board_snapshot, None)
    # Either eligible=True or eligible=False with a non-kicked_back reason;
    # the point is that we do NOT label this an orphaned performer.
    assert elig.reason != "kicked_back", (
        f"non-performer phase misclassified as kick-back: {elig!r}"
    )


def test_compute_eligibility_blocked_takes_precedence_over_kickback() -> None:
    """Control: when a card is in BLOCKED, that reason wins — the operator
    explicitly blocked it; ``performer_orphaned`` would be a misleading log
    in that case."""
    session = _session(content_id="PVTI_A")
    board_snapshot = {
        "TODO": [],
        "IN_PROGRESS": [],
        "BLOCKED": ["PVTI_A"],
    }
    elig = _compute_eligibility("PVTI_A", session, board_snapshot, None)
    assert elig.eligible is False
    assert elig.reason == "blocked_column"


# ---------------------------------------------------------------------------
# FR-009/b log contract
# ---------------------------------------------------------------------------


def test_kicked_back_session_emits_performer_orphaned_log() -> None:
    """FR-009 option (b): when a session is flagged as kicked_back during
    the per-cycle skip-log emission, a ``dispatcher.performer_orphaned``
    structured record MUST be emitted carrying ``performer_id``,
    ``card_id`` and ``reason``.  The performer_id is derived from the
    session's ``performer_stage`` (the daemon's session dict does not
    persist a separate performer_id; the stage uniquely identifies the
    responsible performer pool slot).
    """
    from coordinare.daemon import _log_session_skip

    session = _session(content_id="PVTI_A", performer_stage="implementing")
    elig = _compute_eligibility(
        "PVTI_A",
        session,
        {"TODO": ["PVTI_A"], "IN_PROGRESS": [], "BLOCKED": []},
        None,
    )
    assert elig.reason == "kicked_back"  # precondition

    with structlog.testing.capture_logs() as logs:
        _log_session_skip("PVTI_A", session, elig)

    orphaned = [
        r for r in logs if r.get("event") == "dispatcher.performer_orphaned"
    ]
    assert orphaned, (
        "kicked-back session did not emit dispatcher.performer_orphaned; "
        f"got events: {[r.get('event') for r in logs]}"
    )
    record = orphaned[0]
    assert record.get("card_id") == "PVTI_A"
    assert record.get("reason") == "kicked_back"
    assert record.get("performer_id") == "implementing", (
        "performer_id should derive from performer_stage when no explicit "
        f"performer_id is stored on the session: got {record.get('performer_id')!r}"
    )


def test_non_orphan_skips_do_not_emit_performer_orphaned_log() -> None:
    """Control: ``blocked_column`` / ``dependency_blocked`` / ``missing_card``
    are NOT performer-orphaning events.  They should continue to emit
    ``session_skipped`` and never ``dispatcher.performer_orphaned``."""
    from coordinare.daemon import _log_session_skip

    session = _session(content_id="PVTI_A")
    elig = _compute_eligibility(
        "PVTI_A",
        session,
        {"BLOCKED": ["PVTI_A"]},
        None,
    )
    assert elig.reason == "blocked_column"
    with structlog.testing.capture_logs() as logs:
        _log_session_skip("PVTI_A", session, elig)
    assert not [r for r in logs if r.get("event") == "dispatcher.performer_orphaned"]
    assert [r for r in logs if r.get("event") == "session_skipped"], (
        "expected the existing session_skipped log for non-orphan skips"
    )
