"""Security gate (spec 170 FR-008 to FR-012, FR-019): pure rules over the findings.

Every rule is a pure function with its own test, shown to fail under one
mutation (data-model "Rule predicates"). The anchor rule is the reviewer's,
widened for a sink in a file the survey opened and tied to the PR through
``introduced_by``. Severity and routing come from the category table. Scanner
findings are rule findings with the tool's severity and can never be dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from performer.workflows.reviewer.gate import anchor_in_hunks, anchor_in_surveyed, evidence_matches, full_coverage
from performer.workflows.reviewer.models import ChangedFile
from performer.workflows.security.models import BLOCKING, CATEGORY_TABLE, CWE_TO_CATEGORY, ROUTING, SecurityFinding

__all__ = [
    "GateOutcome", "anchor_ok_security", "severity_for", "routing_for", "apply_downgrade", "map_scanner_category",
    "scanner_to_findings", "normalise_tool_path", "to_findings", "merge_scanner_findings", "split_blocking", "verdict", "run_gate", "MAX_MODEL_SURVIVORS", "MAX_SCANNER_FINDINGS",
]

MAX_MODEL_SURVIVORS = 30
MAX_SCANNER_FINDINGS = 200
_FALLBACK_CATEGORY = "other_insecure_pattern"


def severity_for(category: str) -> str:
    """The category table; an unknown category is advisory."""
    return CATEGORY_TABLE.get(category, "medium")


def routing_for(category: str) -> str:
    return ROUTING.get(category, "implementer")


def apply_downgrade(f: SecurityFinding) -> SecurityFinding:
    """A model finding in a blocking category with a reason becomes advisory, visibly (FR-010)."""
    if f.tool == "model" and f.severity in BLOCKING and f.downgrade_reason.strip():
        return f.model_copy(update={"severity": "medium", "downgraded": True})
    return f


def anchor_ok_security(f: SecurityFinding, changed_files: list[ChangedFile], surveyed_files: Iterable[str], diff_lines: list[str], survey_lines: list[str]) -> bool:
    """FR-008: (changed path AND (line in hunk OR opened)) OR opened unchanged path; introduced_by changed; evidence matches."""
    changed = {c.path for c in changed_files}
    surveyed = set(surveyed_files)
    if f.path in changed:
        placed = anchor_in_hunks(f, changed_files) or anchor_in_surveyed(f, surveyed)
    else:
        placed = f.path in surveyed
    if not placed:
        return False
    if f.introduced_by not in changed:
        return False
    return evidence_matches(f, diff_lines, survey_lines)


_KEYWORD_CATEGORIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("sql", "inject", "xss", "command", "shell", "eval", "exec-", "template"), "injection"),
    (("secret", "credential", "hardcoded", "password", "api-key", "apikey", "token"), "hardcoded_secret"),
    (("deserial", "pickle", "yaml-load", "marshal", "unpickle"), "insecure_deserialization"),
    (("traversal", "path-join", "path_join"), "path_traversal"),
    (("ssrf", "request-forgery"), "ssrf"),
    (("crypto", "cipher", "hash", "md5", "sha1", "random", "tls", "ssl"), "weak_crypto"),
    (("authz", "authoriz", "permission", "idor", "access-control"), "broken_authorization"),
    (("debug", "traceback", "expose", "leak", "verbose"), "information_leak"),
)


def map_scanner_category(raw: str) -> str:
    """A CWE number (as a whole token) maps through the table; a rule id maps by keyword; anything else is other_insecure_pattern.

    The live round showed semgrep rules without CWE metadata (``python.flask.security.injection.tainted-sql-string``)
    landing in the fallback and never collapsing into the model's ``injection`` finding at the same line.
    """
    text = str(raw or "").strip()
    cwe_tokens = [text] if text.isdigit() else []
    cwe_tokens += re.findall(r"\bCWE-(\d+)\b", text)
    for token in cwe_tokens:
        if token in CWE_TO_CATEGORY:
            return CWE_TO_CATEGORY[token]
    lowered = text.lower()
    for needles, category in _KEYWORD_CATEGORIES:
        if any(n in lowered for n in needles):
            return category
    return _FALLBACK_CATEGORY


def normalise_tool_path(path: str) -> str:
    """Tool paths as the diff names them: bandit reports ``./app/web.py`` for ``app/web.py``."""
    text = str(path or "").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def scanner_to_findings(raw: list[dict[str, Any]], tool_of: dict[str, str] | None = None) -> list[SecurityFinding]:
    """Tool findings in the 083 shape become rule findings with the tool's severity (FR-011)."""
    out: list[SecurityFinding] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        description = str(r.get("description") or "")
        tool = "bandit" if description.startswith("bandit:") else "semgrep"
        category = map_scanner_category(str(r.get("category") or ""))
        if category == _FALLBACK_CATEGORY:
            # Live round: semgrep tags tainted-sql-string with CWE-915 and CWE-704, which say nothing
            # about injection; the rule id does. A CWE that maps nowhere defers to the rule id's words.
            category = map_scanner_category(description)
        path = normalise_tool_path(str(r.get("file") or ""))
        out.append(SecurityFinding(
            path=path, line=max(int(r.get("line") or 0), 0), category=category,
            problem=(description or f"{tool} finding")[:500], why_blocking=f"reported by {tool} as {r.get('severity')}"[:500],
            evidence="", origin="rule", severity=str(r.get("severity") or "medium"), routing=routing_for(category),
            introduced_by=path, tool=tool,
        ))
    return out


def to_findings(model_out: Any, categories: Iterable[str]) -> list[SecurityFinding]:
    """Model output to findings with severity and routing assigned by code, then the downgrade rule."""
    allowed = set(categories)
    out: list[SecurityFinding] = []
    for mf in getattr(model_out, "findings", []) or []:
        if mf.category not in allowed:
            continue
        f = SecurityFinding(
            path=mf.path, line=mf.line, category=mf.category, problem=mf.problem, why_blocking=mf.why_blocking, evidence=mf.evidence,
            origin="model", severity=severity_for(mf.category), routing=routing_for(mf.category), introduced_by=mf.introduced_by,
            tool="model", downgrade_reason=(getattr(mf, "downgrade_reason", "") or "")[:300],
        )
        out.append(apply_downgrade(f))
    return out


def merge_scanner_findings(model_survivors: list[SecurityFinding], scanner: list[SecurityFinding]) -> list[SecurityFinding]:
    """Scanner findings appended; a model finding at the same (path, line, category) collapses into the tool's."""
    keys = {(s.path, s.line, s.category) for s in scanner}
    kept = [m for m in model_survivors if (m.path, m.line, m.category) not in keys]
    return kept + list(scanner)


def split_blocking(findings: list[SecurityFinding]) -> tuple[list[SecurityFinding], list[SecurityFinding]]:
    blocking = [f for f in findings if f.severity in BLOCKING]
    advisory = [f for f in findings if f.severity not in BLOCKING]
    return blocking, advisory


def verdict(blocking: list[SecurityFinding], coverage_ok: bool) -> str:
    if blocking:
        return "security_failed"
    return "security_passed" if coverage_ok else "env_blocked"


@dataclass
class GateOutcome:
    blocking: list[SecurityFinding]
    advisory: list[SecurityFinding]
    dropped: list[SecurityFinding]
    verdict: str
    unread_files: list[str]
    covered_files: list[str]
    scanner_findings: list[SecurityFinding] = field(default_factory=list)


def run_gate(
    model_findings: list[SecurityFinding], scanner_raw: list[dict[str, Any]], *, changed_files: list[ChangedFile], diff_lines: list[str],
    survey_lines: list[str], surveyed_files: Iterable[str], truncated: bool, coverage_pass_ran: bool, reanchored: list[SecurityFinding] | None = None,
) -> GateOutcome:
    surveyed = list(surveyed_files)
    kept, dropped = [], []
    for f in model_findings:
        (kept if anchor_ok_security(f, changed_files, surveyed, diff_lines, survey_lines) else dropped).append(f)
    for f in reanchored or []:
        if anchor_ok_security(f, changed_files, surveyed, diff_lines, survey_lines):
            kept.append(f)
    scanner = scanner_to_findings(scanner_raw)[:MAX_SCANNER_FINDINGS]
    # The model's survivors are capped; the tools' findings are never sliced out (FR-011).
    merged = merge_scanner_findings(kept[:MAX_MODEL_SURVIVORS], scanner)
    blocking, advisory = split_blocking(merged)
    coverage_ok = full_coverage(changed_files, truncated, coverage_pass_ran)
    unread = [c.path for c in changed_files if not (c.fully_in_diff or c.opened_by_survey)]
    covered = [c.path for c in changed_files if c.fully_in_diff or c.opened_by_survey]
    return GateOutcome(
        blocking=blocking, advisory=advisory, dropped=dropped, verdict=verdict(blocking, coverage_ok),
        unread_files=unread if not coverage_ok else [], covered_files=covered, scanner_findings=scanner,
    )
