"""Models for the reviewer workflow (spec 169).

ChangedFile, Hunk, Finding, Disposition, ReviewRecord per data-model.md.
Every field is bounded and required. Extra fields are forbidden.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated


class _Bounded(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hunk(_Bounded):
    """A unified diff hunk with new-side line numbers for anchor validation."""

    header: str  # e.g., "@@ -10,5 +10,7 @@"
    start_line: Annotated[int, Field(ge=1)]  # First new-side line (c in +c,d), minimum 1
    end_line: Annotated[int, Field(ge=1)]  # Last new-side line (d in +c,d), minimum 1
    lines: list[str] = Field(default_factory=list)  # Hunk content for evidence matching


class ChangedFile(_Bounded):
    """Represents a file in the PR diff with hunks and surveyed state."""

    path: str  # File path from diff header
    hunks: list[Hunk]  # Parsed hunks from the diff
    fully_in_diff: bool  # True if entire file is in injected text
    opened_by_survey: bool = False  # True if survey ran a command on this file
    # 412 round 8: "+++ /dev/null" -- the file is gone from the worktree, so
    # it is not a scannable target (though it still counts for coverage).
    deleted: bool = False
    # 412 round 20: a truncation cut clears ``deleted`` so a cut-through
    # deletion holds as unread rather than passing as covered -- but the
    # file still does not exist on disk, and the scan argv must keep
    # excluding it. Records what the diff header said before the cut.
    deleted_before_cut: bool = False


# Categories per spec 169 data-model.md
DEFAULT_CATEGORIES = (
    "logic_error",
    "test_missing",
    "style",
    "performance",
    "security",
    "documentation_by_implementer",
    "unaddressed_feedback",
)

#: 412: advisory categories report but never block. A refactor, docs or
#: dep-bump PR gets a near-certain ``test_missing`` hit; that must not hold
#: the card. Everything else in DEFAULT_CATEGORIES is blocking.
ADVISORY_CATEGORIES = frozenset({"style", "test_missing"})


class Finding(_Bounded):
    """A code issue identified by the reviewer."""

    introduced_by: str = ""  # Changed cause; empty preserves legacy changed-path findings.
    path: str  # Changed or surveyed file path; "" if unanchored prior comment
    line: Annotated[int, Field(ge=0)]  # New-side line number; 0 if unanchored
    category: Annotated[str, StringConstraints(min_length=1, max_length=64)]  # From the configured set (FR-018); the model schema pins it to a Literal
    problem: Annotated[str, StringConstraints(min_length=1, max_length=500)]  # What is wrong
    why_blocking: Annotated[str, StringConstraints(min_length=1, max_length=500)]  # Why this must be fixed
    evidence: Annotated[str, StringConstraints(max_length=200)]  # Offending line or code snippet; empty only when origin == "rule"
    origin: Literal["model", "rule"]  # "model" from findings call, "rule" from gate

    @model_validator(mode="after")
    def _evidence_empty_only_for_rules(self) -> Finding:
        """Evidence can be empty only for rule-added findings."""
        if not self.evidence and self.origin != "rule":
            raise ValueError("evidence must be non-empty for model-added findings")
        return self


class Disposition(_Bounded):
    """How a prior comment was handled."""

    prior_comment_id: str  # ID of the comment from relay_feedback
    status: Literal["fixed", "not_fixed"]  # Whether the issue was addressed
    finding_index: int | None = None  # Index into findings if status is not_fixed and a finding addresses it


class ReviewRecord(_Bounded):
    """The complete workflow execution record, travels in PerformerResponse.report."""

    changed_files: list[ChangedFile]  # Parsed diff structure
    diff_truncated: bool  # True if injected diff was capped
    unread_files: list[str] = Field(default_factory=list)  # After coverage pass, still unread

    survey_commands: Annotated[list[dict[str, Any]], Field(default_factory=list)]  # Commands run
    survey_refusals: Annotated[list[dict[str, Any]], Field(default_factory=list)]  # Refused commands

    findings: Annotated[list[Finding], Field(default_factory=list, max_length=30)]  # Surviving blocking findings after the gate (the canonical list coordinare lifts)
    advisory_findings: Annotated[list[Finding], Field(default_factory=list, max_length=30)]  # Surviving but advisory: posted, not blocking
    findings_before_gate: Annotated[list[Finding], Field(default_factory=list, max_length=30)]  # Model's raw findings
    findings_dropped: Annotated[list[Finding], Field(default_factory=list, max_length=30)]  # Unanchored findings, dropped
    findings_after_anchor_recheck: Annotated[list[Finding], Field(default_factory=list, max_length=30)]  # Reprompted findings

    dispositions: Annotated[list[Disposition], Field(default_factory=list)]  # Prior comments handled

    coverage_pass_ran: bool = False  # True if coverage pass was needed
    coverage_pass_output: str = ""  # Model's coverage report

    #: 412: nothing_to_review is the explicit verdict for an empty parsed
    #: diff -- advance-with-note for the coordinare, never "approved".
    verdict: Literal["approved", "changes_requested", "env_blocked", "nothing_to_review"]
    covered_files: list[str]  # Changed files that were in diff or surveyed

    post_error: str | None = None  # If post failed, the error message
    posted_review_url: str | None = None  # GitHub review URL if successful

    workflow_metrics: dict[str, Any] = Field(default_factory=dict)  # Timing and token metrics



class PriorComment(_Bounded):
    """One relayed open comment from an earlier round, normalised by intake (FR-002)."""

    id: str
    path: str = ""
    line: Annotated[int, Field(ge=0)] = 0
    body: Annotated[str, StringConstraints(max_length=4000)] = ""


class ModelDisposition(_Bounded):
    """The model's answer for one prior comment."""

    prior_comment_id: str
    status: Literal["fixed", "not_fixed"]
    finding_index: int | None = None


def model_findings_schema(categories: tuple[str, ...] | list[str], max_findings: int = 30) -> type[BaseModel]:
    """Build the schema the findings call is validated against (FR-005, FR-018).

    ``category`` is pinned to a Literal of the configured set, so an unknown
    category is a schema violation (one reprompt) rather than a silent drop.
    ``extra="forbid"`` on both levels rejects a ``verdict`` key: the verdict is
    derived by code.
    """
    cats = tuple(dict.fromkeys(str(c) for c in categories if str(c)))
    if not cats:
        raise ValueError("the reviewer needs at least one finding category")
    category_type = Literal[cats]  # type: ignore[valid-type]

    class ModelFinding(_Bounded):
        introduced_by: str = ""
        path: str
        line: Annotated[int, Field(ge=0)]
        category: category_type  # type: ignore[valid-type]
        problem: Annotated[str, StringConstraints(min_length=1, max_length=500)]
        why_blocking: Annotated[str, StringConstraints(min_length=1, max_length=500)]
        evidence: Annotated[str, StringConstraints(min_length=1, max_length=200)]

    class ModelFindings(_Bounded):
        findings: Annotated[list[ModelFinding], Field(default_factory=list, max_length=max_findings)]
        dispositions: Annotated[list[ModelDisposition], Field(default_factory=list, max_length=100)]

    ModelFinding.__name__ = "ModelFinding"
    ModelFindings.__name__ = "ModelFindings"
    return ModelFindings


__all__ = [
    "Hunk",
    "ChangedFile",
    "Finding",
    "Disposition",
    "ReviewRecord",
    "PriorComment",
    "ModelDisposition",
    "model_findings_schema",
    "DEFAULT_CATEGORIES",
    "ADVISORY_CATEGORIES",
]
