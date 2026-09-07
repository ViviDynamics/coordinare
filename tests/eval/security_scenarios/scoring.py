"""Scoring for one security run against a fixture's expectations (spec 170 SC-001 to SC-003, SC-006).

The signals that matter: the verdict is derived by code, every surviving model
finding is anchored to a changed or surveyed file, scanner findings survive with
their tool, the expected categories appear, the downgrade is visible, exactly one
review with the right event (none on a hold), the hold names the tool, and the
tree stayed clean. Live mode relaxes the verdict to the fixture's accepted set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from performer.workflows.security.models import BLOCKING, SecurityRecord

from tests.eval.security_scenarios.fixtures import Fixture


@dataclass
class Score:
    fixture: str
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def score_run(fixture: Fixture, report: dict, reviews: list[dict], model_calls: int, *, live: bool = False) -> Score:
    exp = fixture.expect
    s = Score(fixture=fixture.name)
    try:
        record = SecurityRecord.model_validate(report.get("security") or {})
    except Exception as exc:
        s.checks["record_valid"] = False
        s.notes.append(f"security record invalid: {exc}")
        return s
    s.checks["record_valid"] = True

    accepted = set(exp.live_verdicts) if live and exp.live_verdicts else {exp.verdict}
    s.checks["verdict"] = record.verdict in accepted
    if not s.checks["verdict"]:
        s.notes.append(f"verdict {record.verdict}, expected {sorted(accepted)}")

    survivors = record.blocking + record.advisory
    n = len(record.blocking)
    s.checks["blocking_count"] = (exp.min_blocking <= n <= exp.max_blocking) if not live else True
    s.checks["verdict_matches_blocking"] = (record.verdict == "security_failed") == (n > 0) or record.verdict == "env_blocked"
    s.checks["blocking_are_blocking"] = all(f.severity in BLOCKING for f in record.blocking) and all(f.severity not in BLOCKING for f in record.advisory)

    changed = {f.path for f in record.changed_files}
    surveyed = {f.path for f in record.changed_files if f.opened_by_survey}
    s.checks["model_findings_anchored"] = all((f.path in changed or f.path in surveyed or f.tool != "model") and f.introduced_by for f in survivors)

    present = {f.category for f in survivors}
    s.checks["categories"] = all(c in present for c in exp.categories) if not live else True
    if not s.checks["categories"]:
        s.notes.append(f"missing categories {set(exp.categories) - present}")
    tools = {f.tool for f in survivors}
    s.checks["tools"] = all(t in tools for t in exp.tools)
    s.checks["downgraded"] = sum(1 for f in survivors if f.downgraded) == exp.downgraded if not live else True

    if record.verdict == "env_blocked":
        s.checks["one_review"] = reviews == []
        s.checks["hold_names"] = (exp.hold_names in (record.hold_reason or "")) if exp.hold_names else bool(record.hold_reason)
        if fixture.scanner_failure:
            s.checks["no_model_call_after_scanner_failure"] = model_calls == 0
    else:
        s.checks["one_review"] = len(reviews) == 1 and (live or reviews[0]["event"] == exp.review_event)
        if not s.checks["one_review"]:
            s.notes.append(f"reviews: {[r['event'] for r in reviews]}, expected one {exp.review_event}")
        if reviews:
            s.checks["inline_anchored"] = all(c["path"] in changed for c in reviews[0]["comments"])
    s.checks["write_free"] = bool((report.get("write_free_check") or {}).get("passed"))
    return s
