"""Reviewer gate (spec 169 FR-006 to FR-009, FR-017): pure rules over the findings.

Every rule is a pure function with its own test, shown to fail under one
mutation (data-model "Rule predicates"). The gate drops findings whose anchor
cannot be verified, adds ``unaddressed_feedback`` for prior comments without a
disposition, adds ``documentation_by_implementer`` when the brief is present
and the diff touches the documentation tree, and derives the verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from performer.workflows.reviewer.diffparse import line_in_hunks
from performer.workflows.reviewer.models import ChangedFile, Disposition, Finding, PriorComment

__all__ = [
    "GateOutcome",
    "anchor_in_hunks",
    "anchor_in_surveyed",
    "evidence_matches",
    "anchor_ok",
    "has_disposition",
    "add_unaddressed_feedback",
    "is_documentation_path",
    "touches_documentation_tree",
    "add_documentation_by_implementer",
    "full_coverage",
    "verdict",
    "cap_findings",
    "to_findings",
    "run_gate",
]

DOCUMENTATION_DIRS = ("docs/", "doc/")
DOCUMENTATION_BASENAMES = ("README", "CONTRIBUTING", "CHANGELOG")
MAX_FINDINGS = 30


def anchor_in_hunks(finding: Finding, changed_files: list[ChangedFile]) -> bool:
    """The path is a changed file and the line falls in one of its new-side hunks."""
    for f in changed_files:
        if f.path == finding.path:
            return line_in_hunks(f, finding.line)
    return False


def anchor_in_surveyed(finding: Finding, surveyed_files: Iterable[str]) -> bool:
    """The file was opened by the survey, so any line of it may carry a finding."""
    return finding.path in set(surveyed_files)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def evidence_matches(finding: Finding, diff_lines: list[str], survey_lines: list[str]) -> bool:
    """The evidence is a substring of some diff or survey line (whitespace folded).

    Rule-origin findings carry no evidence and pass; a model finding with empty
    evidence never matches.
    """
    if finding.origin == "rule":
        return finding.evidence == ""
    needle = _normalise(finding.evidence)
    if not needle:
        return False
    for line in diff_lines:
        if needle in _normalise(line):
            return True
    for line in survey_lines:
        if needle in _normalise(line):
            return True
    return False


def anchor_ok(finding: Finding, changed_files: list[ChangedFile], surveyed_files: Iterable[str], diff_lines: list[str], survey_lines: list[str]) -> bool:
    """A surveyed sink may be unchanged, but its introducing cause must be changed."""
    changed = {f.path for f in changed_files}
    surveyed = set(surveyed_files)
    cause = finding.introduced_by or finding.path
    if cause not in changed:
        return False
    if finding.path in changed:
        placed = anchor_in_hunks(finding, changed_files) or finding.path in surveyed
    else:
        placed = finding.path in surveyed
    return placed and evidence_matches(finding, diff_lines, survey_lines)


def has_disposition(comment_id: str, dispositions: Iterable[Any]) -> bool:
    """FR-007: the comment id is named by some disposition."""
    return any(str(getattr(d, "prior_comment_id", None) or (d.get("prior_comment_id") if isinstance(d, dict) else None)) == str(comment_id) for d in dispositions)


def add_unaddressed_feedback(findings: list[Finding], prior_comments: list[PriorComment], dispositions: Iterable[Any]) -> list[Finding]:
    """FR-007: one rule finding per prior comment without a disposition."""
    out = list(findings)
    for c in prior_comments:
        if has_disposition(c.id, dispositions):
            continue
        out.append(Finding(
            path=c.path, line=c.line if c.path else 0, category="unaddressed_feedback",
            problem=f"Prior review comment {c.id} was not addressed: {c.body[:400]}",
            why_blocking="Open review feedback must be addressed or answered before approval.",
            evidence="", origin="rule",
        ))
    return out


def is_documentation_path(path: str) -> bool:
    """docs/, doc/, or a basename starting with README, CONTRIBUTING or CHANGELOG."""
    if any(path.startswith(d) for d in DOCUMENTATION_DIRS):
        return True
    base = path.rsplit("/", 1)[-1]
    return any(base.startswith(b) for b in DOCUMENTATION_BASENAMES)


def touches_documentation_tree(changed_files: list[ChangedFile]) -> list[str]:
    return [f.path for f in changed_files if is_documentation_path(f.path)]


def add_documentation_by_implementer(findings: list[Finding], changed_files: list[ChangedFile], brief_present: bool) -> list[Finding]:
    """FR-008: brief present and the diff touches documentation is a finding."""
    if not brief_present:
        return list(findings)
    touched = touches_documentation_tree(changed_files)
    if not touched:
        return list(findings)
    first = touched[0]
    changed = next(f for f in changed_files if f.path == first)
    line = changed.hunks[0].start_line if changed.hunks else 0
    return list(findings) + [Finding(
        path=first, line=line, category="documentation_by_implementer",
        problem="The implementer edited documentation: " + ", ".join(touched[:10]),
        why_blocking="Documentation is owned by the documenter stage; the implementer's brief scopes code and tests only.",
        evidence="", origin="rule",
    )]


def full_coverage(changed_files: list[ChangedFile], truncated: bool, coverage_pass_ran: bool) -> bool:
    """FR-009: every changed file fully in the diff or opened; a truncated diff needs the coverage pass."""
    if truncated and not coverage_pass_ran:
        return False
    return all(f.fully_in_diff or f.opened_by_survey for f in changed_files)


def verdict(findings: list[Finding], coverage_ok: bool) -> str:
    """Any surviving finding is changes_requested; none is approved only with coverage."""
    if findings:
        return "changes_requested"
    return "approved" if coverage_ok else "env_blocked"


def cap_findings(findings: list[Finding], limit: int = MAX_FINDINGS) -> list[Finding]:
    return list(findings)[:limit]


def to_findings(model_findings: Any, categories: Iterable[str]) -> list[Finding]:
    """Model output to Finding records with origin=model; unknown categories are refused upstream by the schema."""
    allowed = set(categories)
    out: list[Finding] = []
    for mf in getattr(model_findings, "findings", []) or []:
        if mf.category not in allowed:
            continue
        out.append(Finding(path=mf.path, line=mf.line, category=mf.category, problem=mf.problem,
                           why_blocking=mf.why_blocking, evidence=mf.evidence, origin="model",
                           introduced_by=getattr(mf, "introduced_by", "")))
    return out


@dataclass
class GateOutcome:
    findings: list[Finding]
    dropped: list[Finding]
    dispositions: list[Disposition]
    verdict: str
    unread_files: list[str]
    covered_files: list[str]
    fixed_ids: list[str] = field(default_factory=list)


def run_gate(
    model_findings: list[Finding],
    model_dispositions: Iterable[Any],
    *,
    changed_files: list[ChangedFile],
    prior_comments: list[PriorComment],
    diff_lines: list[str],
    survey_lines: list[str],
    brief_present: bool,
    truncated: bool,
    coverage_pass_ran: bool,
    reanchored: list[Finding] | None = None,
    surveyed_files: Iterable[str] = (),
) -> GateOutcome:
    surveyed = set(surveyed_files) | {f.path for f in changed_files if f.opened_by_survey}
    kept, dropped = [], []
    for f in model_findings:
        (kept if anchor_ok(f, changed_files, surveyed, diff_lines, survey_lines) else dropped).append(f)
    for f in reanchored or []:
        if anchor_ok(f, changed_files, surveyed, diff_lines, survey_lines):
            kept.append(f)
    known = {c.id for c in prior_comments}
    dispositions: list[Disposition] = []
    for d in model_dispositions:
        pid = str(getattr(d, "prior_comment_id", ""))
        if pid in known:
            idx = getattr(d, "finding_index", None)
            dispositions.append(Disposition(prior_comment_id=pid, status=d.status, finding_index=idx if isinstance(idx, int) and 0 <= idx < len(kept) else None))
    kept = add_unaddressed_feedback(kept, prior_comments, dispositions)
    kept = add_documentation_by_implementer(kept, changed_files, brief_present)
    kept = cap_findings(kept)
    coverage_ok = full_coverage(changed_files, truncated, coverage_pass_ran)
    unread = [f.path for f in changed_files if not (f.fully_in_diff or f.opened_by_survey)]
    covered = [f.path for f in changed_files if f.fully_in_diff or f.opened_by_survey]
    return GateOutcome(
        findings=kept, dropped=dropped, dispositions=dispositions, verdict=verdict(kept, coverage_ok),
        unread_files=unread if not coverage_ok else [], covered_files=covered,
        fixed_ids=[d.prior_comment_id for d in dispositions if d.status == "fixed"],
    )
