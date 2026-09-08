"""Advocate eval fixtures (spec 173 SC-005)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fixture:
    name: str
    issues: list[dict]
    docs: dict[str, str]
    #: What the run must do, checked by scoring.py.
    expect_actions: dict[str, str]
    expect_calls_zero: bool = False
    expect_withheld: bool = False
    notes: str = ""
    stub_reply: dict | None = field(default=None)


README = {"README.md": "# Project\n\nRun `make start` to boot the app on port 8080."}


def _issue(n: int, title: str, body: str, labels: list[str] | None = None) -> dict:
    return {"id": f"I_{n}", "number": n, "title": title, "body": body,
            "url": f"https://github.com/o/r/issues/{n}", "labels": labels or []}


FIXTURES: list[Fixture] = [
    Fixture(
        name="answerable",
        issues=[_issue(1, "How do I start the app?", "I cannot find the command.")],
        docs=README,
        expect_actions={"I_1": "replied"},
        notes="the documentation covers it, so it is answered with a citation",
        stub_reply={"classifications": [{
            "issue_id": "I_1", "classification": "question", "confidence": 0.92,
            "reasoning": "the readme gives the command",
            "answer": "Based on `README.md`: run `make start`, which boots the app on port 8080.",
            "cited_documents": ["README.md"]}]},
    ),
    Fixture(
        name="unanswerable",
        issues=[_issue(2, "How do I configure SSO?", "Nothing in the docs covers this.")],
        docs=README,
        expect_actions={"I_2": "escalated"},
        notes="honest 'not documented' reaches a human instead of an invention",
        stub_reply={"classifications": [{
            "issue_id": "I_2", "classification": "question", "confidence": 0.8,
            "reasoning": "the documentation does not cover SSO",
            "answer": None, "cited_documents": []}]},
    ),
    Fixture(
        name="hallucinated_citation",
        issues=[_issue(3, "Where is the auth config?", "Point me at it.")],
        docs=README,
        expect_actions={"I_3": "escalated"},
        expect_withheld=True,
        notes="the answer cites a file the run never read, so it is withheld",
        stub_reply={"classifications": [{
            "issue_id": "I_3", "classification": "question", "confidence": 0.95,
            "reasoning": "auth lives in the config guide",
            "answer": "Based on `docs/auth.md`: set AUTH_MODE=sso.",
            "cited_documents": ["docs/auth.md"]}]},
    ),
    Fixture(
        name="sensitive_keyword",
        issues=[_issue(4, "Billing problem", "I was charged twice and want a refund.")],
        docs=README,
        expect_actions={"I_4": "escalated"},
        expect_calls_zero=True,
        notes="escalates before any model call, so it costs nothing and cannot be wrong",
    ),
    Fixture(
        name="already_handled",
        issues=[_issue(5, "How do I start?", "?", labels=["advocate-handled"])],
        docs=README,
        expect_actions={},
        expect_calls_zero=True,
        notes="the label carries the decision, so a restart cannot double-answer",
    ),
]
