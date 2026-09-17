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

from performer.workflows.reviewer.diffparse import _UNNAMED_TAIL_PATH, line_in_hunks
from performer.workflows.reviewer.models import ADVISORY_CATEGORIES, ChangedFile, Disposition, Finding, PriorComment

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
    "blocking_of",
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


def _module_paths(modules: Any) -> list[str]:
    """Brief modules arrive serialized as dicts (``{"path": ..., "note": ...}``);
    bare strings are accepted for older relays, and live Module objects carry
    ``.path``. Normalize every shape to its path."""
    out = []
    for m in modules or []:
        if isinstance(m, dict):
            p = str(m.get("path") or "").strip()
        elif hasattr(m, "path"):
            p = str(getattr(m, "path", "") or "").strip()
        else:
            p = str(m).strip()
        if p:
            out.append(p)
    return out


def add_documentation_by_implementer(findings: list[Finding], changed_files: list[ChangedFile], brief: dict[str, Any] | None) -> list[Finding]:
    """FR-008: brief present and the diff touches documentation is a finding.

    412: not when the work itself is documentation. A docs-scoped brief (every
    module it names is a documentation path) and a diff that touches only
    documentation paths are the deliverable, not a lane violation; a finding
    there would block every docs PR with itself.
    """
    if not brief:
        return list(findings)
    raw_modules = brief.get("modules")
    modules = _module_paths(raw_modules)
    # 412 round 26, corrected round 30: an empty ``modules`` list alone is
    # NOT docs-scoped -- the architect schema explicitly permits
    # ``modules: []`` with ``docs: []`` for ordinary code cards, and a
    # projection may omit ``docs``. Docs-scoping needs affirmative
    # evidence: every named module is a documentation path, or the brief
    # carries a non-empty ``docs`` list. Without evidence the diff is
    # flagged -- an advisory note is the safe side.
    raw_docs = brief.get("docs")
    # 412 round 32: a brief naming code modules is code-scoped even when it
    # also lists documentation topics -- the docs-list fallback needs a
    # modules list that is empty, so a mixed brief cannot exempt a docs-only
    # diff from the lane rule.
    docs_scoped_brief = (
        (bool(modules) and all(is_documentation_path(m) for m in modules))
        or (not modules and isinstance(raw_docs, list) and len(raw_docs) > 0)
    )
    touched = touches_documentation_tree(changed_files)
    if not touched:
        return list(findings)
    # 412: the exception is a docs-scoped brief whose diff is documentation
    # and nothing else. A code-scoped brief with a docs-only diff is a lane
    # violation, and so is a docs-scoped brief whose diff reaches into code.
    # 412 round 17: the synthetic truncation tail is a coverage marker, not a
    # path -- it is not documentation, but it must not defeat the exception;
    # the truncation is held by the coverage gates, not this lane rule.
    visible = [f for f in changed_files if f.path != _UNNAMED_TAIL_PATH]
    if docs_scoped_brief and visible and all(is_documentation_path(f.path) for f in visible):
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
    # 412 round 8: a deleted file is absent from the worktree -- it can never
    # be opened, so it is covered by the diff's own removals.
    return all(f.fully_in_diff or f.opened_by_survey or f.deleted for f in changed_files)


def blocking_of(findings: list[Finding]) -> list[Finding]:
    """412: the advisory tier (style, test_missing) never blocks; the rest do."""
    return [f for f in findings if f.category not in ADVISORY_CATEGORIES]


def verdict(findings: list[Finding], coverage_ok: bool) -> str:
    """412: advisory findings are posted but never block; approval needs coverage."""
    if blocking_of(findings):
        return "changes_requested"
    return "approved" if coverage_ok else "env_blocked"


def cap_findings(findings: list[Finding], limit: int = MAX_FINDINGS) -> list[Finding]:
    """412: blocking findings never fall off the end of the cap -- the advisory
    tier fills the remaining budget instead. 30 style findings can no longer
    push a logic_error past the line and read as "approved"."""
    blocking = [f for f in findings if f.category not in ADVISORY_CATEGORIES]
    advisory = [f for f in findings if f.category in ADVISORY_CATEGORIES]
    kept_blocking = blocking[:limit]
    return kept_blocking + advisory[: max(0, limit - len(kept_blocking))]


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
    blocking: list[Finding] = field(default_factory=list)
    fixed_ids: list[str] = field(default_factory=list)


def run_gate(
    model_findings: list[Finding],
    model_dispositions: Iterable[Any],
    *,
    changed_files: list[ChangedFile],
    prior_comments: list[PriorComment],
    diff_lines: list[str],
    survey_lines: list[str],
    brief: dict[str, Any] | None,
    truncated: bool,
    coverage_pass_ran: bool,
    reanchored: list[Finding] | None = None,
    surveyed_files: Iterable[str] = (),
) -> GateOutcome:
    # 412 round 32: the anchor pool is the caller's STRICT read set
    # (``opened_paths`` -- explicit content reads) plus the workflow's
    # discovered unchanged-path reads. The loose ``opened_by_survey`` flag
    # drives coverage only: a read-only command that merely names the path
    # (``git log -p -- path``) shows the model the file exists, but is not
    # content a finding's evidence can come from.
    surveyed = set(surveyed_files)
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
    kept = add_documentation_by_implementer(kept, changed_files, brief)
    pre_cap = list(kept)
    kept = cap_findings(kept)
    # 412 round 7: cap_findings reorders (blocking first) and truncates. The
    # dispositions' finding_index was minted against the pre-cap order --
    # remap onto the final order so a persisted not_fixed disposition points
    # at the same finding; one whose finding fell off the cap becomes None.
    if len(kept) != len(pre_cap) or [id(f) for f in kept] != [id(f) for f in pre_cap]:
        final_position = {id(f): i for i, f in enumerate(kept)}
        for d in dispositions:
            idx = d.finding_index
            if idx is None or not (0 <= idx < len(pre_cap)):
                d.finding_index = None
                continue
            landed = final_position.get(id(pre_cap[idx]))
            d.finding_index = landed if landed is not None else None
    coverage_ok = full_coverage(changed_files, truncated, coverage_pass_ran)
    unread = [f.path for f in changed_files if not (f.fully_in_diff or f.opened_by_survey or f.deleted)]
    covered = [f.path for f in changed_files if f.fully_in_diff or f.opened_by_survey or f.deleted]
    return GateOutcome(
        findings=kept, dropped=dropped, dispositions=dispositions, verdict=verdict(kept, coverage_ok),
        unread_files=unread if not coverage_ok else [], covered_files=covered,
        blocking=blocking_of(kept),
        fixed_ids=[d.prior_comment_id for d in dispositions if d.status == "fixed"],
    )
