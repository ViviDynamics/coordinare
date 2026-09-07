"""Data models for documenter workflow (spec 171)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "KINDS",
    "REQUIRED_HEADINGS",
    "README_SECTIONS",
    "KIND_TO_SECTION",
    "DOC_PATH_PREFIXES",
    "DOC_ROOT_FILES",
    "POINTER_FILES",
    "POINTER_MARKERS",
    "PLAN_CAP",
    "PAGE_MIN_CHARS",
    "PAGE_MAX_CHARS",
    "SUMMARY_MAX_CHARS",
    "POINTER_MAX_LINES",
    "BAD_LINK_TEXT",
    "WikiPage",
    "RepositoryLayout",
    "PagePlan",
    "PageEvidence",
    "PageResult",
    "DocsRecord",
    "model_page_write_schema",
]

# Constants per spec 171 data-model.md
KINDS = ("explanation", "how-to", "reference", "decision")
REQUIRED_HEADINGS = {
    "explanation": ("What it is", "How it fits", "Why it is this way", "Where to change it"),
    "how-to": ("Goal", "Prerequisites", "Steps", "Verify"),
    "reference": (),
    "decision": ("Context", "Decision", "Consequences", "Status"),
}
README_SECTIONS = ("Start here", "Architecture", "How to", "Reference", "Decisions", "Optional")
KIND_TO_SECTION = {
    "explanation": "Architecture",
    "how-to": "How to",
    "reference": "Reference",
    "decision": "Decisions",
}
DOC_PATH_PREFIXES = ("docs/", "doc/")
DOC_ROOT_FILES = ("README", "CONTRIBUTING", "CHANGELOG")
POINTER_FILES = ("AGENTS.md", "CLAUDE.md")
POINTER_MARKERS = (
    "<!-- coordinare:wiki-pointer:start -->",
    "<!-- coordinare:wiki-pointer:end -->",
)
PLAN_CAP = 8
PAGE_MIN_CHARS = 400
PAGE_MAX_CHARS = 12000
SUMMARY_MAX_CHARS = 300
POINTER_MAX_LINES = 40
BAD_LINK_TEXT = ("here", "link", "this")


class WikiPage(BaseModel):
    """Inventory page from docs/wiki/."""

    path: str
    kind: str | None
    title: str
    citations: list[str]
    links: list[str]
    size: int
    summary: str = ""  # first paragraph after the H1, for the README entry

    model_config = {"extra": "forbid"}


class RepositoryLayout(BaseModel):
    """Repository structure for init mode."""

    project_name: str
    packages: list[dict[str, Any]] = Field(default_factory=list)
    has_ci: bool
    test_command_hint: str = ""

    model_config = {"extra": "forbid"}


class PagePlan(BaseModel):
    """Plan entry for a page to document."""

    path: str
    kind: str | None
    source: Literal["brief", "inventory", "init", "index"]
    justification: str
    exists: bool
    say: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class PageEvidence(BaseModel):
    """Commands run during gather phase for a page."""

    commands: list[dict[str, Any]] = Field(default_factory=list)
    chars: int

    model_config = {"extra": "forbid"}


class PageResult(BaseModel):
    """Result after gate for a page."""

    path: str
    kind: str | None = None
    action: Literal["write", "unchanged", "retire", "generated"] = "write"
    reason: str = ""
    citations_checked: int = 0
    citations_missing: list[str] = Field(default_factory=list)
    links_missing: list[str] = Field(default_factory=list)
    contract_failures: list[str] = Field(default_factory=list)
    size: int = 0
    dropped: bool = False
    drop_reason: str | None = None

    model_config = {"extra": "forbid"}

    @field_validator("reason")
    @classmethod
    def reason_max_300(cls, v: str) -> str:
        if len(v) > 300:
            raise ValueError("reason must be <= 300 characters")
        return v


class DocsRecord(BaseModel):
    """Complete documenter run record."""

    mode: Literal["update", "init"]
    brief_present: bool = False
    changed_files: list[str]
    diff_truncated: bool = False
    inventory_size: int = 0
    plan: list[PagePlan] = Field(default_factory=list, max_length=8)
    deferred: list[str] = Field(default_factory=list)
    refused_paths: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    results: list[PageResult] = Field(default_factory=list, max_length=8)
    files_written: list[str]
    files_retired: list[str] = Field(default_factory=list)
    readme_generated: bool = False
    pointers_refreshed: list[str] = Field(default_factory=list)
    reverted_paths: list[str] = Field(default_factory=list)
    commit_sha: str | None = None
    verdict: Literal["docs_committed", "env_blocked"]
    hold_reason: str | None = None
    workflow_metrics: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


def model_page_write_schema(max_chars: int) -> type[BaseModel]:
    """Generate the model-facing schema for a page write (FR-006).

    The path is fixed by the plan and must not appear in the schema.

    Args:
        max_chars: Maximum characters for content, from DOC_PAGE_MAX_CHARS or budgets.

    Returns:
        A pydantic model class with action, content, reason and extra='forbid'.
    """

    class ModelPageWrite(BaseModel):
        action: Literal["write", "unchanged", "retire"]
        content: str = ""
        reason: str = ""

        model_config = {"extra": "forbid"}

        @field_validator("content")
        @classmethod
        def content_max_size(cls, v: str) -> str:
            if len(v) > max_chars:
                raise ValueError(f"content must be <= {max_chars} characters")
            return v

        @field_validator("reason")
        @classmethod
        def reason_max_300(cls, v: str) -> str:
            if len(v) > 300:
                raise ValueError("reason must be <= 300 characters")
            return v

    return ModelPageWrite
