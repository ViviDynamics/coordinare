"""Spec 170 FR-014: main.py maps the security workflow's record onto the
statuses coordinare routes today, with the blocking findings in the spec-022
shape, and skips the prose post-processing (committed report, advisory
comments). Without the report key the prose path is untouched."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends.base import BackendStatus
from performer.config import Settings
from performer.main import Performance, handle_status
from performer.models import Score, Stand
from performer.protocol import PerformerMessage


def _perf() -> Performance:
    stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
    stand.git_env = {}
    score = Score(title="Test card", repo_url="https://github.com/acme/repo", branch="feat/test", issue_number=7, pr_url="https://github.com/acme/repo/pull/12")
    perf = Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="security")
    perf.pr_url = "https://github.com/acme/repo/pull/12"
    return perf


def _msg() -> PerformerMessage:
    return PerformerMessage(action="status", session_id="sid", payload={})


def _finding(**over):
    base = {"path": "src/db.py", "line": 12, "category": "injection", "problem": "request param concatenated into SQL", "why_blocking": "attacker controls the query",
            "evidence": 'query = "SELECT * FROM users WHERE id = " + user_id', "origin": "model", "severity": "high", "routing": "implementer",
            "introduced_by": "src/db.py", "tool": "model", "downgraded": False, "downgrade_reason": ""}
    base.update(over)
    return base


def _report(verdict, blocking=None, advisory=None, **over):
    sec = {"changed_files": [{"path": "src/db.py", "hunks": [{"header": "@@ -1 +1,20 @@", "start_line": 1, "end_line": 20, "lines": []}], "fully_in_diff": True, "opened_by_survey": False}],
           "diff_truncated": False, "scan": [{"tool": "semgrep", "command": "semgrep --config auto --json src/db.py", "exit_code": 1, "finding_count": 1, "duration_ms": 900}],
           "unread_files": over.pop("unread_files", []), "survey_commands": [], "survey_refusals": [], "findings_before_gate": [], "findings_dropped": [], "findings_after_anchor_recheck": [],
           "scanner_findings": [], "blocking": blocking or [], "advisory": advisory or [], "coverage_pass_ran": False, "coverage_pass_output": "", "verdict": verdict,
           "hold_reason": over.pop("hold_reason", None), "covered_files": ["src/db.py"], "post_error": over.pop("post_error", None), "posted_review_url": over.pop("posted_review_url", "https://github.com/acme/repo/pull/12#r1"), "workflow_metrics": {}}
    return {"security": sec, "write_free_check": {"passed": True}, "workflow_metrics": {"step_durations_ms": {"intake": 1}}}


async def _handle(output: str, cycle: int = 0):
    perf = _perf()
    perf.security_cycle = cycle
    perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)
    with (
        patch("performer.main.commit_file", new=AsyncMock()) as commit,
        patch("performer.main.post_pr_comment", new=AsyncMock()) as comment,
        patch("performer.main.list_pr_comments", new=AsyncMock(return_value=[])),
    ):
        resp = await handle_status(_msg(), perf, settings)
    return perf, resp, commit, comment


@pytest.mark.asyncio
async def test_security_failed_carries_blocking_findings_in_the_022_shape():
    perf, resp, commit, comment = await _handle(json.dumps(_report("security_failed", blocking=[_finding()], advisory=[_finding(category="weak_crypto", severity="medium", line=30)])))
    assert resp.status == "security_failed" and perf.state == "security_failed"
    assert resp.findings == [{
        "severity": "high", "category": "injection",
        "description": 'injection: request param concatenated into SQL Why blocking: attacker controls the query Evidence: query = "SELECT * FROM users WHERE id = " + user_id',
        "file": "src/db.py", "line": 12, "routing": "implementer",
    }], "advisories do not ride in findings; the record carries them"
    assert resp.report["security"]["verdict"] == "security_failed" and perf.security_cycle == 1
    assert not commit.called and not comment.called, "no committed report, no advisory comments under the workflow"


@pytest.mark.asyncio
async def test_security_passed_reports_passed_and_commits_nothing():
    perf, resp, commit, comment = await _handle(json.dumps(_report("security_passed", advisory=[_finding(category="weak_crypto", severity="medium")])))
    assert resp.status == "security_passed" and perf.state == "security_passed"
    assert resp.report["security"]["advisory"][0]["category"] == "weak_crypto"
    assert not commit.called and not comment.called


@pytest.mark.asyncio
async def test_the_cycle_limit_still_blocks():
    perf, resp, _, _ = await _handle(json.dumps(_report("security_failed", blocking=[_finding()])), cycle=2)
    assert resp.status == "blocked" and perf.state == "blocked" and "blocking finding" in resp.questions[0]


@pytest.mark.asyncio
async def test_a_hold_names_the_tool_or_the_unread_files():
    perf, resp, _, _ = await _handle(json.dumps(_report("env_blocked", hold_reason="semgrep: binary not found", posted_review_url=None)))
    assert resp.status == "env_blocked" and perf.state == "env_blocked" and "semgrep" in resp.reason
    _, resp2, _, _ = await _handle(json.dumps(_report("env_blocked", unread_files=["src/z.py"], posted_review_url=None)))
    assert resp2.status == "env_blocked" and "src/z.py" in resp2.reason


@pytest.mark.asyncio
async def test_output_without_the_report_key_takes_the_prose_path():
    perf, resp, commit, comment = await _handle(json.dumps({"passed": False, "findings": [{"severity": "high", "category": "injection", "description": "d", "file": "src/db.py", "line": 3, "routing": "implementer"}]}))
    assert resp.status == "security_failed"
    assert commit.called, "the prose path commits the security report as before"
    assert resp.findings[0]["category"] == "injection"


@pytest.mark.asyncio
async def test_a_security_dict_without_a_known_verdict_takes_the_prose_path():
    """Review finding: a prose model that happens to emit a `security` key must not fall into the workflow hold."""
    perf, resp, commit, _ = await _handle(json.dumps({"security": {"notes": "looks fine"}, "passed": True, "findings": []}))
    assert resp.status == "security_passed" and perf.state == "security_passed"
    assert commit.called, "the prose path ran"
