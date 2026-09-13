"""A salvaged commit must never let a milestone be skipped (#393/#395).

MUTATION THAT MUST FAIL A TEST HERE: add ``"wip"`` to
``resume.PRIOR_RUN_PREFIXES``. Three independent reviewers read
``SALVAGE_PREFIX = "wip"`` next to ``PRIOR_RUN_PREFIXES`` without it, called the
mismatch a critical defect, and proposed exactly that change. It is the wrong
fix, and this file exists so the next reader finds the reason attached to a
failing test rather than re-deriving it.

Spec-171 ``done`` means SKIP THE MILESTONE. A ``test(#N):`` commit earns that
because the driver writes it only after red was observed. A ``wip(#N):`` commit
is the opposite: the milestone FAILED. Card #160 failed with a spec that passed
the moment it was written, so counting the salvage would read that milestone as
``done`` and skip it -- converting an honest failure into a silent false green,
on the exact card the salvage feature was built for.
"""
from __future__ import annotations

import performer.workflows.implementer.resume as resume
from performer.workflows.implementer.models import MilestonePlan
from performer.workflows.implementer.resume import prior_run_paths, resume_state
from performer.workflows.implementer.salvage import SALVAGE_PREFIX, salvage_message

SPEC = "spec/models/widget_spec.rb"


def _salvage_subject(issue: int = 160) -> str:
    return salvage_message(issue, "The new spec passed immediately").splitlines()[0]


def test_salvage_prefix_is_outside_the_resume_prefixes() -> None:
    """The separation is the invariant, not an accident of naming."""
    assert SALVAGE_PREFIX not in resume.PRIOR_RUN_PREFIXES


def test_a_salvaged_commit_is_not_prior_work() -> None:
    subject = _salvage_subject()
    assert subject.startswith(f"{SALVAGE_PREFIX}(#160):")
    assert prior_run_paths([(subject, [SPEC])], 160) == frozenset()


def test_a_validated_commit_still_is_prior_work() -> None:
    """The control: the rule rejects salvage specifically, not every commit."""
    assert prior_run_paths([("test(#160): failing tests for widget", [SPEC])], 160) == frozenset({SPEC})


def test_card_160_stays_open_when_its_salvaged_spec_passes() -> None:
    """The whole point: a vacuous spec that passes must not read as done."""
    milestone = MilestonePlan(
        index=0, goal="widget spec", done_when="spec passes", scope=SPEC, lane="tests"
    )
    prior = prior_run_paths([(_salvage_subject(), [SPEC])], 160)
    state = resume_state(
        milestone, prior, frozenset({SPEC}), passed=[SPEC], failed=[]
    )
    assert state == "open", "a salvaged, unvalidated spec must not skip its milestone"
