"""Customer advocate agent data models (007)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class IssueType(StrEnum):
    question = "question"
    confusion = "confusion"
    complaint = "complaint"
    feature_request = "feature_request"
    bug_report = "bug_report"
    off_topic = "off_topic"


class AdvocateAction(StrEnum):
    replied = "replied"
    escalated = "escalated"
    acknowledged = "acknowledged"
    triaged = "triaged"
    redirected = "redirected"


class EscalationReason(StrEnum):
    low_confidence = "low_confidence"
    sensitive_keyword = "sensitive_keyword"
    complaint = "complaint"
    no_documentation_match = "no_documentation_match"
    no_documentation_configured = "no_documentation_configured"
    claude_api_failure = "claude_api_failure"


@dataclass
class IssueClassification:
    issue_id: str
    issue_number: int
    classification: IssueType
    confidence_score: float
    sensitive_flagged: bool
    classified_at: datetime

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence_score <= 1.0):
            msg = f"confidence_score must be in [0.0, 1.0], got {self.confidence_score}"
            raise ValueError(msg)
        if not self.issue_id:
            msg = "issue_id must be non-empty"
            raise ValueError(msg)
        if self.classified_at.tzinfo is None:
            msg = "classified_at must be timezone-aware (UTC)"
            raise ValueError(msg)


@dataclass
class AdvocateResponse:
    issue_id: str
    action: AdvocateAction
    response_text: str | None
    source_documents: list[str]
    confidence_score: float
    created_at: datetime


@dataclass
class EscalationRecord:
    issue_id: str
    issue_number: int
    reason: EscalationReason
    notified_channel: str
    escalated_at: datetime


@dataclass
class DocumentationSource:
    file_path: str
    branch_ref: str
    content: str | None
    last_read_at: datetime
    reachable: bool


@dataclass
class ScoringProvider:
    provider_name: str
    score: float
    reasoning: str

    def __post_init__(self) -> None:
        if not (0.0 <= self.score <= 1.0):
            msg = f"score must be in [0.0, 1.0], got {self.score}"
            raise ValueError(msg)
        if not self.provider_name:
            msg = "provider_name must be non-empty"
            raise ValueError(msg)


@dataclass
class ConsensusScore:
    final_score: float
    provider_scores: list[ScoringProvider] = field(default_factory=list)
