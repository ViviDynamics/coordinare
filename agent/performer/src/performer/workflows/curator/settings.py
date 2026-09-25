"""Curator run settings, delivered through ``workflow_env`` (spec 173).

416 adds the settings the scan loop needs to leave a durable trail: a skip
label so a declined issue stays declined, the escalation label so sensitive
topics are marked rather than ignored, and a per-call cap so one gateway
conversation holds a bounded slice of the scan.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from performer.workflows.advocate.settings import _DEFAULT_KEYWORDS

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
    #: 416: the scan's skip mark. An issue the run declined is labelled, so
    #: the next cycle's scan advances instead of re-reading the same pile.
    skipped_label: str = "curator-skipped"
    #: 416: where the intake triage sends a sensitive report instead of the
    #: model. Defaults to the advocate's escalation label.
    escalation_label: str = "needs-human"
    #: The same sensitive-topic rule the advocate runs, same defaults.
    sensitive_keywords: list[str] = field(default_factory=lambda: list(_DEFAULT_KEYWORDS))
    #: How many candidates one gateway conversation may judge. The remainder
    #: of the run's budget waits for the next conversation, so a run that
    #: produces malformed JSON twice costs two slices, not the whole scan.
    max_per_call: int = 10

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
        keywords = defaults._json_list(env, "CURATOR_SENSITIVE_KEYWORDS",
                                       list(defaults.sensitive_keywords))
        try:
            max_per_run = int(env.get("CURATOR_MAX_PER_RUN") or defaults.max_per_run)
        except (TypeError, ValueError):
            max_per_run = defaults.max_per_run
        try:
            max_per_call = int(env.get("CURATOR_MAX_PER_CALL") or defaults.max_per_call)
        except (TypeError, ValueError):
            max_per_call = defaults.max_per_call
        return cls(
            label=env.get("CURATOR_LABEL") or defaults.label,
            backlog_column=env.get("CURATOR_BACKLOG_COLUMN") or defaults.backlog_column,
            criteria=[str(c) for c in criteria],
            max_per_run=max_per_run,
            skipped_label=env.get("CURATOR_SKIPPED_LABEL") or defaults.skipped_label,
            escalation_label=env.get("CURATOR_ESCALATION_LABEL") or defaults.escalation_label,
            sensitive_keywords=keywords,
            max_per_call=max_per_call,
        )

    def _json_list(self, env: dict[str, str], key: str, default: list[str]) -> list[str]:
        raw = (env.get(key) or "").strip()
        if not raw:
            return default
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return default
        if not isinstance(parsed, list) or not parsed:
            return default
        return [str(item) for item in parsed]
