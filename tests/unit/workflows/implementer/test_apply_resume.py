"""Tests for apply_resume (spec 171 FR-007, FR-009).

Mutation check protocol (FR-015): the header names a change to the rule that
must make a test in this file fail. Apply it in the real tree to verify.
- Mutation: skip every "done" milestone instead of a contiguous prefix (drop the
  ``in_prefix`` guard) -> test_a_done_milestone_behind_an_open_one_still_runs fails
- Mutation: leave a "tests_only" milestone in its original lane ->
  test_tests_only_is_relaned_to_the_tests_lane fails
- Mutation: put skipped milestones into remaining as well ->
  test_a_done_prefix_is_skipped fails
"""

import pytest
from performer.workflows.implementer.models import MilestonePlan
from performer.workflows.implementer.resume import apply_resume


def _plans(n: int) -> list[MilestonePlan]:
    return [
        MilestonePlan(index=i, goal=f"milestone {i}", scope=f"src/m{i}.py, tests/test_m{i}.py", done_when=f"m{i} passes", lane="feature", lane_source="brief")
        for i in range(n)
    ]


class TestApplyResume:
    def test_nothing_done_runs_the_whole_plan(self):
        plans = _plans(3)
        skipped, remaining = apply_resume(plans, ["open", "open", "open"])
        assert skipped == []
        assert [p.index for p in remaining] == [0, 1, 2]
        assert all(p.lane == "feature" for p in remaining)

    def test_a_done_prefix_is_skipped(self):
        plans = _plans(3)
        skipped, remaining = apply_resume(plans, ["done", "done", "open"])
        assert [p.index for p in skipped] == [0, 1]
        assert [p.index for p in remaining] == [2]

    def test_a_done_milestone_behind_an_open_one_still_runs(self):
        """An open milestone changes the tree the later one was judged against."""
        plans = _plans(3)
        skipped, remaining = apply_resume(plans, ["open", "done", "done"])
        assert skipped == []
        assert [p.index for p in remaining] == [0, 1, 2]

    def test_tests_only_is_relaned_to_the_tests_lane(self):
        plans = _plans(2)
        skipped, remaining = apply_resume(plans, ["done", "tests_only"])
        assert [p.index for p in skipped] == [0]
        assert len(remaining) == 1
        assert remaining[0].lane == "tests"
        assert remaining[0].lane_source == "resume"
        assert remaining[0].goal == "milestone 1" and remaining[0].scope == plans[1].scope

    def test_a_tests_only_prefix_is_not_skipped(self):
        plans = _plans(2)
        skipped, remaining = apply_resume(plans, ["tests_only", "open"])
        assert skipped == []
        assert [p.lane for p in remaining] == ["tests", "feature"]

    def test_everything_done_leaves_nothing_to_run(self):
        plans = _plans(2)
        skipped, remaining = apply_resume(plans, ["done", "done"])
        assert [p.index for p in skipped] == [0, 1]
        assert remaining == []

    def test_the_original_plans_are_not_mutated(self):
        plans = _plans(2)
        apply_resume(plans, ["done", "tests_only"])
        assert [p.lane for p in plans] == ["feature", "feature"]

    def test_a_state_per_plan_is_required(self):
        with pytest.raises(ValueError):
            apply_resume(_plans(2), ["done"])
