"""165 FR-015: the daemon resolves a documenter side run's dispatch from the
session (repo, branch, token, tech_writer persona and model) and runs the
cycle dispatcher without ever raising into the loop."""
from __future__ import annotations

import asyncio

import pytest

from coordinare.daemon import CoordinareDaemon


class _GitHub:
    org = "ViviDynamics"
    _project_name = "website"


class _WM:
    async def get_fresh_github_token(self):
        return "ghs_fresh"


class _Role:
    backend = "codex"
    effort = None
    temperature = None
    max_tokens = None


class _Performers:
    def resolved_role(self, role):
        assert role == "tech_writer"
        return _Role()


class _Cfg:
    personas = None
    performers = _Performers()

    def resolve_performer_dispatch_model(self, role):
        return {"model": "example/model"}


def _daemon(state: dict) -> CoordinareDaemon:
    d = object.__new__(CoordinareDaemon)
    d._state = state
    d._documenting_side_tasks = set()
    return d


def _session():
    return {
        "card_id": "c1",
        "performer_stage": "implementing",
        "current_symphony": "website",
        "workspace_branch": "coordinare/c1/feature",
        "current_card": {"id": "c1", "title": "T", "body": "B", "issue_number": 5, "base_branch": "main"},
        "blueprint": {
            "summary": "s", "milestones": [{"goal": "g", "scope": [], "done_when": "d"}], "modules": [],
            "data_model": {"changes": []}, "interfaces": [], "risks": [],
            "criteria": [{"surface": "/", "action": "a", "expected": "e", "kind": "functional"}],
            "docs": [{"topic": "t", "location": "docs/wiki/x.md", "say": "s"}],
            "size": "small", "blueprint_hash": "h1", "created_at": "t",
        },
        "documenting_side": None,
    }


@pytest.mark.asyncio
async def test_resolve_builds_the_documenter_dispatch(monkeypatch):
    monkeypatch.setattr("coordinare.services.persona_service.get_effective_instructions", lambda role, personas: "TECH WRITER PERSONA")
    d = _daemon({
        "symphony_workspace_managers": {"website": _WM()},
        "config": _Cfg(),
    })
    resolved = await d._resolve_documenting_side("c1", _session(), {"website": _GitHub()})
    assert resolved is not None
    ctx, ws = resolved
    assert ctx["role"] == "documenting" and ctx["repo_url"] == "https://github.com/ViviDynamics/website.git"
    assert ctx["branch"] == "coordinare/c1/feature" and ctx["backend"] == "codex" and ctx["model"] == "example/model"
    assert ctx["persona_instructions"].startswith("TECH WRITER PERSONA")
    assert "write only inside the repository's documentation tree" in ctx["persona_instructions"]
    assert "write only under docs/" not in ctx["persona_instructions"]
    assert set(ctx["documentation_brief"]) == {"summary", "docs", "modules"}
    assert ws.github_token == "ghs_fresh" and ws.branch == "coordinare/c1/feature"


@pytest.mark.asyncio
async def test_resolve_returns_none_without_a_repo_or_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    d = _daemon({"symphony_workspace_managers": {}, "config": _Cfg()})
    assert await d._resolve_documenting_side("c1", _session(), {}) is None  # no github service at all
    sess = _session()
    assert await d._resolve_documenting_side("c1", sess, {"website": _GitHub()}) is None  # no token


@pytest.mark.asyncio
async def test_cycle_dispatches_and_tracks_the_poll(monkeypatch):
    monkeypatch.setattr("coordinare.services.persona_service.get_effective_instructions", lambda role, personas: "p")

    class _Svc:
        def __init__(self):
            self.dispatched = []

        async def dispatch_card(self, ctx, workspace_info=None):
            self.dispatched.append(ctx)
            return {"session_id": "sid-1"}

        async def check_status(self, sid):
            return {"status": "docs_committed", "head_sha": "abc"}

    svc = _Svc()
    sessions = {"c1": _session()}
    d = _daemon({
        "performer_services": {"documenting": svc},
        "active_sessions": sessions,
        "symphony_workspace_managers": {"website": _WM()},
        "config": _Cfg(),
    })
    await d._dispatch_documenting_side_runs({"website": _GitHub()}, dispatchable_card_ids={"c1"})
    assert len(svc.dispatched) == 1 and sessions["c1"]["documenting_side"]["status"] == "running"
    await asyncio.gather(*d._documenting_side_tasks)
    assert sessions["c1"]["documenting_side"]["status"] == "done"
    await d._dispatch_documenting_side_runs({"website": _GitHub()}, dispatchable_card_ids={"c1"})
    assert len(svc.dispatched) == 1, "once per blueprint hash"


@pytest.mark.asyncio
async def test_cycle_never_raises_into_the_loop():
    d = _daemon({"performer_services": {"documenting": object()}, "active_sessions": {"c1": {"blueprint": "garbage"}}})
    await d._dispatch_documenting_side_runs({})  # should not raise
