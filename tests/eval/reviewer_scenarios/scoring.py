"""Scoring for one reviewer run against a fixture's expectations (spec 169 SC-001).

The signals that matter to the feature: the verdict is derived by code, every
surviving finding is anchored, the rule findings appear when their trigger is
present, exactly one review is recorded with the right event, the coverage
pass ran when it had to, the tree stayed clean, and the record is the shape
coordinare lifts. Live mode relaxes the verdict to the fixture's accepted set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from performer.workflows.reviewer.models import ReviewRecord

from tests.eval.reviewer_scenarios.fixtures import Fixture


@dataclass
class Score:
    fixture: str
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def score_run(fixture: Fixture, report: dict, reviews: list[dict], *, live: bool = False) -> Score:
    exp = fixture.expect
    s = Score(fixture=fixture.name)
    try:
        record = ReviewRecord.model_validate(report.get("review") or {})
    except Exception as exc:
        s.checks["record_valid"] = False
        s.notes.append(f"review record invalid: {exc}")
        return s
    s.checks["record_valid"] = True

    accepted = set(exp.live_verdicts) if live and exp.live_verdicts else {exp.verdict}
    s.checks["verdict"] = record.verdict in accepted
    if not s.checks["verdict"]:
        s.notes.append(f"verdict {record.verdict}, expected {sorted(accepted)}")

    n = len(record.findings)
    s.checks["finding_count"] = (exp.min_findings <= n <= exp.max_findings) if not live else n <= 30
    if not s.checks["finding_count"]:
        s.notes.append(f"{n} findings, expected {exp.min_findings}..{exp.max_findings}")

    changed = {f.path for f in record.changed_files}
    s.checks["findings_anchored"] = all((f.path in changed) or (f.origin == "rule" and f.path == "") for f in record.findings)
    s.checks["verdict_matches_findings"] = (record.verdict == "changes_requested") == (n > 0) or record.verdict == "env_blocked"

    present = {f.category for f in record.findings}
    s.checks["rule_categories"] = all(c in present for c in exp.categories)
    if not s.checks["rule_categories"]:
        s.notes.append(f"missing categories {set(exp.categories) - present}")

    s.checks["dropped"] = len(record.findings_dropped) >= exp.min_dropped if not live else True

    s.checks["coverage_pass"] = record.coverage_pass_ran == exp.coverage_pass
    if not s.checks["coverage_pass"]:
        s.notes.append(f"coverage_pass_ran={record.coverage_pass_ran}, expected {exp.coverage_pass}")

    if record.verdict == "env_blocked":
        s.checks["one_review"] = reviews == []
    else:
        s.checks["one_review"] = len(reviews) == 1 and (live or reviews[0]["event"] == exp.review_event)
        if not s.checks["one_review"]:
            s.notes.append(f"reviews posted: {[r['event'] for r in reviews]}, expected one {exp.review_event}")
        if reviews and record.verdict == "changes_requested":
            s.checks["inline_anchored"] = all(c["path"] in changed for c in reviews[0]["comments"])

    s.checks["write_free"] = bool((report.get("write_free_check") or {}).get("passed"))
    return s
