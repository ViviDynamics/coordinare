"""Deterministic stand-ins for the security eval: a model answering the fixture's
replies in call order, a fake scanner runner returning the fixture's tool output
(or a missing binary), a survey command runner, and a recording GitHub poster."""
from __future__ import annotations

import json
from pathlib import Path

from performer.workflows.budget import ModelReply

from tests.eval.security_scenarios.fixtures import Fixture


def _category_a_reader_would_give(record: dict) -> str:
    """What a model reading this scanner record would call it.

    366 deleted coordinare's CWE table and keyword list, so naming the kind of
    issue is the reader's job. The stub has to do that job for the fixtures it
    is given, and it does it by reading the record's own words rather than
    being handed the answer -- which is what keeps the eval honest about where
    the category comes from. Severity is deliberately absent: the reader says
    what kind of issue it is, and coordinare's category table says how bad that
    kind is.
    """
    blob = json.dumps(record).lower()
    if "cwe-798" in blob or "secret" in blob or "hardcoded" in blob or "key" in blob:
        return "hardcoded_secret"
    if "cwe-89" in blob or "sqli" in blob or "injection" in blob or "sql" in blob:
        return "injection"
    if "hashlib" in blob or "md5" in blob or "b324" in blob or "crypto" in blob:
        return "weak_crypto"
    return "other_insecure_pattern"


def answer_tooling_and_reading(persona: str, content: list[dict]) -> ModelReply | None:
    """Answer the two model calls 366 added, or None if this is another step.

    Every harness that drives the security workflow scripts its replies in step
    order, so the two new calls have to be answered off that list or the
    ordinal drifts and a later step receives the wrong reply. There are four
    such harnesses (the unit end-to-end fixture, the adapter-seam test, this
    eval stub, and the coordinare-side runner), and in #365 the identical
    situation produced three copies of one assertion that CI found one at a
    time, each after the previous fix reported green. So the answer lives here
    once.
    """
    if "opening an unfamiliar repository" in persona:
        return ModelReply(
            content=json.dumps({
                "tools": [
                    {"name": "semgrep", "argv": ["semgrep", "--json", "."], "why": "sources present"},
                    {"name": "bandit", "argv": ["bandit", "-f", "json", "-r", "."], "why": "sources present"},
                ],
                "nothing_applies": "",
            }),
            finish_reason="stop",
        )
    if "reading the raw output of a security scanner" in persona:
        text = "".join(c.get("text", "") for c in content)
        given = [ln[2:] for ln in text.splitlines() if ln.startswith("- ")]
        payload: dict = {}
        for line in text.splitlines():
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except ValueError:
                    payload = {}
        return ModelReply(
            content=json.dumps({
                "findings": [
                    {
                        "category": _category_a_reader_would_give(r),
                        "description": str(r.get("check_id", r.get("test_id", "rule"))),
                        "file": str(r.get("path", r.get("filename", ""))),
                        "line": int((r.get("start", {}) or {}).get("line", r.get("line_number", 0)) or 0),
                    }
                    for r in (payload.get("results", []) if isinstance(payload, dict) else [])
                ],
                "coverage": [{"path": g, "examined": True, "reason": ""} for g in given],
                "summary": "",
            }),
            finish_reason="stop",
        )
    return None


def stub_model_for(fixture: Fixture):
    """The fixture's scripted replies, plus the two calls 366 added.

    Tool selection and output reading are model calls now. Answering them from
    the persona rather than from the ordered reply list is what lets every
    fixture's `replies` keep lining up with the steps it was written for --
    counting them in the ordinal instead made the survey step receive the
    findings reply, which then failed schema validation. This is the third
    harness to need the same treatment, after the unit end-to-end fixture and
    the coordinare-side runner.
    """
    state = {"i": 0}

    async def model_call(persona: str, content: list[dict], max_tokens: int) -> ModelReply:
        answered = answer_tooling_and_reading(persona, content)
        if answered is not None:
            return answered
        reply = fixture.replies[min(state["i"], len(fixture.replies) - 1)]
        state["i"] += 1
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    model_call.state = state
    return model_call


def fake_scanner_for(fixture: Fixture):
    async def runner(argv: list[str], cwd, timeout_s: int):
        tool = Path(argv[0]).name
        if tool == fixture.scanner_failure:
            raise FileNotFoundError(tool)
        results = fixture.semgrep if tool == "semgrep" else fixture.bandit
        return (1 if results else 0), json.dumps({"results": results, "errors": []}), ""

    return runner


def stub_command_runner():
    async def runner(cmd: str, cwd, timeout_s: int):
        if cmd.startswith("git status"):
            return 0, ""
        return 0, "abc123 change\n"

    return runner


class RecordingPoster:
    """Records the review the workflow would post. Live mode uses this too: the eval never writes to GitHub."""

    def __init__(self) -> None:
        self.reviews: list[dict] = []

    async def __call__(self, owner, repo, number, *, event, body, comments, token) -> dict:
        self.reviews.append({"event": event, "body": body, "comments": comments, "number": number})
        return {"html_url": f"https://example.invalid/{owner}/{repo}/pull/{number}#review-{len(self.reviews)}"}
