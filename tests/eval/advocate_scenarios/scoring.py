"""Scoring for the advocate eval (spec 173)."""
from __future__ import annotations

from dataclasses import dataclass, field

from tests.eval.advocate_scenarios.fixtures import Fixture


@dataclass
class Score:
    name: str
    passed: bool
    notes: list[str] = field(default_factory=list)


def score_run(fixture: Fixture, record: dict, gh, model_calls: int) -> Score:
    notes: list[str] = []

    actions = {o["issue_id"]: o["action"] for o in record.get("outcomes") or []}
    if actions != fixture.expect_actions:
        notes.append(f"actions {actions} != expected {fixture.expect_actions}")

    if fixture.expect_calls_zero and model_calls:
        notes.append(f"expected no model call, made {model_calls}")

    if fixture.expect_withheld and not record.get("withheld"):
        notes.append("expected the answer to be withheld, nothing was")

    # every reply must cite a document the run actually read
    read = set(record.get("documents_read") or [])
    for outcome in record.get("outcomes") or []:
        if outcome["action"] != "replied":
            continue
        cited = set(outcome.get("cited_documents") or [])
        if not cited:
            notes.append(f"{outcome['issue_id']}: replied citing nothing")
        elif not cited <= read:
            notes.append(f"{outcome['issue_id']}: cited {cited - read} which was never read")

    # nothing ungrounded may reach the issue
    if fixture.expect_withheld:
        for _number, body in getattr(gh, "comments", []):
            if "docs/auth.md" in body:
                notes.append("the withheld answer still reached the issue")

    return Score(name=fixture.name, passed=not notes, notes=notes)
