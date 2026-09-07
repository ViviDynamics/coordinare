"""Five fixture pull requests for the closer workflow eval (spec 172 SC-005).

Each fixture carries the review threads as GitHub reports them, the canned model
judgements, and the expectations scoring checks. Deterministic by construction;
live mode swaps the stub model for the gateway and keeps the fake GitHub, so no
real thread is ever fetched or resolved.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def comment(author: str, body: str, at: str = "2026-09-07T10:00:00Z") -> dict:
    return {"author": author, "body": body, "created_at": at}


def thread(tid: str, *comments: dict, resolved: bool = False, outdated: bool = False, path: str = "src/app.py", line: int = 10) -> dict:
    return {"id": tid, "path": path, "line": line, "resolved": resolved, "outdated": outdated, "comments": list(comments)}


ASK = comment("reviewer", "This needs a guard for the empty case: passing an empty list raises IndexError.")
REPLY = comment("implementer", "Added the guard in commit abc123; the function now returns early when the list is empty.", "2026-09-07T11:00:00Z")
THANKS = comment("implementer", "Thanks, noted!", "2026-09-07T12:00:00Z")  # a bare acknowledgement: answered by rule, but it addresses nothing


@dataclass(frozen=True)
class Expectation:
    verdict: str
    model_calls: int | None          # exact when stubbed; None when a real model may vary
    resolved: tuple[str, ...] = ()
    open_ids: tuple[str, ...] = ()
    live_verdicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fixture:
    name: str
    threads: list[dict]
    judgements: list[dict]
    expect: Expectation
    resolve_failures: tuple[str, ...] = field(default=())


CLEAN = Fixture(name="clean", threads=[thread("t1", ASK, resolved=True), thread("t2", ASK, resolved=True)], judgements=[],
                expect=Expectation(verdict="approved", model_calls=0, live_verdicts=("approved",)))
ANSWERED = Fixture(name="answered", threads=[thread("t1", ASK, REPLY)],
                   judgements=[{"thread_id": "t1", "addressed": True, "quote": "Added the guard in commit abc123", "reason": ""}],
                   expect=Expectation(verdict="approved", model_calls=1, resolved=("t1",), live_verdicts=("approved",)))
OPEN = Fixture(name="open", threads=[thread("t1", ASK)], judgements=[],
               expect=Expectation(verdict="changes_requested", model_calls=0, open_ids=("t1",), live_verdicts=("changes_requested",)))
OUTDATED = Fixture(name="outdated", threads=[thread("t1", ASK, outdated=True)], judgements=[],
                   expect=Expectation(verdict="approved", model_calls=0, resolved=("t1",), live_verdicts=("approved",)))
HALLUCINATED = Fixture(name="hallucinated_quote", threads=[thread("t1", ASK, THANKS)],
                       judgements=[{"thread_id": "t1", "addressed": True, "quote": "I fixed it in a follow-up branch", "reason": ""}],
                       expect=Expectation(verdict="changes_requested", model_calls=1, open_ids=("t1",), live_verdicts=("changes_requested",)))

FIXTURES: list[Fixture] = [CLEAN, ANSWERED, OPEN, OUTDATED, HALLUCINATED]
