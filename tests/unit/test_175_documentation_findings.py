"""Integration contract for the single documenter and per-role findings."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from performer.models import Score
from performer.workspace import _documenter_tree

from coordinare.daemon import _persist_active_sessions
from coordinare.services import documenting_side as side
from coordinare.services.documentation_findings import (
    MAX_RECORD_CHARS,
    clean_findings,
    collect,
    inject,
    reset,
)
from coordinare.state_store import PersistedSession


def session():
    return {"card_id": "c1", "performer_stage": "implementing", "phase": "monitoring_performer",
            "workspace_branch": "coordinare/c1/work", "current_symphony": "example",
            "current_card": {"id": "c1", "title": "Feature", "issue_number": 1},
            "blueprint": {"blueprint_hash": "bp", "summary": "Explain behavior", "size": "large", "modules": ["src/app.py"],
                          "docs": [{"topic": "behavior", "location": "docs/wiki/behavior.md", "say": "Explain the API"}]}}


def test_412_empty_diff_verdicts_collect_their_reports():
    """412 round 4: nothing_to_review/nothing_to_scan/not_applicable advance
    through the terminal-success path, so their structured reports must
    collect like any other terminal instead of being silently discarded."""
    state = session()
    collect(state, "reviewing", "nothing_to_review",
            {"report": {"review": {"verdict": "nothing_to_review", "covered_files": ["a.py"],
                                   "advisory_findings": [{"category": "style", "body": "naming"}]}}, "head_sha": "abc"})
    collect(state, "security", "not_applicable",
            {"report": {"security": {"verdict": "not_applicable", "blocking": [], "advisory": []}}, "head_sha": "abc"})
    records = state["documentation_findings"]
    assert records["reviewing"]["findings"]["covered_files"] == ["a.py"]
    # 412 round 30: the advisory notes survive the projection (and ride the
    # findings hash) -- the documenter sees them as structured analysis.
    assert records["reviewing"]["findings"]["advisory_findings"] == [{"category": "style", "body": "naming"}]
    assert records["security"]["findings"]["verdict"] == "not_applicable"


def test_412_baseline_scanner_findings_survive_the_projection():
    """412 round 25: baseline findings are documented as reported and never
    dropped -- the coordinare's security projection persists them, not just
    the performer-side record."""
    state = session()
    collect(state, "security", "security_passed",
            {"report": {"security": {"verdict": "security_passed", "blocking": [], "advisory": [],
                                     "baseline_scanner_findings": [{"category": "secret", "problem": "hardcoded key"}]}},
             "head_sha": "abc"})
    findings = state["documentation_findings"]["security"]["findings"]
    assert findings["baseline_scanner_findings"] == [{"category": "secret", "problem": "hardcoded key"}]


def test_all_analysts_have_independent_bounded_persisted_inputs():
    state = session()
    samples = [("assessing", "assessment_complete", {"assessment": {"goal": "Goal", "ready": True}}),
               ("architecting", "plan_committed", {"blueprint": state["blueprint"]}),
               ("reviewing", "approved", {"review": {"verdict": "approved", "findings": [{"body": "review"}], "survey_commands": ["SECRET RAW OUTPUT"]}}),
               ("security", "security_passed", {"security": {"verdict": "security_passed", "blocking": [], "advisory": [{"body": "security"}]}}),
               ("qa", "qa_failed", {"passed": True, "criteria_checked": 1, "failures": [{"actual": "missing"}], "executed_checks": [{"output": "SECRET RAW OUTPUT"}]})]
    for role, marker, report in samples:
        collect(state, role, marker, {"report": report, "head_sha": "abc"})
    records = state["documentation_findings"]
    assert set(records) == {r for r, _, _ in samples}
    assert records["reviewing"]["findings"]["findings"][0]["body"] == "review"
    assert records["security"]["findings"]["advisory"][0]["body"] == "security"
    assert records["qa"]["findings"]["passed"] is False  # coordinare verdict beats self-report
    assert "SECRET RAW OUTPUT" not in json.dumps(records)
    persisted = _persist_active_sessions({"c1": state})["c1"]
    restored = PersistedSession.model_validate_json(persisted.model_dump_json())
    assert restored.documentation_findings == records
    assert PersistedSession(card_id="old").documentation_findings == {}
    reset(state, "reviewing")
    assert "reviewing" not in state["documentation_findings"]
    assert "security" in state["documentation_findings"]


def test_large_and_malformed_inputs_do_not_escape_the_carrier_budget():
    state = {}
    collect(state, "reviewing", "approved", {"report": {"review": {"verdict": "approved", "findings": [{"body": "x" * 50000}] * 1000}}})
    record = state["documentation_findings"]["reviewing"]
    assert len(json.dumps(record["findings"], ensure_ascii=True)) <= MAX_RECORD_CHARS
    assert record["findings"]["findings"]  # retain a prefix, not just the verdict
    assert record["truncated"]
    assert clean_findings({"reviewing": "bad", "unknown": {"findings": {}}}) == {}
    assert PersistedSession(card_id="c", documentation_findings="broken").documentation_findings == {}


def test_actual_score_accepts_side_payload_and_final_pointers_are_distinct():
    state = session()
    ctx = side.build_card_context(state["current_card"], state, persona="p", backend="codex", model_block={}, repo_url="https://example.com/org/repo.git", base_branch="main")
    score = Score.model_validate(ctx)
    assert score.doc_mode == "update" and score.documenting_side_run is True
    assert _documenter_tree(score) == "docs/"
    inject(ctx, state, "documenting")
    final = Score.model_validate(ctx)
    assert final.documenting_side_run is False and _documenter_tree(final) is None
    unrelated = {}
    inject(unrelated, state, "reviewing")
    assert unrelated == {}


def test_new_findings_schedule_an_update_but_never_a_second_writer():
    state = session()
    side.record_dispatched(state, blueprint_hash="bp", session_id="s")
    collect(state, "reviewing", "approved", {"report": {"review": {"verdict": "approved"}}})
    assert not side.should_dispatch(state)[0]
    side.record_result(state, status="done", head_sha="abc", reason=None, paths=["docs/wiki/a.md"])
    assert side.should_dispatch(state)[0]
    for stage in ("documenting", "closing", "closing_review"):
        assert not side.should_dispatch({**state, "performer_stage": stage})[0]
    ctx = {}
    inject(ctx, state, "implementing")
    assert ctx["completed_documentation"] == {"head_sha": "abc", "paths": ["docs/wiki/a.md"]}


@pytest.mark.asyncio
async def test_resume_and_completion_follow_canonical_replaced_session():
    state = session()
    side.record_dispatched(state, blueprint_hash="bp", session_id="restored", job_id="runner")
    sessions = {"c1": state}
    polling = set()
    tasks = []
    entered, release = asyncio.Event(), asyncio.Event()

    class Service:
        async def dispatch_card(self, *args, **kwargs):
            pytest.fail("restored running job must not be dispatched twice")

        async def check_status(self, sid):
            assert sid == "restored"
            entered.set()
            await release.wait()
            return {"status": "docs_committed", "report": {"docs": {"commit_sha": "newhead", "files_written": ["docs/wiki/a.md"]}}}

    async def resolve(*args):
        pytest.fail("resume does not resolve a new dispatch")

    def spawn(coro):
        tasks.append(asyncio.create_task(coro))

    kwargs = {"svc": Service(), "resolve": resolve, "spawn": spawn, "polling": polling, "get_session": sessions.get}
    await side.run_cycle(sessions, **kwargs)
    await entered.wait()
    sessions["c1"] = copy.deepcopy(state)
    await side.run_cycle(sessions, **kwargs)
    assert len(tasks) == 1
    release.set()
    await asyncio.gather(*tasks)
    assert sessions["c1"]["documenting_side"]["status"] == "done"
    assert state["documenting_side"]["status"] == "running"  # detached original was not updated
    saved = _persist_active_sessions(sessions)["c1"]
    assert saved.documenting_side.job_id == "runner"
    assert saved.documenting_side.head_sha == "newhead"
    assert saved.documenting_side.paths == ["docs/wiki/a.md"]
    assert not polling


@pytest.mark.asyncio
async def test_daemon_side_uses_symphony_workflow_and_real_score(monkeypatch):
    from coordinare.config import PerformerRoleConfig
    from tests.unit.test_daemon_documenting_side import _WM, _Cfg, _daemon, _GitHub

    role = PerformerRoleConfig(backend="codex", workflow="documenter", workflow_env={"DOC_PLAN_CAP": "3"})
    effective = _Cfg()
    effective.performers = SimpleNamespace(resolved_role=lambda name: role)
    effective.git_base_url = "https://git.example.com"
    effective.github_api_url = "https://git.example.com/api/v3"
    effective.github_graphql_url = "https://git.example.com/api/graphql"
    effective.base_branch = "trunk"
    effective.resolve_performer_orchestration = lambda name: {"strategy": "always"}
    d = _daemon({"config": _Cfg(), "symphony_configs": {"example": SimpleNamespace(effective_config=lambda cfg: effective)},
                 "symphony_workspace_managers": {"example": _WM()}})
    monkeypatch.setattr("coordinare.services.persona_service.get_effective_instructions", lambda *args: "docs")
    ctx, workspace = await d._resolve_documenting_side("c1", session(), {"example": _GitHub()})
    assert ctx["workflow"] == "documenter" and ctx["workflow_env"] == {"DOC_PLAN_CAP": "3"}
    assert ctx["orchestration"] == {"strategy": "always"}
    assert workspace.repo_url.startswith("https://git.example.com/")
    assert ctx["base_branch"] == "trunk" and ctx["github_api_url"].endswith("/api/v3")
    # The actual wire model validates the configured side-run fields. This catches
    # the old unsupported doc_mode=blueprint that dict-only tests overlooked.
    ctx.pop("orchestration")  # simplified resolver double above is not an orchestration fixture
    Score.model_validate(ctx)


@pytest.mark.asyncio
async def test_expired_writer_can_still_be_confirmed_stopped():
    state = session()
    side.record_dispatched(state, blueprint_hash="bp", session_id="early")
    side.record_result(state, status="failed", head_sha=None, reason="timeout", writer_active=True)
    async def check(_session_id):
        await asyncio.sleep(0.01)
        return {"status": "docs_committed", "head_sha": "dochead"}
    assert await side.poll_to_completion(state, check, session_id="early", timeout_s=0) == "done"
    assert state["documenting_side"]["writer_active"] is False


def test_implementer_workflow_turn_receives_completed_documentation():
    from performer.workflows.implementer.driver import _build_brief
    from performer.workflows.implementer.models import MilestonePlan
    ctx = SimpleNamespace(score=SimpleNamespace(completed_documentation={"head_sha": "dochead", "paths": ["docs/wiki/behavior.md"]}),
                          runner_kind="pytest", test_command="", investigation_note=None)
    plan = MilestonePlan(index=0, goal="Build feature", scope="src/app.py", done_when="Tests pass", lane="feature", lane_source="brief")
    brief = _build_brief(ctx, plan, kind="implement", persona_kind="IMPLEMENT")
    assert "dochead" in brief.persona
    assert "docs/wiki/behavior.md" in brief.persona
    assert "implementation brief remains authoritative" in brief.persona


@pytest.mark.asyncio
@pytest.mark.parametrize("record,phase", [({"status": "running"}, "dispatching"), ({"status": "failed", "writer_active": True}, "blocked")])
async def test_final_documenter_waits_for_confirmed_early_writer(record, phase, monkeypatch):
    import importlib
    from unittest.mock import AsyncMock
    module = importlib.import_module("coordinare.graph.nodes.dispatch_performer")
    body = AsyncMock()
    monkeypatch.setattr(module, "_dispatch_performer_body", body)
    state = session()
    state.update(performer_stage="documenting", phase="dispatching", documenting_side=record)
    result = await module.dispatch_performer(state)
    assert result["phase"] == phase
    body.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_record", [{"status": "running"}, {"status": "failed", "writer_active": True}])
async def test_fanout_does_not_overwrite_completed_recovery(old_record):
    from unittest.mock import AsyncMock

    from tests.unit.test_daemon_coverage import _make_daemon, _make_session
    daemon = _make_daemon()
    sess = _make_session("card-a")
    sess["documenting_side"] = {**old_record, "session_id": "early"}
    daemon._state["active_sessions"] = {"card-a": sess}
    daemon._state["board_snapshot"] = {"IN_PROGRESS": ["card-a"]}
    daemon._state["github_service"] = None
    async def invoke(state):
        side.record_result(daemon._state["active_sessions"]["card-a"], status="done", head_sha="dochead", reason=None)
        return state
    daemon._graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=invoke))
    await daemon._invoke_multi_session()
    assert daemon._state["active_sessions"]["card-a"]["documenting_side"]["head_sha"] == "dochead"
