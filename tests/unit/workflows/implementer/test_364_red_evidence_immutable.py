"""An implement turn cannot edit the tests that established red (#364).

The red observation is the evidence that the test tests something. While an
implement turn could rewrite the milestone's own test files, that evidence was
retroactively editable: judge any failure the expected red, then weaken the
test during green until it passes. CI runs the weakened test and passes too, so
no downstream gate catches it -- which matters precisely because the design in
#364 puts the red judgement in the model's hands and relies on those gates.

The original rule blocked only *another* milestone's tests, to stop a turn
pre-empting a later milestone's tests turn. That reason is preserved; this
widens it to the milestone's own tests, which is what the function's docstring
already claimed.
"""
from __future__ import annotations

from performer.workflows.implementer.cycle import scope_violations

OWN = "spec/models/timesheet/week_spec.rb"
OTHER = "spec/models/timesheet/entry_spec.rb"
SOURCE = "app/models/timesheet/week.rb"


def _reverted(violations: list[dict]) -> set[str]:
    return {v["path"] for v in violations}


def _kind_for(violations: list[dict], path: str) -> str | None:
    return next((v["kind"] for v in violations if v["path"] == path), None)


class TestImplementTurnCannotEditTests:
    def test_its_own_milestone_test_is_reverted(self) -> None:
        """The hole this closes."""
        v = scope_violations(
            "implement", {OWN: "modified"}, "rspec", milestone_test_files=[OWN],
        )
        assert OWN in _reverted(v)
        assert _kind_for(v, OWN) == "reverted_own_test"

    def test_another_milestones_test_is_still_reverted(self) -> None:
        """The original rule, preserved."""
        v = scope_violations(
            "implement", {OTHER: "modified"}, "rspec", milestone_test_files=[OWN],
        )
        assert OTHER in _reverted(v)
        assert _kind_for(v, OTHER) == "reverted_foreign_test"

    def test_a_test_is_reverted_even_with_no_milestone_list(self) -> None:
        """`milestone_test_files=None` previously allowed editing any test file."""
        v = scope_violations(
            "implement", {OWN: "modified"}, "rspec", milestone_test_files=None,
        )
        assert OWN in _reverted(v)

    def test_a_new_test_file_is_reverted_too(self) -> None:
        """Adding a test during green is the same laundering route as editing one."""
        v = scope_violations(
            "implement", {"spec/models/new_thing_spec.rb": "added"}, "rspec",
            milestone_test_files=[OWN],
        )
        assert "spec/models/new_thing_spec.rb" in _reverted(v)

    def test_the_reason_names_the_rule(self) -> None:
        v = scope_violations(
            "implement", {OWN: "modified"}, "rspec", milestone_test_files=[OWN],
        )
        reason = next(x["reason"] for x in v if x["path"] == OWN)
        assert "364" in reason, "an operator seeing this revert needs to find the rule"


class TestEverythingElseIsUnchanged:
    def test_implement_turn_may_still_write_source(self) -> None:
        v = scope_violations(
            "implement", {SOURCE: "modified"}, "rspec", milestone_test_files=[OWN],
        )
        assert SOURCE not in _reverted(v)

    def test_tests_turn_may_still_write_tests(self) -> None:
        """The red step is where tests are written; that must keep working."""
        v = scope_violations("tests", {OWN: "modified"}, "rspec", milestone_test_files=[OWN])
        assert OWN not in _reverted(v)

    def test_tests_turn_still_may_not_write_source(self) -> None:
        v = scope_violations("tests", {SOURCE: "modified"}, "rspec", milestone_test_files=[OWN])
        assert SOURCE in _reverted(v)

    def test_implement_turn_writing_source_and_test_reverts_only_the_test(self) -> None:
        """The implementation survives; only the evidence-editing is undone."""
        v = scope_violations(
            "implement", {SOURCE: "modified", OWN: "modified"}, "rspec",
            milestone_test_files=[OWN],
        )
        assert _reverted(v) == {OWN}

    def test_python_stack_behaves_the_same(self) -> None:
        """The rule is about turn kind, not language."""
        v = scope_violations(
            "implement", {"tests/test_week.py": "modified"}, "pytest",
            milestone_test_files=["tests/test_week.py"],
        )
        assert "tests/test_week.py" in _reverted(v)
