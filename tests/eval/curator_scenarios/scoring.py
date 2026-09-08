"""Scoring for the curator eval (spec 173)."""
from __future__ import annotations

from dataclasses import dataclass, field

from tests.eval.curator_scenarios.fixtures import Fixture

#: Columns coordinare dispatches from. The curator must never target one.
DISPATCH_COLUMNS = {"TODO", "IN_PROGRESS", "IN_REVIEW"}


def _quote_for(record: dict, issue_id: str) -> str:
    """The passage a promotion was justified with, from the record itself."""
    for outcome in record.get("outcomes") or []:
        if outcome.get("issue_id") == issue_id:
            return str(outcome.get("quote") or "")
    return ""


@dataclass
class Score:
    name: str
    passed: bool
    notes: list[str] = field(default_factory=list)


def score_run(fixture: Fixture, record: dict, board, model_calls: int,
              *, live: bool = False, issues: list[dict] | None = None) -> Score:
    notes: list[str] = []

    actions = {o["issue_id"]: o["action"] for o in record.get("outcomes") or []}
    # Stubbed, the exact actions are pinned because the reply is fixed. Live,
    # they are the model's call: `unquotable_reason` only fabricates a quote
    # because the stub does, and a well-behaved model quotes the issue and is
    # rightly promoted. What must hold either way is the INVARIANT below --
    # nothing reaches the board on a quote the issue does not contain.
    if not live and actions != fixture.expect_actions:
        notes.append(f"actions {actions} != expected {fixture.expect_actions}")

    if live:
        text = " ".join(f"{i.get('title', '')} {i.get('body', '')}" for i in (issues or []))
        folded = " ".join(text.split()).lower()
        for outcome in record.get("outcomes") or []:
            if outcome["action"] != "added":
                continue
            quote = _quote_for(record, outcome["issue_id"])
            if quote and " ".join(quote.split()).lower() not in folded:
                notes.append(f"{outcome['issue_id']}: added on a quote the issue does not contain")

    if fixture.expect_calls_zero and model_calls:
        notes.append(f"expected no model call, made {model_calls}")

    if not live and fixture.expect_rejected and not record.get("rejected_judgements"):
        notes.append("expected the judgement to be rejected, it was not")

    for outcome in record.get("outcomes") or []:
        column = (outcome.get("column") or "").upper().replace(" ", "_")
        if column in DISPATCH_COLUMNS:
            notes.append(f"{outcome['issue_id']}: targeted the dispatch column {column}")

    if getattr(board, "moved", None):
        notes.append("the curator moved a card between columns")

    # a promoted issue must carry a quote a human can check
    for outcome in record.get("outcomes") or []:
        if outcome["action"] == "added" and not any(
            outcome["issue_id"] in str(c) or outcome["number"] == n
            for n, c in getattr(board, "comments", [])
        ):
            notes.append(f"{outcome['issue_id']}: added with no comment explaining why")

    return Score(name=fixture.name, passed=not notes, notes=notes)
