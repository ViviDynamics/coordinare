"""Core in-memory domain models for a single performer invocation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from performer.backends.base import BackendAdapter

# ---------------------------------------------------------------------------
# Score — the dispatch payload
# ---------------------------------------------------------------------------

_GITHUB_REPO_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(\.git)?$"
)


class Score(BaseModel):
    """Dispatch payload received from the coordinare."""

    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    repo_url: str
    branch: str
    github_token: str
    base_branch: str = ""

    @field_validator("repo_url")
    @classmethod
    def _validate_repo_url(cls, v: str) -> str:
        if not _GITHUB_REPO_RE.match(v):
            msg = (
                f"repo_url must be a GitHub HTTPS URL "
                f"(https://github.com/{{owner}}/{{repo}}[.git]), got: {v!r}"
            )
            raise ValueError(msg)
        return v

    @field_validator("branch")
    @classmethod
    def _validate_branch(cls, v: str) -> str:
        if not v.strip():
            msg = "branch must be non-empty"
            raise ValueError(msg)
        if " " in v or ".." in v:
            msg = f"branch contains invalid git ref characters: {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("github_token")
    @classmethod
    def _validate_github_token(cls, v: str) -> str:
        if not v.strip():
            msg = "github_token must be non-empty"
            raise ValueError(msg)
        return v

    @property
    def owner_repo(self) -> tuple[str, str]:
        """Extract (owner, repo) from the validated GitHub URL."""
        path = self.repo_url.rstrip("/").removesuffix(".git")
        parts = path.split("/")
        return parts[-2], parts[-1]


# ---------------------------------------------------------------------------
# Stand — the ephemeral workspace
# ---------------------------------------------------------------------------

PerformanceState = Literal[
    "accepted", "working", "blocked", "pr_opened", "error", "session_expired"
]


@dataclass
class Stand:
    """Ephemeral local workspace created for a single performance."""

    path: Path
    branch: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Performance — the in-memory session
# ---------------------------------------------------------------------------

@dataclass
class Performance:
    """Represents one complete end-to-end performer session."""

    session_id: str
    stand: Stand
    score: Score
    backend: "BackendAdapter"
    state: PerformanceState = "accepted"
    pr_url: str | None = None
    pr_node_id: str | None = None
    open_questions: list[str] = field(default_factory=list)
    error_reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
