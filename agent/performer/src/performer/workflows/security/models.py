"""Models for the security workflow (spec 170).

SecurityFinding, ScanResult, SecurityRecord per data-model.md.
Every field is bounded and required. Extra fields are forbidden.
The model-facing schema is built per-run with configurable categories.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

from performer.workflows.reviewer.models import ChangedFile


class _Bounded(BaseModel):
    model_config = ConfigDict(extra="forbid")


SECURITY_CATEGORIES = (
    "injection",
    "broken_authorization",
    "hardcoded_secret",
    "insecure_deserialization",
    "path_traversal",
    "ssrf",
    "weak_crypto",
    "missing_hardening",
    "information_leak",
    "vulnerable_dependency",
    "supply_chain",
    "insecure_configuration",
    "other_insecure_pattern",
)

CATEGORY_TABLE: dict[str, str] = {
    "hardcoded_secret": "critical",
    "injection": "high",
    "broken_authorization": "high",
    "insecure_deserialization": "high",
    "path_traversal": "high",
    "ssrf": "high",
    "vulnerable_dependency": "high",
    "supply_chain": "high",
    "insecure_configuration": "high",
    "weak_crypto": "medium",
    "missing_hardening": "medium",
    "information_leak": "medium",
    "other_insecure_pattern": "medium",
}

ROUTING: dict[str, str] = {
    "broken_authorization": "architect",
    "insecure_configuration": "architect",
}

BLOCKING = frozenset({"critical", "high"})



class SecurityFinding(_Bounded):
    """A security issue identified by a scanner or the model."""

    path: str  # Changed or surveyed file; scanner findings: the tool's file
    line: Annotated[int, Field(ge=0)]  # New-side line number; 0 if no line info
    category: Annotated[str, StringConstraints(min_length=1, max_length=64)]  # One of SECURITY_CATEGORIES after mapping
    problem: Annotated[str, StringConstraints(min_length=1, max_length=500)]  # What is wrong
    why_blocking: Annotated[str, StringConstraints(min_length=1, max_length=500)]  # Why this must be fixed
    evidence: Annotated[str, StringConstraints(max_length=200)]  # Offending code; "" only for origin == "rule"
    origin: Literal["model", "rule"]  # "model" from findings call, "rule" from scanner
    severity: Literal["critical", "high", "medium", "low"]
    routing: Literal["implementer", "architect"]
    introduced_by: str  # A changed file
    tool: str  # "model" or the scanner tool name determined by the model (spec 366)
    downgraded: bool = False
    downgrade_reason: Annotated[str, StringConstraints(max_length=300)] = ""

    @model_validator(mode="after")
    def _evidence_empty_only_for_rules(self) -> SecurityFinding:
        """Evidence can be empty only for rule-added (scanner) findings."""
        if not self.evidence and self.origin != "rule":
            raise ValueError("evidence must be non-empty for model-added findings")
        return self

    @model_validator(mode="after")
    def _downgraded_only_for_model(self) -> SecurityFinding:
        """downgraded=True only when tool == "model"."""
        if self.downgraded and self.tool != "model":
            raise ValueError("downgraded can only be true for model-added findings (tool='model')")
        return self


class ScanResult(_Bounded):
    """Result of running a single scanner tool.

    The tool name is determined at runtime by the model's judgement of what
    scanning applies to this repository. Spec 366: rather than a fixed pair
    (semgrep + bandit), the model decides what tooling is appropriate,
    reversing spec 170's fail-closed scanner-before-model design.
    """

    tool: str  # Tool name determined by model (e.g. "semgrep", "bandit", "golangci-lint", etc.)
    command: str  # Shell-quoted command that was run
    exit_code: int | None  # None if tool crashed before exiting
    finding_count: Annotated[int, Field(ge=0)]
    duration_ms: Annotated[int, Field(ge=0)]
    error: str | None = None  # Set only on the hold path (tool unavailable, timeout, malformed output)


class SecurityRecord(_Bounded):
    """The complete security workflow execution record, travels in PerformerResponse.report."""

    changed_files: list[ChangedFile]  # the reviewer's parsed diff shape
    diff_truncated: bool
    scan: list[ScanResult]  # One per tool, in run order
    unread_files: list[str] = Field(default_factory=list)  # After coverage pass, still unread
    survey_commands: Annotated[list[dict[str, Any]], Field(default_factory=list)]
    survey_refusals: Annotated[list[dict[str, Any]], Field(default_factory=list)]
    findings_before_gate: Annotated[list[SecurityFinding], Field(default_factory=list, max_length=30)]  # Model findings, before anchor rule
    findings_dropped: Annotated[list[SecurityFinding], Field(default_factory=list, max_length=30)]  # Unanchored, dropped
    findings_after_anchor_recheck: Annotated[list[SecurityFinding], Field(default_factory=list, max_length=30)]  # Reprompted
    scanner_findings: Annotated[list[SecurityFinding], Field(default_factory=list, max_length=200)]  # Rule findings after category mapping
    #: 412 round 18: unanchored tool findings -- pre-existing issues at
    #: unchanged lines. Reported, never blocking, never dropped.
    baseline_scanner_findings: Annotated[list[SecurityFinding], Field(default_factory=list, max_length=200)]  # Unanchored tool findings: reported, never blocking
    blocking: Annotated[list[SecurityFinding], Field(default_factory=list, max_length=230)]  # Surviving, severity in BLOCKING
    advisory: Annotated[list[SecurityFinding], Field(default_factory=list, max_length=230)]  # Surviving, medium or low
    coverage_pass_ran: bool = False
    coverage_pass_output: str = ""
    #: 412: nothing_to_scan (the parsed diff carried no files) and
    #: not_applicable (no statically scannable source, decided from file
    #: types by code) are explicit advance-with-note verdicts the coordinare
    #: records; they are neither a pass nor a hold.
    verdict: Literal["security_passed", "security_failed", "env_blocked", "nothing_to_scan", "not_applicable"]
    hold_reason: str | None = None  # Scanner tool/reason, unread files, or post error
    covered_files: list[str]
    post_error: str | None = None
    posted_review_url: str | None = None
    workflow_metrics: dict[str, Any] = Field(default_factory=dict)


def model_security_findings_schema(categories: tuple[str, ...] | list[str], max_findings: int = 30) -> type[BaseModel]:
    """Build the schema the security findings call is validated against.

    category is pinned to a Literal of the configured set, so an unknown
    category is a schema violation (one reprompt) rather than a silent drop.
    extra="forbid" on both levels rejects severity, routing, and verdict keys:
    these are derived by code, not supplied by the model.
    """
    cats = tuple(dict.fromkeys(str(c) for c in categories if str(c)))
    if not cats:
        raise ValueError("the security workflow needs at least one finding category")
    category_type = Literal[cats]  # type: ignore[valid-type]

    class ModelSecurityFinding(_Bounded):
        path: str
        line: Annotated[int, Field(ge=0)]
        category: category_type  # type: ignore[valid-type]
        problem: Annotated[str, StringConstraints(min_length=1, max_length=500)]
        why_blocking: Annotated[str, StringConstraints(min_length=1, max_length=500)]
        evidence: Annotated[str, StringConstraints(min_length=1, max_length=200)]
        introduced_by: str
        downgrade_reason: Annotated[str, StringConstraints(max_length=300)] = ""

    class ModelSecurityFindings(_Bounded):
        findings: Annotated[list[ModelSecurityFinding], Field(default_factory=list, max_length=max_findings)]

    ModelSecurityFinding.__name__ = "ModelSecurityFinding"
    ModelSecurityFindings.__name__ = "ModelSecurityFindings"
    return ModelSecurityFindings


__all__ = [
    "SecurityFinding",
    "ScanResult",
    "SecurityRecord",
    "SECURITY_CATEGORIES",
    "CATEGORY_TABLE",
    "ROUTING",
    "BLOCKING",
    "model_security_findings_schema",
]
