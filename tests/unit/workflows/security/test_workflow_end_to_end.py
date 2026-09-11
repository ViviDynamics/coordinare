"""170 end-to-end scenarios against a stubbed model, a fake scanner runner and a
recording GitHub poster: clean, injection, scanner-only secret, dedup, hallucinated
anchor with re-anchor, surveyed unchanged sink in the body, introduced_by not
changed, downgrade recorded, scanner unavailable holds before any model call,
coverage hold, post failure hold, dirty tree, step events."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.security import STATES, SecurityWorkflow
from performer.workflows.security.models import SecurityRecord
from performer.workflows.toolkit import Toolkit

from tests.unit.workflows.security._fixtures import (
    DIFF,
    INJECTION,
    NO_FINDINGS,
    OPEN_EXTRA,
    OPEN_SINK,
    SURVEY,
    TRUNCATED_DIFF,
)

SEMGREP_KEY = {"check_id": "generic.secrets.hardcoded-key", "path": "src/db.py", "start": {"line": 6}, "extra": {"severity": "ERROR", "metadata": {"cwe": ["CWE-798: Use of Hard-coded Credentials"]}}}
SEMGREP_SQLI = {"check_id": "python.flask.sqli", "path": "src/db.py", "start": {"line": 7}, "extra": {"severity": "WARNING", "metadata": {"cwe": ["CWE-89: SQL Injection"]}}}
BANDIT_MD5 = {"test_id": "B324", "test_name": "hashlib", "filename": "src/db.py", "line_number": 8, "issue_severity": "MEDIUM", "issue_confidence": "LOW"}  # MEDIUM/LOW normalises to medium


def _category_a_reader_would_give(record: dict) -> str:
    """What a model reading this scanner record would call it.

    The stand-in for a judgement, not a reimplementation of one: #366 deleted
    coordinare's CWE table and keyword list precisely so that naming the kind of
    issue is the reader's job. The fake has to do that job for the fixtures it
    is given, and the fact that it reads the record's own words -- rather than
    being handed the answer -- is what keeps these tests honest about where the
    category comes from.

    Severity is deliberately absent. The reader says what kind of issue it is;
    coordinare's category table says how bad that kind is, for scanner and model
    findings alike, so a reading cannot call a SQL injection low.
    """
    blob = json.dumps(record).lower()
    if "cwe-798" in blob or "secret" in blob or "hardcoded" in blob:
        return "hardcoded_secret"
    if "cwe-89" in blob or "sqli" in blob or "injection" in blob:
        return "injection"
    if "hashlib" in blob or "md5" in blob or "b324" in blob:
        return "weak_crypto"
    return "other_insecure_pattern"


class FakeGitHub:
    def __init__(self, fail=False):
        self.reviews, self.fail = [], fail

    async def post(self, owner, repo, number, *, event, body, comments, token):
        if self.fail:
            raise RuntimeError("502 Bad Gateway")
        self.reviews.append({"number": number, "event": event, "body": body, "comments": comments})
        return {"html_url": f"https://github.com/{owner}/{repo}/pull/{number}#pullrequestreview-{len(self.reviews)}"}


def fake_scanner(semgrep=None, bandit=None, *, missing=None, timeout=None, garbage=None, empty=None):
    calls = []

    async def runner(argv, cwd, timeout_s):
        tool = Path(argv[0]).name
        calls.append(tool)
        if tool == missing:
            raise FileNotFoundError(tool)
        if tool == timeout:
            raise TimeoutError()
        if tool == garbage:
            return 1, "not json at all", "boom"  # exit 1 is a normal code; the body is what is wrong
        if tool == empty:
            return 1, "", "crashed"
        results = (semgrep if tool == "semgrep" else bandit) or []
        return (1 if results else 0), json.dumps({"results": results, "errors": []}), "progress noise on stderr"

    runner.calls = calls
    return runner


def _toolkit(replies):
    state = {"i": 0, "commands": []}

    async def model_call(persona, content, max_tokens):
        # 366: tool selection and output reading are model calls now. Answer
        # them from the persona rather than from the ordered reply list, so
        # every existing test's `replies` still lines up with the steps it was
        # written for.
        if "opening an unfamiliar repository" in persona:
            state["planned"] = state.get("planned", 0) + 1
            return ModelReply(
                content=json.dumps({
                    "tools": [
                        {"name": "semgrep", "argv": ["semgrep", "--json", "."], "why": "python sources"},
                        {"name": "bandit", "argv": ["bandit", "-f", "json", "-r", "."], "why": "python sources"},
                    ],
                    "nothing_applies": "",
                }),
                finish_reason="stop",
            )
        if "reading the raw output of a security scanner" in persona:
            text = "".join(c.get("text", "") for c in content)
            tool_name = next((ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("Tool:")), "scanner")
            given = [ln[2:] for ln in text.splitlines() if ln.startswith("- ")]
            payload = {}
            for line in text.splitlines():
                if line.startswith("{"):
                    try:
                        payload = json.loads(line)
                    except Exception:
                        payload = {}
            return ModelReply(
                content=json.dumps({
                    "findings": [
                        {
                            "category": _category_a_reader_would_give(r),
                            "description": f"{tool_name}:{r.get('check_id', r.get('test_id', 'rule'))}",
                            "file": str(r.get("path", r.get("filename", ""))),
                            "line": int((r.get("start", {}) or {}).get("line", r.get("line_number", 0)) or 0),
                        }
                        for r in (payload.get("results", []) if isinstance(payload, dict) else [])
                    ],
                    # the fake scanners read everything they are handed; the
                    # abstention path is covered directly in test_scanner.py
                    "coverage": [{"path": g, "examined": True, "reason": ""} for g in given],
                    "summary": "",
                }),
                finish_reason="stop",
            )
        reply = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    async def runner(cmd, cwd, timeout_s):
        state["commands"].append(cmd)
        if cmd.startswith("git status"):
            return 0, ""
        if "src/sink.py" in cmd:
            return 0, "def run(sql):\n    return db.execute(sql)\n"
        if "src/extra.py" in cmd:
            return 0, "import os\nX = 1\n"
        return 0, "abc123 add lookup\n"

    events = []
    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=runner, event_sink=events.append, call_limit=12)
    return tk, events, state


def _score(**over):
    base = dict(pr_diff=DIFF, implementation_brief={"work_kind": "feature"}, title="Add lookup", description="Look a user up by id",
                pr_url="https://github.com/o/r/pull/7", owner_repo=("o", "r"), effective_github_token="tok", backend="codex", model="m", workflow_env={})
    base.update(over)
    return SimpleNamespace(**base)


async def _run(replies, score, *, gh=None, scanner=None):
    gh = gh or FakeGitHub()
    scanner = scanner or fake_scanner()
    tk, events, state = _toolkit(replies)
    result = await SecurityWorkflow(poster=gh.post, scan_runner=scanner).run(SimpleNamespace(path=Path("/tmp/x")), score, tk)
    record = SecurityRecord.model_validate(result.report["security"])
    return result, record, gh, events, state


@pytest.mark.asyncio
async def test_clean_change_passes_with_one_comment_review():
    result, record, gh, events, state = await _run([SURVEY, NO_FINDINGS], _score())
    assert record.verdict == "security_passed" and record.blocking == [] and record.advisory == []
    assert [r["event"] for r in gh.reviews] == ["COMMENT"] and gh.reviews[0]["comments"] == []
    assert [r.tool for r in record.scan] == ["semgrep", "bandit"] and all(r.exit_code == 0 for r in record.scan)
    assert result.report["write_free_check"]["passed"] is True and state["commands"][-1].startswith("git status --porcelain")
    steps = [e.text for e in events if e.text.startswith("security.")]
    assert steps == ["security.intake", "security.tooling", "security.scan", "security.survey", "security.findings", "security.gate", "security.post", "security.report"]
    assert set(STATES) >= {s.split(".", 1)[1] for s in steps}
    # Spec 366: planning (1) + reading per tool (2) + survey (1) + findings (1) = 5 model calls
    assert result.report["workflow_metrics"]["model_calls"] == 5 and result.report["workflow_metrics"]["commands_run"] >= 3


@pytest.mark.asyncio
async def test_unparseable_output_is_read_rather_than_held():
    """366: coordinare no longer has an opinion about a scanner's output format.

    Spec 170 treated output it could not parse as a broken tool, because it had
    one hand-written normalizer per tool and anything else was unreadable. The
    model reads whatever the scanner printed, so a table, a log or a sentence is
    as readable as JSON -- and a tool coordinare has never heard of needs no code
    change. What still holds the card is a tool that examined nothing, which is
    a fact about coverage rather than about syntax.
    """
    _, record, _, _, _ = await _run([SURVEY, {"findings": [INJECTION]}], _score(), scanner=fake_scanner(garbage="semgrep"))
    assert record.verdict != "env_blocked", "unparseable output is not a broken scanner"
    assert [r.tool for r in record.scan] == ["semgrep", "bandit"], "both tools still ran and were read"

@pytest.mark.asyncio
async def test_an_injection_is_blocking_inline_and_routed_to_the_implementer():
    result, record, gh, _, _ = await _run([SURVEY, {"findings": [INJECTION]}], _score())
    assert record.verdict == "security_failed"
    f = record.blocking[0]
    assert (f.severity, f.routing, f.tool, f.origin, f.line) == ("high", "implementer", "model", "model", 7)
    assert gh.reviews[0]["event"] == "REQUEST_CHANGES" and [(c["path"], c["line"]) for c in gh.reviews[0]["comments"]] == [("src/db.py", 7)]
    assert "Route: implementer" in gh.reviews[0]["comments"][0]["body"]
    assert result.findings[0]["category"] == "injection"


@pytest.mark.asyncio
async def test_a_scanner_secret_blocks_when_the_model_reports_nothing():
    _, record, gh, _, _ = await _run([SURVEY, NO_FINDINGS], _score(), scanner=fake_scanner(semgrep=[SEMGREP_KEY]))
    assert record.verdict == "security_failed"
    f = record.blocking[0]
    assert (f.tool, f.origin, f.severity, f.category, f.line, f.evidence) == ("semgrep", "rule", "critical", "hardcoded_secret", 6, "")
    assert record.scan[0].finding_count == 1 and record.scan[0].exit_code == 1
    assert gh.reviews[0]["event"] == "REQUEST_CHANGES" and "Reported by semgrep" in gh.reviews[0]["comments"][0]["body"]


@pytest.mark.asyncio
async def test_a_model_and_a_scanner_finding_at_the_same_anchor_collapse_to_the_tool():
    _, record, _, _, _ = await _run([SURVEY, {"findings": [INJECTION]}], _score(), scanner=fake_scanner(semgrep=[SEMGREP_SQLI]))
    assert len(record.blocking) == 1 and record.blocking[0].tool == "semgrep" and record.blocking[0].severity == "high"
    assert len(record.findings_before_gate) == 1, "the model finding was still recorded before the merge"


@pytest.mark.asyncio
async def test_a_hallucinated_anchor_is_dropped_then_reanchored_once():
    bad = {**INJECTION, "line": 99, "evidence": "cursor.execute(query)"}
    _, record, _, _, state = await _run([SURVEY, {"findings": [bad]}, {"findings": [INJECTION]}], _score())
    assert [f.line for f in record.findings_dropped] == [99] and [f.line for f in record.findings_after_anchor_recheck] == [7]
    assert record.verdict == "security_failed" and state["i"] == 3


@pytest.mark.asyncio
async def test_introduced_by_must_be_a_changed_file():
    foreign = {**INJECTION, "introduced_by": "src/other.py"}
    _, record, _, _, _ = await _run([SURVEY, {"findings": [foreign]}, {"findings": [foreign]}], _score())
    assert len(record.findings_dropped) == 1 and record.blocking == [] and record.verdict == "security_passed"


@pytest.mark.asyncio
async def test_a_sink_in_a_surveyed_unchanged_file_anchors_in_the_body():
    sink = {"path": "src/sink.py", "line": 2, "category": "injection", "problem": "raw SQL executed", "why_blocking": "tainted from lookup",
            "evidence": "return db.execute(sql)", "introduced_by": "src/db.py", "downgrade_reason": ""}
    _, record, gh, _, _ = await _run([OPEN_SINK, {"findings": [sink]}], _score())
    assert [f.path for f in record.blocking] == ["src/sink.py"]
    assert gh.reviews[0]["event"] == "REQUEST_CHANGES" and gh.reviews[0]["comments"] == [] and "`src/sink.py:2`" in gh.reviews[0]["body"]
    unopened = {**sink, "path": "src/never.py"}
    _, record2, _, _, _ = await _run([SURVEY, {"findings": [unopened]}, {"findings": [unopened]}], _score())
    assert record2.blocking == [] and len(record2.findings_dropped) == 1


@pytest.mark.asyncio
async def test_a_downgrade_reason_makes_a_blocking_category_advisory_visibly():
    down = {**INJECTION, "downgrade_reason": "user_id is cast to int two lines earlier in the same function"}
    _, record, gh, _, _ = await _run([SURVEY, {"findings": [down]}], _score())
    assert record.verdict == "security_passed" and record.blocking == []
    f = record.advisory[0]
    assert (f.downgraded, f.severity, f.category) == (True, "medium", "injection") and "cast to int" in f.downgrade_reason
    assert gh.reviews[0]["event"] == "COMMENT" and "Downgraded to advisory" in gh.reviews[0]["body"]


@pytest.mark.asyncio
async def test_a_downgrade_never_applies_to_a_scanner_finding():
    down = {**INJECTION, "downgrade_reason": "false positive"}
    _, record, _, _, _ = await _run([SURVEY, {"findings": [down]}], _score(), scanner=fake_scanner(semgrep=[SEMGREP_SQLI]))
    assert record.verdict == "security_failed" and record.blocking[0].tool == "semgrep" and record.blocking[0].downgraded is False


@pytest.mark.asyncio
async def test_advisories_pass_with_one_comment_listing_them():
    _, record, gh, _, _ = await _run([SURVEY, NO_FINDINGS], _score(), scanner=fake_scanner(bandit=[BANDIT_MD5]))
    assert record.verdict == "security_passed" and [f.tool for f in record.advisory] == ["bandit"] and record.advisory[0].category == "weak_crypto"
    assert gh.reviews[0]["event"] == "COMMENT" and "bandit" in gh.reviews[0]["body"] and "`src/db.py:8`" in gh.reviews[0]["body"]


# "garbage" is deliberately absent: under 366 a scanner printing something
# coordinare cannot parse is not a broken scanner, it is a scanner whose output
# the model reads. That case is asserted directly below, and the abstention it
# can still produce -- a reader that cannot tell whether any file was examined
# -- holds through the coverage floor in test_scanner.py.
@pytest.mark.parametrize("kind,tool,needle", [("missing", "semgrep", "binary not found"), ("timeout", "bandit", "timed out"), ("empty", "bandit", "no output")])
@pytest.mark.asyncio
async def test_a_broken_scanner_holds_after_planning_and_before_reading(kind, tool, needle):
    """Spec 366 reverses spec 170: the model now plans the tools before scanners run.

    A broken scanner holds the card in env_blocked after the planning step but
    before any attempt to read the output. The spec-170 assertion that scan runs
    before ANY model call is deliberately reversed here.
    """
    scanner = fake_scanner(**{kind: tool})
    _, record, gh, events, state = await _run([SURVEY, {"findings": [INJECTION]}], _score(), scanner=scanner)
    assert record.verdict == "env_blocked" and tool in record.hold_reason and needle in record.hold_reason
    assert state["i"] == 0, "no normal reply model calls consumed (planning and reading are separate calls)"
    assert gh.reviews == [] and record.blocking == []
    steps = [e.text for e in events if e.text.startswith("security.")]
    # Spec 366: tooling (planning), scan (failure), report. No survey/findings/gate/post because scan failed.
    assert steps == ["security.intake", "security.tooling", "security.scan", "security.report"]


@pytest.mark.asyncio
async def test_a_truncated_diff_runs_the_coverage_pass_and_unread_files_hold():
    _, record, _gh, _, _ = await _run([SURVEY, OPEN_EXTRA, NO_FINDINGS], _score(pr_diff=TRUNCATED_DIFF))
    assert record.diff_truncated and record.coverage_pass_ran and record.verdict == "security_passed" and "src/extra.py" in record.covered_files
    _, hold, gh2, _, _ = await _run([SURVEY, SURVEY, NO_FINDINGS], _score(pr_diff=TRUNCATED_DIFF))
    assert hold.verdict == "env_blocked" and hold.unread_files == ["src/extra.py"] and gh2.reviews == []
    _, still_fails, _, _, _ = await _run([SURVEY, SURVEY, {"findings": [INJECTION]}], _score(pr_diff=TRUNCATED_DIFF))
    assert still_fails.verdict == "security_failed", "a blocking finding outranks the coverage hold"


@pytest.mark.asyncio
async def test_a_failed_post_is_a_hold_with_the_error_recorded():
    _, record, _, _, _ = await _run([SURVEY, {"findings": [INJECTION]}], _score(), gh=FakeGitHub(fail=True))
    assert record.verdict == "env_blocked" and "502" in record.post_error and "502" in record.hold_reason and len(record.blocking) == 1


@pytest.mark.asyncio
async def test_a_dirty_tree_fails_the_round():
    from performer.workflows.security.report import SecurityWroteToTree

    calls = {"n": 0}

    async def model(persona, content, max_tokens):
        # 366: the two new persona-dispatched calls must NOT advance the
        # ordinal. This counter picks which scripted reply the survey and
        # findings steps get; counting the tooling and reading calls too made
        # the survey step receive the findings reply, which then failed schema
        # validation against SurveyProposal. The step order did not change --
        # the counter's meaning did.
        if "opening an unfamiliar repository" in persona:
            return ModelReply(
                content=json.dumps({
                    "tools": [
                        {"name": "semgrep", "argv": ["semgrep", "--json", "."], "why": "python sources"},
                        {"name": "bandit", "argv": ["bandit", "-f", "json", "-r", "."], "why": "python sources"},
                    ],
                    "nothing_applies": "",
                }),
                finish_reason="stop",
            )
        if "reading the raw output of a security scanner" in persona:
            return ModelReply(
                content=json.dumps({"findings": [], "coverage": [{"path": "src/db.py", "examined": True, "reason": ""}], "summary": ""}),
                finish_reason="stop",
            )
        calls["n"] += 1
        reply = SURVEY if calls["n"] == 1 else NO_FINDINGS
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    async def runner(cmd, cwd, timeout_s):
        return (0, " M src/db.py\n") if cmd.startswith("git status") else (0, "{}")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model, command_runner=runner, call_limit=12)
    with pytest.raises(SecurityWroteToTree):
        await SecurityWorkflow(poster=FakeGitHub().post, scan_runner=fake_scanner()).run(SimpleNamespace(path=Path("/tmp/x")), _score(), tk)


@pytest.mark.asyncio
async def test_every_step_is_timed_and_logged(monkeypatch):
    from performer.workflows import security as security_mod

    from tests.unit.workflows._fakelog import FakeLog

    fake = FakeLog()
    monkeypatch.setattr(security_mod, "log", fake)
    result, _, _, _, _ = await _run([SURVEY, {"findings": [INJECTION]}], _score())
    durations = result.metrics.step_durations_ms
    assert list(durations) == ["intake", "tooling", "scan", "survey", "findings", "gate", "post", "report"]
    assert result.report["workflow_metrics"]["step_durations_ms"] == durations
    events = {e["event"]: e for e in fake.entries}
    assert events["security.scan"]["findings"] == 0 and events["security.gate"]["verdict"] == "security_failed"


@pytest.mark.asyncio
async def test_a_second_tool_failure_keeps_the_first_tools_scan_result():
    """Review finding: the hold record shows semgrep ran before bandit went missing."""
    _, record, gh, _, state = await _run([SURVEY, NO_FINDINGS], _score(), scanner=fake_scanner(missing="bandit"))
    assert record.verdict == "env_blocked" and "bandit" in record.hold_reason
    assert [r.tool for r in record.scan] == ["semgrep"] and state["i"] == 0 and gh.reviews == []
