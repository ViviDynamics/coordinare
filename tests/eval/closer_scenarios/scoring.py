"""Scoring for one closer run (spec 172 SC-001 to SC-003, SC-005)."""
from __future__ import annotations

from dataclasses import dataclass, field

from performer.workflows.closer.gate import _fold
from performer.workflows.closer.models import ClosingRecord

from tests.eval.closer_scenarios.fixtures import Fixture


@dataclass
class Score:
    fixture: str
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def score_run(fixture: Fixture, report: dict, gh, model_calls: int, *, live: bool = False) -> Score:
    exp = fixture.expect
    s = Score(fixture=fixture.name)
    try:
        record = ClosingRecord.model_validate(report.get("closing") or {})
    except Exception as exc:
        s.checks["record_valid"] = False
        s.notes.append(f"closing record invalid: {exc}")
        return s
    s.checks["record_valid"] = True
    accepted = set(exp.live_verdicts) if live and exp.live_verdicts else {exp.verdict}
    s.checks["verdict"] = record.verdict in accepted
    if not s.checks["verdict"]:
        s.notes.append(f"verdict {record.verdict}, expected {sorted(accepted)}")
    if exp.model_calls is not None and not live:
        s.checks["model_calls"] = model_calls == exp.model_calls
        if not s.checks["model_calls"]:
            s.notes.append(f"{model_calls} model call(s), expected {exp.model_calls}")
    # SC-001: never approve with an open thread
    s.checks["no_approval_with_open"] = not (record.verdict == "approved" and record.open_threads)
    # SC-003: every resolution is justified by the outdated rule or a quote in that thread
    bodies = {t["id"]: " ".join(c["body"] for c in t["comments"]) for t in fixture.threads}
    for entry in record.resolved:
        reason = entry.get("reason", "")
        tid = entry.get("thread_id", "")
        quote = reason[len("addressed:"):].strip() if reason.startswith("addressed:") else ""
        ok = reason.startswith("outdated") or bool(quote and _fold(quote) in _fold(bodies.get(tid, "")))
        s.checks[f"justified:{tid}"] = ok
        if not ok:
            s.notes.append(f"{tid} resolved with an unjustified reason: {reason[:80]}")
    if not live:
        s.checks["resolved_set"] = tuple(sorted(gh.resolved)) == tuple(sorted(exp.resolved))
        s.checks["open_set"] = tuple(sorted(t["thread_id"] for t in record.open_threads)) == tuple(sorted(exp.open_ids))
    s.checks["one_review"] = len(gh.reviews) == 1 and gh.reviews[0]["event"] == "COMMENT"
    s.checks["nothing_resolved_on_rejection"] = not (record.verdict == "changes_requested" and gh.resolved)
    return s
