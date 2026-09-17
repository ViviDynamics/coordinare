"""Bounded per-role analysis inputs for the single documentation writer (175)."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

# Deliberately omit raw diffs, command output, logs, prompts and posting metadata.
_FIELDS = {
    "assessing": ("assessment", ("goal", "expected_behavior", "out_of_scope", "assumptions", "criteria")),
    "architecting": ("blueprint", ("summary", "docs", "modules", "interfaces", "risks")),
    "reviewing": ("review", ("verdict", "findings", "advisory_findings", "covered_files")),
    "security": ("security", ("verdict", "blocking", "advisory", "covered_files", "baseline_scanner_findings")),
    "qa": (None, ("passed", "criteria_checked", "criteria_passed", "failures", "qa_findings")),
}
# 412: the explicit empty-diff verdicts advance through the terminal-success
# path and must collect their structured reports like any other terminal --
# otherwise a reviewing/security stage that ends with nothing_to_review,
# nothing_to_scan or not_applicable silently contributes nothing.
_TERMINALS = {"assessment_complete", "plan_committed", "approved", "changes_requested", "security_passed", "security_failed", "qa_passed", "qa_failed", "nothing_to_review", "nothing_to_scan", "not_applicable"}
MAX_RECORD_CHARS = 12000


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, list):
        return [_bounded(v, depth + 1) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k)[:80]: _bounded(v, depth + 1) for k, v in list(value.items())[:20]}
    return value if value is None or isinstance(value, (bool, int, float)) else None


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def clean_findings(value: Any) -> dict[str, Any]:
    """Validate optional persisted state without making older snapshots unloadable."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for role in _FIELDS:
        entry = value.get(role)
        if not isinstance(entry, dict) or not isinstance(entry.get("findings"), dict):
            continue
        findings = _bounded(entry["findings"])
        if len(json.dumps(findings, ensure_ascii=True)) > MAX_RECORD_CHARS:
            continue
        head = str(entry.get("source_head") or "")[:64]
        result[role] = {"role": role, "source_head": head, "findings": findings,
                        "terminal_marker": str(entry.get("terminal_marker") or "")[:64],
                        "truncated": bool(entry.get("truncated", False)),
                        "content_hash": content_hash({"source_head": head, "findings": findings})}
    return result


def collect(state: dict[str, Any], stage: str, marker: str, status: dict[str, Any]) -> None:
    """Record completed structured analysis; prose/unknown reports add nothing."""
    if stage not in _FIELDS or marker not in _TERMINALS or status.get("env_cache_health_failed"):
        return
    report = status.get("report")
    if not isinstance(report, dict):
        return
    key, fields = _FIELDS[stage]
    record = report.get(key) if key else report
    if not isinstance(record, dict):
        return
    findings = {}
    for field in fields:
        if field not in record:
            continue
        candidate = {**findings, field: _bounded(record[field])}
        while (isinstance(candidate[field], list) and candidate[field]
               and len(json.dumps(candidate, ensure_ascii=True)) > MAX_RECORD_CHARS):
            candidate[field].pop()
        if len(json.dumps(candidate, ensure_ascii=True)) <= MAX_RECORD_CHARS:
            findings = candidate
    if not findings:
        return
    current = clean_findings(state.get("documentation_findings"))
    head = str(status.get("head_sha") or state.get("head_at_last_turn") or state.get("head_at_dispatch") or "")[:64]
    if stage == "qa":
        findings["passed"] = marker == "qa_passed"
    current[stage] = {"role": stage, "source_head": head, "findings": findings,
                      "terminal_marker": marker,
                      "truncated": findings != {k: record[k] for k in fields if k in record},
                      "content_hash": content_hash({"source_head": head, "findings": findings})}
    state["documentation_findings"] = current


def reset(state: CoordinareState, stage: str) -> None:
    if stage in _FIELDS and state.get("documentation_findings"):
        records = clean_findings(state["documentation_findings"])
        records.pop(stage, None)
        state["documentation_findings"] = records


def inject(context: dict[str, Any], state: CoordinareState, stage: str) -> None:
    if stage == "documenting":
        records = clean_findings(state.get("documentation_findings"))
        if records:
            context["documentation_findings"] = records
        context["documenting_side_run"] = False
    elif stage == "implementing":
        side = state.get("documenting_side") or {}
        if isinstance(side, dict) and side.get("status") == "done" and side.get("head_sha"):
            context["completed_documentation"] = {"head_sha": side["head_sha"], "paths": copy.deepcopy(side.get("paths") or [])}
