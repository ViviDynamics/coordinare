"""Data models for the advocate workflow (spec 173).

The model classifies and drafts; code decides. The guarded response schema
therefore forbids anything that looks like a verdict or an action: the model
never says "escalate" or "handled", it says what kind of issue this is and, at
most, an answer it can cite.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

IssueKind = Literal[
    "question", "confusion", "complaint", "feature_request", "bug_report", "off_topic",
]

#: The kinds an answer may be drafted for. Everything else is acknowledged,
#: triaged, redirected or escalated by rule, with no prose from the model.
ANSWERABLE_KINDS: frozenset[str] = frozenset({"question", "confusion"})

Action = Literal["replied", "acknowledged", "triaged", "redirected", "escalated"]


class IssueCandidate(BaseModel):
    """One open issue the run is considering. Built by intake, never by a model."""

    model_config = ConfigDict(extra="ignore")

    issue_id: str = Field(..., min_length=1)
    number: int = 0
    title: str = ""
    body: str = ""
    url: str = ""
    created_at: str = ""
    labels: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        """Title and body together, as the keyword rule and the model see it."""
        return f"{self.title} {self.body}"


class DocumentRead(BaseModel):
    """A documentation file the run tried to read.

    ``read=False`` records an absent file rather than dropping it, so a
    misconfigured ``doc_sources`` is visible in the record instead of looking
    like documentation that simply had nothing to say.
    """

    path: str = Field(..., min_length=1)
    content: str = ""
    read: bool = False


class Classification(BaseModel):
    """The model's judgement about one issue."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(..., min_length=1)
    classification: IssueKind
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field(..., min_length=1, max_length=2000)
    answer: str | None = Field(default=None, max_length=6000)
    cited_documents: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: object) -> object:
        """Clamp rather than reject: a model returning 1.4 has still expressed
        high confidence, and failing the whole batch over it helps nobody."""
        try:
            return min(1.0, max(0.0, float(value)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return value


class ClassificationBatch(BaseModel):
    """What one guarded call returns."""

    model_config = ConfigDict(extra="forbid")

    classifications: list[Classification] = Field(default_factory=list, max_length=50)


class IssueOutcome(BaseModel):
    """What the run decided and did for one issue. Produced by code."""

    issue_id: str
    number: int = 0
    action: Action
    escalation_reason: str | None = None
    classification: IssueKind | None = None
    cited_documents: list[str] = Field(default_factory=list)
    label_applied: str = ""
    comment_posted: bool = False
    model_calls: int = 0


class AdvocateRecord(BaseModel):
    """The run's report, carried under the report key ``advocate``."""

    verdict: Literal["advocate_complete", "env_blocked"] = "advocate_complete"
    issues_seen: int = 0
    documents_read: list[str] = Field(default_factory=list)
    outcomes: list[IssueOutcome] = Field(default_factory=list)
    withheld: list[dict] = Field(default_factory=list)
    model_calls: int = 0
    error: str | None = None
    write_free_check: str = ""
    workflow_metrics: dict = Field(default_factory=dict)


def model_classification_schema() -> type[ClassificationBatch]:
    """The schema the guarded call validates against.

    Deliberately the same object the workflow consumes: specs 169 and 170 both
    shipped a hand-written JSON schema that drifted from the pydantic model, so
    here there is only one definition and the JSON schema is derived from it.
    """
    return ClassificationBatch
