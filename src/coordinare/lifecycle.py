"""Canonical performer lifecycle constants — single source of truth.

Shared by ``__main__``, ``dry_run``, and any other module that needs the
role→stage mapping or canonical role ordering without importing the CLI layer.
"""
from __future__ import annotations

ROLE_TO_STAGE: dict[str, str] = {
    "advocate": "advocate",
    "assessor": "assessing",
    "architect": "architecting",
    "implementer": "implementing",
    "reviewer": "reviewing",
    "security": "security",
    "qa": "qa",
    "tech_writer": "documenting",
    # 042: Final closing pass after tech_writer — verifies prior PR feedback
    # was addressed by subsequent commits and resolves all review threads.
    # Distinct stage from "reviewing" so _advance_stage's index lookup advances
    # past the substantive reviewer correctly when the closer runs.
    "closer": "closing_review",
}

CANONICAL_ORDER: list[str] = [
    "advocate", "assessor", "architect", "implementer",
    "reviewer", "security", "qa", "tech_writer", "closer",
]

# 048: Stages (not role names) that must run at most one instance
# regardless of max_concurrency configuration.  Uses stage names
# (matching ROLE_TO_STAGE values) because SlotManager and
# dispatch_performer operate on stages, not role names.
# The assessor needs a consistent board view for dependency detection
# (046), and the closer needs exclusive merge access to avoid race
# conditions on the default branch.
SINGLETON_STAGES: frozenset[str] = frozenset({"assessing", "closing_review"})
