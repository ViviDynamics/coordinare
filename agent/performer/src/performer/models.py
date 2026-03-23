"""Core in-memory domain models for a single performer invocation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# BackendEvent — normalised activity event emitted by any backend
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"github_pat_[A-Za-z0-9_]{82,}"),          # fine-grained PAT
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                    # classic PAT
    re.compile(r"gho_[A-Za-z0-9]{36}"),                    # OAuth app token
    re.compile(r"ghs_[A-Za-z0-9]{36}"),                    # App installation token
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{90,}"),             # Anthropic API key
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{20,}"),       # Bearer header value
    re.compile(r"AKIA[0-9A-Z]{16}"),                        # AWS access key
]


def _redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class BackendEventType(str, Enum):
    progress = "progress"  # general working update
    tool_use = "tool_use"  # agent called a tool (file read/write, shell, etc.)
    thinking = "thinking"  # internal reasoning (e.g. Claude extended thinking)
    cost     = "cost"      # token / cost accounting
    error    = "error"     # non-fatal error within the session
    output   = "output"    # raw output line (fallback for unrecognised events)


class BackendEvent(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: BackendEventType
    text: str               # human-readable one-line summary (≤ 200 chars)
    detail: str = ""        # optional longer detail (file path, tool args, etc.)

    @model_validator(mode="before")
    @classmethod
    def _redact_sensitive_detail(cls, data: object) -> object:
        if isinstance(data, dict) and "detail" in data:
            data["detail"] = _redact_secrets(str(data["detail"]))
        return data

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
    clarifications: list[dict] = Field(default_factory=list)
    repo_url: str
    branch: str
    github_token: str = ""
    # Empty string is permitted when the runtime environment supplies a
    # GITHUB_TOKEN env var (e.g. Kubernetes transport where auth is injected
    # via K8s Secrets, or local dev with GITHUB_TOKEN exported).  Always use
    # ``effective_github_token`` for API calls and git auth — it resolves the
    # payload token first and falls back to GITHUB_TOKEN.  If neither is set,
    # GitHub API helpers raise GitHubAPIError(401) rather than sending a
    # malformed ``Authorization: Bearer `` header.
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

    @property
    def effective_github_token(self) -> str:
        """Resolved GitHub token for API calls and git HTTP auth.

        When ``github_token`` is empty (Kubernetes transport — auth is injected
        into the container via K8s Secrets), falls back to the ``GITHUB_TOKEN``
        environment variable so that git operations and GitHub API calls succeed
        without the coordinare needing to know the Kubernetes secret value.
        """
        return self.github_token.strip() or os.environ.get("GITHUB_TOKEN", "")

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
    "accepted", "working", "blocked", "pr_opened", "plan_committed",
    "approved", "changes_requested",
    "security_passed", "security_failed",
    "qa_passed", "qa_failed",
    "docs_committed",
    "error", "session_expired", "waiting_for_checks",
]


@dataclass
class Stand:
    """Ephemeral local workspace created for a single performance."""

    path: Path
    branch: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    git_env: dict[str, str] = field(default_factory=dict)


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
    role: str = "implementing"  # 020: performer role (e.g. "implementing", "architecting")
    pr_url: str | None = None
    pr_node_id: str | None = None
    pr_head_sha: str | None = None
    plan_path: str | None = None  # 020: path to committed architecture plan
    check_attempt: int = 0
    review_comments: list[dict] = field(default_factory=list)  # 021: [{file, line, body}]
    review_suggestions: list[str] = field(default_factory=list)  # 021: non-blocking suggestions
    review_cycle: int = 0  # 021: number of review cycles exhausted
    security_findings: list[dict] = field(default_factory=list)  # 022: [{severity, category, ...}]
    security_cycle: int = 0  # 022: number of security fix cycles
    qa_failures: list[dict] = field(default_factory=list)  # 023: [{criterion, expected, actual, test}]
    qa_new_tests: list[str] = field(default_factory=list)  # 023: paths of committed test files
    qa_report: dict | None = None  # 023: pass report {criteria_checked, criteria_passed, new_tests_added}
    qa_cycle: int = 0  # 023: number of QA fix cycles
    docs_files_modified: list[str] = field(default_factory=list)  # 024: doc files committed
    open_questions: list[str] = field(default_factory=list)
    error_reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
