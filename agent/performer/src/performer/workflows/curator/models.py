"""Data models for the curator workflow (spec 173).

The model judges; code decides and code acts. The guarded schema carries no
column, no label and no action: a model that could name the column it lands in
could put work straight into the pipeline.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IssueCandidate(BaseModel):
    """One open issue that is not already on the board."""

    model_config = ConfigDict(extra="ignore")

    issue_id: str = Field(..., min_length=1)
    number: int = 0
    title: str = ""
    body: str = ""
    url: str = ""
    labels: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"


class SelectionJudgement(BaseModel):
    """The model's verdict on one candidate."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(..., min_length=1)
    qualifies: bool
    reason: str = Field(..., min_length=1, max_length=1000)
    quote: str = Field(default="", max_length=1000)


class SelectionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judgements: list[SelectionJudgement] = Field(default_factory=list, max_length=50)


class CurationOutcome(BaseModel):
    issue_id: str
    number: int = 0
    action: Literal["added", "skipped", "rejected"]
    reason: str = ""
    #: The passage from the issue that justified a promotion. On the record as
    #: well as in the comment, so the justification can be checked without
    #: reading GitHub.
    quote: str = ""
    board_item_id: str | None = None
    column: str | None = None


class CurationRecord(BaseModel):
    """The run's report, carried under the report key ``curation``."""

    verdict: Literal["curation_complete", "env_blocked"] = "curation_complete"
    candidates_seen: int = 0
    outcomes: list[CurationOutcome] = Field(default_factory=list)
    rejected_judgements: list[dict] = Field(default_factory=list)
    model_calls: int = 0
    error: str | None = None
    write_free_check: str = ""
    workflow_metrics: dict = Field(default_factory=dict)
