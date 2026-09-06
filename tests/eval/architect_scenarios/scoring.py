"""Qualitative scoring for one architect run against a fixture's expectations.

The signals are the ones that matter to the feature: sized right, documenter
decision right, criteria present, nothing written, refusals actually refused,
and the readers' slices disjoint. Not a CI gate in live mode (Constitution II);
the stubbed mode is exact and does run in CI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from performer.workflows.architect.allowlist import is_allowed

from coordinare.graph.nodes.dispatch_performer import documenter_side_run_wanted, inject_briefs
from tests.eval.architect_scenarios.fixtures import Fixture


@dataclass
class Score:
    fixture: str
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def score_run(fixture: Fixture, report: dict, ran_commands: list[str], *, live: bool = False) -> Score:
    bp = report["blueprint"]
    exp = fixture.expect
    s = Score(fixture=fixture.name)
    s.checks["size"] = report.get("size") == exp.size
    n_ms = len(bp.get("milestones") or [])
    s.checks["milestone_count"] = exp.min_milestones <= n_ms <= exp.max_milestones
    s.checks["data_model"] = len((bp.get("data_model") or {}).get("changes") or []) >= exp.min_data_model_changes
    n_docs = len(bp.get("docs") or [])
    s.checks["docs"] = {"none": n_docs == 0, "one": n_docs == 1, "some": n_docs >= 1}[exp.docs]
    s.checks["criteria"] = len(bp.get("criteria") or []) >= exp.min_criteria
    wf = report.get("write_free_check") or {}
    s.checks["write_free"] = bool(wf.get("passed"))
    refused_expected = sum(1 for c in fixture.survey_commands if any(w in c for w in ("install", "migrate", "rm ", "curl")))
    # Stubbed: the planted forbidden commands were counted as refused. Live: the
    # model proposes its own, so only the second half applies. Either way,
    # every command that actually ran must pass the allow-list itself (SC-003);
    # a word match on "migrate" flagged `ls db/migrate` in a live round.
    s.checks["refusals_refused"] = (live or wf.get("refused_commands", 0) >= refused_expected) and all(
        is_allowed(c)[0] for c in ran_commands
    )
    # slices: derive the three briefs as dispatch would and check disjointness
    ctx_i, ctx_d, ctx_q = {}, {}, {}
    inject_briefs(ctx_i, {"blueprint": bp}, role="implementing")
    inject_briefs(ctx_d, {"blueprint": bp}, role="documenting")
    inject_briefs(ctx_q, {"blueprint": bp}, role="qa")
    s.checks["slices_disjoint"] = (
        "docs" not in ctx_i.get("implementation_brief", {})
        and "milestones" not in ctx_d.get("documentation_brief", {})
        and set(ctx_q.get("verification_brief", {})) <= {"summary", "criteria"}
    )
    s.checks["documenter_decision"] = documenter_side_run_wanted(bp) == (exp.docs != "none")
    s.checks["single_turn_flag"] = (ctx_i.get("implementer_single_turn") is True) == (exp.size == "small")
    for k, ok in s.checks.items():
        if not ok:
            s.notes.append(f"{k} failed: size={report.get('size')} milestones={n_ms} docs={n_docs}")
    return s
