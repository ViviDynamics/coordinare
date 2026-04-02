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
}

CANONICAL_ORDER: list[str] = [
    "advocate", "assessor", "architect", "implementer",
    "reviewer", "security", "qa", "tech_writer",
]
