"""Data models for the closer workflow (spec 172).

Thread, Classification, and ClosingRecord define the structures passed through
the workflow. model_judgements_schema builds the pydantic model for schema-guarding
the model call.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ThreadComment(BaseModel):
    """A comment in a review thread."""

    author: str = Field(..., description="GitHub login or empty string if deleted")
    body: str = Field(..., max_length=4000, description="Comment text")
    created_at: str = Field(..., description="ISO 8601 timestamp")
    author_association: str = Field(default="NONE", description="GitHub author association (MEMBER, COLLABORATOR, OWNER, NONE, ...)")

    @property
    def is_bot(self) -> bool:
        """True when the login follows GitHub's bot convention (login[bot])."""
        return self.author.endswith("[bot]")


class Thread(BaseModel):
    """A GitHub review thread with its comments."""

    id: str
    path: str
    line: int = Field(..., ge=0)
    resolved: bool
    outdated: bool
    comments: list[ThreadComment]

    @property
    def first_author(self) -> str:
        """The author of the first comment, or empty string."""
        return self.comments[0].author if self.comments else ""

    @property
    def last_author(self) -> str:
        """The author of the last comment, or empty string."""
        return self.comments[-1].author if self.comments else ""

    @property
    def raiser_only(self) -> bool:
        """True if every author equals the first author."""
        if not self.comments:
            return True
        first = self.first_author
        return all(c.author == first for c in self.comments)


class Classification(BaseModel):
    """The result of classifying a thread by code rules."""

    thread_id: str
    state: Literal["resolved", "stale", "answered", "open"]
    rule: str = Field(..., max_length=200, description="The predicate that decided it")


class Judgement(BaseModel):
    """The model's or gate's judgement on whether a thread is addressed."""

    thread_id: str
    addressed: bool
    quote: str = Field(default="", max_length=300, description="Verbatim quote from thread (if addressed)")
    reason: str = Field(default="", max_length=300, description="Why addressed or not")
    accepted: bool = Field(..., description="Whether the gate accepted this judgement")
    discard_reason: str | None = Field(default=None, description="Why discarded, if not accepted")


class ClosingRecord(BaseModel):
    """The complete report from a closer workflow run (spec 172)."""

    head_sha: str | None = Field(default=None)
    threads_read: int = Field(..., ge=0)
    pages_read: int = Field(default=0, ge=0)
    classifications: list[Classification] = Field(default=[])
    judgements: list[Judgement] = Field(default=[])
    resolved: list[dict[str, str]] = Field(default=[], description="[{thread_id, reason}]")
    open_threads: list[dict[str, Any]] = Field(default=[], description="[{thread_id, path, line, excerpt}]")
    verdict: Literal["approved", "changes_requested", "env_blocked"]
    hold_reason: str | None = Field(default=None)
    posted_review_url: str | None = Field(default=None)
    workflow_metrics: dict[str, Any] = Field(default={})

    def validate_schema(self) -> None:
        """Validate the record against the JSON schema (contracts/closing-record.schema.json)."""
        import jsonschema

        schema_path = Path(__file__).resolve().parents[4] / "specs/172-closer-workflow/contracts/closing-record.schema.json"
        if not schema_path.exists():
            raise FileNotFoundError(f"Schema file not found: {schema_path}")

        with open(schema_path) as f:
            schema = json.load(f)

        # Convert to dict for jsonschema validation
        data = self.model_dump(mode="json")
        jsonschema.validate(data, schema)


def model_judgements_schema(max_threads: int = 20) -> type:
    """Build a pydantic model for schema-guarding the model call.

    The model returns judgements per thread with addressed/not_addressed and a quote.
    The schema MUST reject a verdict key (caught as validation error).
    The schema MUST reject more than max_threads entries.
    """

    class ModelJudgement(BaseModel):
        """One judgement from the model."""

        model_config = ConfigDict(extra="forbid")

        thread_id: str
        addressed: bool
        quote: str = Field(default="", max_length=300)
        reason: str = Field(default="", max_length=300)

    class JudgementsResponse(BaseModel):
        """The response envelope from the model."""

        model_config = ConfigDict(extra="forbid")

        judgements: list[ModelJudgement] = Field(..., max_length=max_threads)

        @field_validator("judgements")
        @classmethod
        def validate_judgements_count(cls, v: list) -> list:
            if len(v) > max_threads:
                raise ValueError(f"too many judgements: {len(v)} > {max_threads}")
            return v

    return JudgementsResponse


__all__ = ["ThreadComment", "Thread", "Classification", "Judgement", "ClosingRecord", "model_judgements_schema"]
