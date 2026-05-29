"""Performer success-result schema (spec 076 T070, FR-017).

Defines the strict contract for what a performer reports when its turn
ends.  Used by ``monitor_performer`` to validate terminal outcomes —
specifically the ``done`` outcome MUST include the new PR artefacts
(if any) so the orchestrator never silently loses track of work that
was pushed to GitHub.

The schema is permissive in a backward-compatible way: every artefact
field is Optional.  This lets older performer images (pre-076) keep
working — they just don't trigger the FR-015 write-through path.  The
strict layer is in ``monitor_performer._record_pr_artefacts`` which
warns when ``done`` is received without any artefact fields and the
card lacks a PR.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

PerformerOutcome = Literal[
    "done",
    "partial_progress",
    "blocked",
    "idle_timeout",
]


class PerformerSuccessResult(BaseModel):
    """076 FR-017: strict schema for a performer's terminal status report.

    All artefact fields are Optional so old performer images stay
    compatible; the strict-on-missing-PR-artefact check lives at the
    consumption site (``monitor_performer._record_pr_artefacts``).
    """

    model_config = ConfigDict(extra="ignore")  # tolerate extra status fields

    outcome: PerformerOutcome
    pushed_branch: str | None = None
    pr_url: str | None = None
    pr_node_id: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    comment: str | None = None
    next_focus: str | None = None


__all__ = ["PerformerOutcome", "PerformerSuccessResult"]
