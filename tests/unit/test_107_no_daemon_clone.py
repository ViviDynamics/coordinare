"""107: the daemon clones the target repository only when it has to.

Coordinare is an orchestrator. A containerized performer clones for itself
(the dispatch payload carries only repo_url and branch), so a daemon-side
clone for it is redundant. Two things still need the tree on the daemon: the
subprocess transport, which works in the daemon's clone, and the 083
dispatch-time scanner floor for a security role on the prose path.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

from coordinare.graph.nodes.dispatch_performer import (
    _STAGE_TO_ROLE,
    _daemon_clone_needed,
    _performer_clones_own_workspace,
    _setup_workspace,
)
from coordinare.resilience import ResilientAgentService
from coordinare.services.agent_service import AgentService
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.persona_classifier import PROJECT_CONTEXT_BYTES, _fetch_project_context
from coordinare.workspace import WorkspaceInfo, WorkspaceManager
from tests.unit.test_workspace import _make_config

_CARD = {"id": "89", "title": "add auth", "status": "IN_PROGRESS",
         "pr_url": "https://github.com/acme/myrepo/pull/5", "pr_node_id": "PR_5"}


def _http_service() -> Any:
    return SimpleNamespace(clones_own_workspace=True)


def _state(*, security_workflow: str | None = None) -> dict:
    performers = SimpleNamespace(
        resolved_role=lambda role: SimpleNamespace(workflow=security_workflow if role == "security" else None),
    )
    return {"config": SimpleNamespace(performers=performers), "current_card": dict(_CARD)}


# --- which performers clone for themselves ----------------------------------- #


def test_http_performer_service_declares_it_clones_for_itself() -> None:
    assert HTTPPerformerService.clones_own_workspace is True
    assert getattr(AgentService, "clones_own_workspace", False) is False


def test_marker_is_found_through_the_resilience_wrapper() -> None:
    wrapped = ResilientAgentService.__new__(ResilientAgentService)
    wrapped._inner = _http_service()
    assert _performer_clones_own_workspace(wrapped) is True
    wrapped._inner = SimpleNamespace()
    assert _performer_clones_own_workspace(wrapped) is False


@pytest.mark.parametrize("service", [None, SimpleNamespace(), MagicMock(), SimpleNamespace(clones_own_workspace="yes")])
def test_only_an_explicit_true_counts(service) -> None:
    """An unknown service (or a mock that answers every attribute) keeps the clone."""
    assert _performer_clones_own_workspace(service) is False


# --- when the daemon still needs the tree ------------------------------------ #


_NON_SECURITY_STAGES = sorted(stage for stage in _STAGE_TO_ROLE if stage != "security")


def test_stage_list_is_the_real_one() -> None:
    """Guard the parametrisation below against a stage name that maps to no role."""
    assert "closing_review" in _NON_SECURITY_STAGES and "security" in _STAGE_TO_ROLE
    assert all(_STAGE_TO_ROLE[stage] for stage in _NON_SECURITY_STAGES)


@pytest.mark.parametrize("stage", _NON_SECURITY_STAGES)
def test_self_cloning_performer_needs_no_daemon_clone(stage) -> None:
    assert _daemon_clone_needed(_state(), stage, _http_service()) is False


def test_http_service_keeps_its_docstring() -> None:
    assert HTTPPerformerService.__doc__ and "adapter" in HTTPPerformerService.__doc__


@pytest.mark.parametrize("stage", ["implementing", "security"])
def test_subprocess_performer_always_needs_the_daemon_clone(stage) -> None:
    assert _daemon_clone_needed(_state(security_workflow="security"), stage, SimpleNamespace()) is True


def test_prose_path_security_role_keeps_the_clone_and_says_why() -> None:
    """The 083 scanner floor runs in the daemon over its clone. It fails closed
    without one, so the clone stays for this stage and a warning names the fix."""
    with structlog.testing.capture_logs() as logs:
        assert _daemon_clone_needed(_state(), "security", _http_service()) is True
    (event,) = [e for e in logs if e["event"] == "security_floor.daemon_side_scan"]
    assert event["card_id"] == "89" and "workflow: security" in event["remedy"]


def test_security_workflow_scans_in_the_performer_so_no_clone() -> None:
    with structlog.testing.capture_logs() as logs:
        assert _daemon_clone_needed(_state(security_workflow="security"), "security", _http_service()) is False
    assert not [e for e in logs if e["event"] == "security_floor.daemon_side_scan"]


# --- _setup_workspace passes the decision on ---------------------------------- #


class _Manager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def prepare(self, card: dict[str, Any], *, local_clone: bool = True) -> WorkspaceInfo:
        self.calls.append({"local_clone": local_clone})
        return WorkspaceInfo(path=None, branch="coordinare/89/add-auth",
                             repo_url="https://github.com/acme/myrepo.git", github_token="t")


class _LegacyManager:
    """A manager written before #107: no local_clone keyword."""

    def __init__(self) -> None:
        self.called = False

    async def prepare(self, card: dict[str, Any]) -> WorkspaceInfo:
        self.called = True
        return WorkspaceInfo(path=None, branch="b", repo_url="u", github_token="t")


async def _setup(manager: Any, service: Any, stage: str = "implementing", state: dict | None = None):
    state = state if state is not None else _state()
    info, terminal = await _setup_workspace(
        state, dict(_CARD), "89", stage, manager, None, lambda: None, service=service,
    )
    assert terminal is None
    return state, info


@pytest.mark.asyncio
async def test_self_cloning_dispatch_asks_for_no_local_clone() -> None:
    manager = _Manager()
    with structlog.testing.capture_logs() as logs:
        state, info = await _setup(manager, _http_service())
    assert manager.calls == [{"local_clone": False}]
    assert state["workspace_path"] is None and state["workspace_branch"] == info.branch
    assert [e for e in logs if e["event"] == "workspace.daemon_clone_skipped"]


@pytest.mark.asyncio
async def test_subprocess_dispatch_keeps_the_local_clone() -> None:
    manager = _Manager()
    await _setup(manager, SimpleNamespace())
    assert manager.calls == [{"local_clone": True}]


@pytest.mark.asyncio
async def test_prose_security_dispatch_keeps_the_local_clone() -> None:
    manager = _Manager()
    await _setup(manager, _http_service(), stage="security")
    assert manager.calls == [{"local_clone": True}]


@pytest.mark.asyncio
async def test_manager_without_the_keyword_is_called_as_before() -> None:
    manager = _LegacyManager()
    await _setup(manager, _http_service())
    assert manager.called is True


@pytest.mark.asyncio
async def test_no_service_given_keeps_the_local_clone() -> None:
    manager = _Manager()
    state = _state()
    await _setup_workspace(state, dict(_CARD), "89", "implementing", manager, None, lambda: None)
    assert manager.calls == [{"local_clone": True}]


# --- WorkspaceManager honours it ---------------------------------------------- #


@pytest.mark.asyncio
async def test_prepare_without_local_clone_runs_no_git_and_still_resolves_branch_and_token() -> None:
    cfg = _make_config(agent_transport="subprocess", github_token="static-pat")
    mgr = WorkspaceManager(cfg)
    mgr._resolve_pr_head = AsyncMock(return_value="coordinare/89/add-auth")
    with patch("coordinare.workspace._run_git", new=AsyncMock(side_effect=AssertionError("daemon ran git"))), \
         patch("coordinare.workspace.tempfile.mkdtemp", side_effect=AssertionError("daemon made a clone dir")):
        info = await mgr.prepare(dict(_CARD), local_clone=False)
    assert info.path is None
    assert info.branch == "coordinare/89/add-auth"
    assert info.repo_url == "https://github.com/acme/myrepo.git"
    assert info.github_token == "static-pat"


# --- project context without a clone ------------------------------------------ #


@pytest.mark.asyncio
async def test_project_context_is_read_through_the_api_at_the_pr_head() -> None:
    files = {"CLAUDE.md": "c" * (PROJECT_CONTEXT_BYTES + 50), "AGENTS.md": "agents"}
    github = SimpleNamespace(get_file_content=AsyncMock(side_effect=lambda o, r, path, ref: files[path]))
    out = await _fetch_project_context(github, "acme", "myrepo", "abc123")
    assert out == {"claude_md_head": "c" * PROJECT_CONTEXT_BYTES, "agents_md_head": "agents"}
    github.get_file_content.assert_any_await("acme", "myrepo", "CLAUDE.md", "abc123")


@pytest.mark.asyncio
async def test_project_context_falls_back_to_the_default_branch() -> None:
    github = SimpleNamespace(get_file_content=AsyncMock(return_value=None))
    assert await _fetch_project_context(github, "acme", "myrepo", "") == {"claude_md_head": "", "agents_md_head": ""}
    github.get_file_content.assert_any_await("acme", "myrepo", "AGENTS.md", "HEAD")


@pytest.mark.asyncio
@pytest.mark.parametrize("github", [
    None, SimpleNamespace(),
    SimpleNamespace(get_file_content=AsyncMock(side_effect=RuntimeError("graphql down"))),
    SimpleNamespace(get_file_content=AsyncMock(return_value={"not": "text"})),
])
async def test_project_context_read_is_best_effort(github) -> None:
    assert await _fetch_project_context(github, "acme", "myrepo", "abc") == {"claude_md_head": "", "agents_md_head": ""}
