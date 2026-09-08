"""Curator eval fixtures (spec 173 SC-005)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fixture:
    name: str
    issues: list[dict]
    on_board: set[str] = field(default_factory=set)
    expect_actions: dict[str, str] = field(default_factory=dict)
    expect_rejected: bool = False
    expect_calls_zero: bool = False
    notes: str = ""
    stub_reply: dict | None = None


def _issue(n: int, title: str, body: str, labels: list[str] | None = None) -> dict:
    return {"id": f"I_{n}", "number": n, "title": title, "body": body,
            "url": f"https://github.com/o/r/issues/{n}", "labels": labels or []}


READY = _issue(1, "Add a health endpoint",
               "Acceptance: GET /health returns 200 with a JSON body.")
VAGUE = _issue(2, "Make it better", "The app feels slow sometimes. Improve it.")

FIXTURES: list[Fixture] = [
    Fixture(
        name="qualifies",
        issues=[READY],
        expect_actions={"I_1": "added"},
        notes="a scoped issue with a testable criterion reaches the backlog",
        stub_reply={"judgements": [{
            "issue_id": "I_1", "qualifies": True,
            "reason": "it states a single testable acceptance criterion",
            "quote": "GET /health returns 200"}]},
    ),
    Fixture(
        name="does_not_qualify",
        issues=[VAGUE],
        expect_actions={"I_2": "skipped"},
        notes="a meta-request is left for a human",
        stub_reply={"judgements": [{
            "issue_id": "I_2", "qualifies": False,
            "reason": "no acceptance criteria and unbounded scope", "quote": ""}]},
    ),
    Fixture(
        name="unquotable_reason",
        issues=[READY],
        expect_actions={},
        expect_rejected=True,
        notes="a reason the issue does not support is rejected, not acted on",
        stub_reply={"judgements": [{
            "issue_id": "I_1", "qualifies": True,
            "reason": "the issue includes a rollout plan",
            "quote": "phased rollout across three regions"}]},
    ),
    Fixture(
        name="already_on_board",
        issues=[READY],
        on_board={"I_1"},
        expect_actions={},
        expect_calls_zero=True,
        notes="the board itself is the durable marker, so no duplicate is added",
    ),
]
