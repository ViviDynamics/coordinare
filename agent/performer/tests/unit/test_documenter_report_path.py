"""Spec 171 FR-013, FR-014: main.py maps the documenter workflow's record onto
docs_committed with the files it wrote (or env_blocked for a hold) and skips the
prose plan-then-write path. Without the report key, or without a known verdict,
the prose path is untouched."""
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


def _perf(doc_mode: str = "update") -> Performance:
    stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
    stand.git_env = {}
    score = Score(title="Test card", repo_url="https://github.com/acme/repo", branch="feat/test", issue_number=7, doc_mode=doc_mode)
    return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="documenting")


def _msg() -> PerformerMessage:
    return PerformerMessage(action="status", session_id="sid", payload={})


def _report(verdict="docs_committed", written=None, retired=None, pointers=None, **over):
    docs = {"mode": over.pop("mode", "update"), "brief_present": True, "changed_files": ["src/a.py"], "diff_truncated": False, "inventory_size": 3,
            "plan": [{"path": "docs/wiki/a.md", "kind": "reference", "source": "brief", "justification": "brief: a", "exists": False}],
            "deferred": [], "refused_paths": [], "evidence": {}, "results": [{"path": "docs/wiki/a.md", "action": "write", "dropped": False}],
            "files_written": written if written is not None else ["docs/wiki/a.md", "docs/wiki/README.md"], "files_retired": retired or [],
            "readme_generated": True, "pointers_refreshed": pointers or [], "commit_sha": over.pop("commit_sha", "abc123"),
            "verdict": verdict, "hold_reason": over.pop("hold_reason", None), "workflow_metrics": {}}
    return {"docs": docs, "write_free_check": {"passed": True}, "workflow_metrics": {"step_durations_ms": {"intake": 1}}}


async def _handle(output: str, doc_mode: str = "update"):
    perf = _perf(doc_mode)
    perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)
    with (
        patch("performer.main.commit_files", new=AsyncMock(return_value=["docs/x.md"])) as commit,
        patch("performer.main.create_pull_request", new=AsyncMock(return_value=("https://pr", "node"))) as create_pr,
    ):
        resp = await handle_status(_msg(), perf, settings)
    return perf, resp, commit, create_pr


@pytest.mark.asyncio
async def test_docs_committed_carries_the_written_files_and_skips_the_prose_commit():
    perf, resp, commit, create_pr = await _handle(json.dumps(_report(retired=["docs/wiki/old.md"], pointers=["AGENTS.md"])))
    assert resp.status == "docs_committed" and perf.state == "docs_committed"
    assert resp.files_modified == ["docs/wiki/a.md", "docs/wiki/README.md", "docs/wiki/old.md", "AGENTS.md"]
    assert resp.report["docs"]["commit_sha"] == "abc123"
    assert not commit.called, "the workflow already committed through commit_files"
    assert not create_pr.called, "update mode opens no PR"


@pytest.mark.asyncio
async def test_a_no_change_run_reports_docs_committed_with_no_files():
    perf, resp, _, _ = await _handle(json.dumps(_report(written=[], commit_sha=None)))
    assert resp.status == "docs_committed" and resp.files_modified == []


@pytest.mark.asyncio
async def test_init_mode_opens_the_seed_pr_as_the_prose_path_does():
    perf, resp, _, create_pr = await _handle(json.dumps(_report(mode="init")), doc_mode="init")
    assert resp.status == "docs_committed" and create_pr.called and resp.pr_url == "https://pr" and perf.pr_node_id == "node"


@pytest.mark.asyncio
async def test_a_hold_is_env_blocked_with_the_reason():
    perf, resp, _, _ = await _handle(json.dumps(_report("env_blocked", written=[], hold_reason="the tree was dirty at start: src/a.py")))
    assert resp.status == "env_blocked" and perf.state == "env_blocked" and "dirty" in resp.reason


@pytest.mark.asyncio
async def test_a_docs_dict_without_a_known_verdict_takes_the_prose_path():
    """A prose model that emits a `docs` key is not a workflow report."""
    perf, resp, commit, _ = await _handle(json.dumps({"docs": {"notes": "n/a"}, "files": [{"path": "docs/wiki/x.md", "content": "# X\n"}]}))
    assert resp.status == "docs_committed" and commit.called, "the legacy single-shot manifest commits through the prose path"


@pytest.mark.asyncio
async def test_output_without_the_report_key_takes_the_prose_path():
    perf, resp, commit, _ = await _handle(json.dumps({"files": [{"path": "docs/wiki/x.md", "content": "# X\n"}]}))
    assert resp.status == "docs_committed" and commit.called


@pytest.mark.asyncio
async def test_an_init_pr_open_failure_is_named_in_the_response():
    perf = _perf("init")
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps(_report(mode="init")))
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)
    with (
        patch("performer.main.commit_files", new=AsyncMock()),
        patch("performer.main.create_pull_request", new=AsyncMock(side_effect=RuntimeError("422"))),
    ):
        resp = await handle_status(_msg(), perf, settings)
    assert resp.status == "docs_committed" and resp.pr_url is None and "PR open failed" in (resp.reason or "") and "422" in resp.reason
