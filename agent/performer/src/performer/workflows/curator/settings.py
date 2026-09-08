"""Curator run settings, delivered through ``workflow_env`` (spec 173)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

_DEFAULT_CRITERIA = [
    "Clear, testable acceptance criteria",
    "Well-defined scope (single concern, not a meta-epic)",
    "No unresolved blockers, open questions, or external dependencies",
]


@dataclass
class CuratorSettings:
    label: str = "curator-proposed"
    backlog_column: str = "Backlog"
    criteria: list[str] = field(default_factory=lambda: list(_DEFAULT_CRITERIA))
    max_per_run: int = 5

    @classmethod
    def from_env(cls, env: dict[str, str] | None) -> CuratorSettings:
        env = env or {}
        defaults = cls()
        raw = (env.get("CURATOR_CRITERIA") or "").strip()
        try:
            criteria = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            criteria = None
        if not isinstance(criteria, list) or not criteria:
            criteria = list(defaults.criteria)
        try:
            max_per_run = int(env.get("CURATOR_MAX_PER_RUN") or defaults.max_per_run)
        except (TypeError, ValueError):
            max_per_run = defaults.max_per_run
        return cls(
            label=env.get("CURATOR_LABEL") or defaults.label,
            backlog_column=env.get("CURATOR_BACKLOG_COLUMN") or defaults.backlog_column,
            criteria=[str(c) for c in criteria],
            max_per_run=max_per_run,
        )
