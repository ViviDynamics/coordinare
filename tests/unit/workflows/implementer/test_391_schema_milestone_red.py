"""Spec 391: the tests turn must know the shape of a schema milestone's red.

A schema milestone (migration, new table, column, index or model) has no
function to import, so the behaviour-shaped guidance alone produced migration
specs that asserted what the current schema already satisfies -- green before
the migration exists -- and the judge correctly rejected them. The personas
must carry the schema shape, stack-neutrally: ask the schema whether the
object exists, expecting the post-migration answer, so the test fails before
the migration and passes after. And they must not carry any framework recipe
(no table_exists?, no ActiveRecord, nothing of any vendor).
"""
from __future__ import annotations

import pytest
from performer.workflows.implementer import personas

# Words that would bake one stack's recipe into a stack-neutral prompt. The
# issue is explicit that the shape travels, the expression does not.
RECIPES = (
    "table_exists",
    "ActiveRecord",
    "Rails",
    "rails",
    "sqlite_master",
    "information_schema",
    "PRAGMA",
    "describe_table",
    "column_exists",
)


@pytest.mark.parametrize("template", [personas.TESTS, personas.REPAIR_TESTS])
class TestSchemaMilestoneShape:
    """The schema red shape reaches the tests turn, for both personas."""

    def test_names_the_schema_shape(self, template: str) -> None:
        assert "schema" in template

    def test_carries_the_mechanism(self, template: str) -> None:
        # The load-bearing direction, per the issue's mechanism paragraph: the
        # test asks whether the schema object exists expecting the
        # post-migration answer, so it fails BEFORE and passes AFTER. An
        # inverted instruction (expect the pre-migration answer) reproduces
        # the very defect this fixes, green before the migration.
        # Prose line wraps must not gate the rule: compare on flattened text.
        flat = " ".join(template.split())
        assert "fails before the migration" in flat
        assert "passes after" in flat

    def test_no_framework_recipe(self, template: str) -> None:
        for recipe in RECIPES:
            assert recipe not in template, recipe

    def test_renders_with_schema_milestone(self, template: str) -> None:
        rendered = template.format(
            milestone_goal="Schema S1: project_assignments model",
            scope_paths="spec/migrations/",
            done_when="project_assignments table exists",
            test_conventions="rspec",
            passing_test_files=["spec/migrations/x_spec.rb"],
        )
        flat = " ".join(rendered.split())
        assert "project_assignments" in flat
        assert "fails before the migration" in flat
