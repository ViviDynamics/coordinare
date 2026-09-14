"""Size rule (spec 165 FR-006, research R5): a pure function of the blueprint.

``small`` drives a single implementer turn (no milestone loop). Whether a
documenter runs is a separate rule on the docs list (FR-014), so docs do not
enter here. The model never sets size; ``Blueprint`` forbids the field.
"""
from __future__ import annotations

from typing import Literal

from performer.workflows.architect.models import Blueprint

Size = Literal["small", "large"]


def size_of(blueprint: Blueprint) -> Size:
    # 410: module count and criteria count weigh in. Before this, a
    # twelve-module refactor with no data-model or interface entries was
    # "small" (one turn) on milestone count alone, and a one-column add was
    # always "large" because the data-model change forced it.
    if (
        len(blueprint.milestones) <= 1
        and len(blueprint.modules) <= 3
        and len(blueprint.criteria) <= 3
        and not blueprint.data_model.changes
        and not blueprint.interfaces
    ):
        return "small"
    return "large"
