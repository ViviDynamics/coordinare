"""331: a still-valid blueprint must survive a restart instead of being replanned.

Website #160 committed "chore: add architecture plan for #160" three separate
times across restarts. The spec-125 verdict cache could never help: it keys on
the live PR head, and at planning time there is no PR. So planning reuse is
keyed on the REQUIREMENTS instead.

These tests deliberately stamp the signature through `stamp_blueprint_signature`
-- the SAME entry point monitor_performer uses when it lifts a blueprint. The
first cut of this suite computed the signature itself, which meant every test
passed while the production path stored a signature derived from a state key
that does not exist, so reuse never once fired. A test that recomputes the
value under test proves nothing.
"""
from __future__ import annotations

from coordinare.graph.nodes.dispatch_performer import (
    blueprint_requirements_signature,
    blueprint_reuse_allowed,
    stamp_blueprint_signature,
)
from coordinare.graph.state import initial_state

CARD = {"id": "PVTI_card", "title": "Schema S1: keyed project_assignments"}
PLAN = {"milestones": [{"name": "m1"}], "size": "medium"}


def _planned(**over):
    """A state as it stands just after the architect reported a blueprint."""
    st = initial_state()
    st["current_card"] = dict(CARD)
    st["blueprint"] = dict(PLAN)
    st["card_clarifications"] = []
    st["requirements_changed"] = False
    st["relay_feedback"] = []
    stamp_blueprint_signature(st)  # exactly what monitor_performer does
    st.update(over)
    return st


def test_the_stamped_signature_is_the_one_reuse_checks() -> None:
    """The regression that mattered: stamping and checking must agree. They
    were computed from different cards, so the feature was inert in production
    while every unit test passed."""
    st = _planned()
    assert st["blueprint_signature"] == blueprint_requirements_signature(st)
    assert blueprint_reuse_allowed(st, "architecting") is True


def test_the_signature_reads_the_real_card_key() -> None:
    """Guards the specific slip: the card must come from `current_card`, which
    exists on CoordinareState. A key that is always missing yields a constant
    signature that happens to be self-consistent in a hand-rolled test."""
    a = _planned()
    b = _planned()
    b["current_card"] = {"id": "PVTI_other", "title": "Something else"}
    assert blueprint_requirements_signature(a) != blueprint_requirements_signature(b)


def test_unchanged_requirements_reuse_the_plan() -> None:
    assert blueprint_reuse_allowed(_planned(), "architecting") is True


def test_new_clarification_forces_replanning() -> None:
    st = _planned()
    st["card_clarifications"] = [{"comment_id": 99, "text": "use a join table"}]
    assert blueprint_reuse_allowed(st, "architecting") is False


def test_changed_requirements_force_replanning() -> None:
    assert blueprint_reuse_allowed(_planned(requirements_changed=True), "architecting") is False


def test_queued_feedback_forces_replanning() -> None:
    st = _planned(relay_feedback=[{"body": "reviewer wants a different shape"}])
    assert blueprint_reuse_allowed(st, "architecting") is False


def test_retitled_card_forces_replanning() -> None:
    st = _planned()
    st["current_card"] = {"id": CARD["id"], "title": "Schema S1: DROP project_assignments"}
    assert blueprint_reuse_allowed(st, "architecting") is False


def test_no_blueprint_means_no_reuse() -> None:
    assert blueprint_reuse_allowed(_planned(blueprint=None), "architecting") is False
    assert blueprint_reuse_allowed(_planned(blueprint={}), "architecting") is False


def test_missing_signature_fails_closed() -> None:
    """A pre-331 snapshot has a blueprint but no signature: replan once."""
    st = _planned(blueprint_signature=None)
    assert blueprint_reuse_allowed(st, "architecting") is False


def test_only_the_architect_stage_is_short_circuited() -> None:
    for stage in ("implementing", "reviewing", "documenting", "qa", None):
        assert blueprint_reuse_allowed(_planned(), stage) is False


def test_clarification_order_does_not_change_the_signature() -> None:
    """Board polling order must not spuriously invalidate a good plan."""
    a = _planned()
    a["card_clarifications"] = [{"comment_id": 1}, {"comment_id": 2}]
    b = _planned()
    b["card_clarifications"] = [{"comment_id": 2}, {"comment_id": 1}]
    assert blueprint_requirements_signature(a) == blueprint_requirements_signature(b)


def test_signature_survives_a_persist_restore_round_trip() -> None:
    """The feature exists to survive a restart, so prove it does."""
    st = _planned()
    stored = st["blueprint_signature"]
    restored = initial_state()
    restored["current_card"] = dict(CARD)
    restored["blueprint"] = dict(PLAN)
    restored["card_clarifications"] = []
    restored["requirements_changed"] = False
    restored["relay_feedback"] = []
    restored["blueprint_signature"] = stored  # what daemon restore puts back
    assert blueprint_reuse_allowed(restored, "architecting") is True
