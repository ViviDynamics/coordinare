"""555 — implementer-side rebase: the daemon decides, the implementer does.

Covers the service-level pieces: the opt-in config key, the divergence
feedback contract, the request helper, the anti-thrash marker, and the
guarantee that no clone/rebase/push entry point survives in the daemon.
The guard and sweep call sites are covered in
tests/unit/graph/nodes/test_dispatch_performer_rebase.py and
tests/unit/graph/nodes/test_check_board_rebase.py.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from coordinare.config import ImplementerRebaseConfig, ProjectConfiguration
from coordinare.models.rebase import RebaseJob, RebaseOutcome, RebaseRound
from coordinare.services import rebase
from coordinare.services.rebase import (
    build_divergence_feedback,
    classify_pre_dispatch,
    implementer_rebase_enabled,
    request_implementer_rebase,
    should_attempt_rebase,
)

_BRANCH = "coordinare/PVTI_1/feature"
_MAIN = "a" * 40
_HEAD = "b" * 40


# --- config (FR-008) ---------------------------------------------------------- #


def test_config_defaults_on() -> None:
    """Owner decision 2026-10-13: on by default; `enabled: false` opts out."""
    assert ProjectConfiguration.model_fields["implementer_rebase"].default_factory is ImplementerRebaseConfig
    assert ImplementerRebaseConfig().enabled is True


def test_config_rejects_unknown_keys() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ImplementerRebaseConfig(enabled=True, daemon_side=True)  # type: ignore[call-arg]


@pytest.mark.parametrize(("config", "expected"), [
    (None, False),
    (SimpleNamespace(), True),
    (SimpleNamespace(implementer_rebase=None), True),
    (SimpleNamespace(implementer_rebase=ImplementerRebaseConfig()), True),
    (SimpleNamespace(implementer_rebase=ImplementerRebaseConfig(enabled=False)), False),
])
def test_enabled_follows_the_model_default_unless_switched_off(config, expected) -> None:
    assert implementer_rebase_enabled(config) is expected


# --- main SHA is read through the platform API, never git --------------------- #


@pytest.mark.asyncio
async def test_fetch_main_sha_reads_the_branch_head_from_the_service() -> None:
    github = SimpleNamespace(get_branch_head_sha=AsyncMock(return_value=_MAIN))
    assert await rebase.fetch_main_sha(github) == _MAIN
    github.get_branch_head_sha.assert_awaited_once_with("main")


@pytest.mark.asyncio
@pytest.mark.parametrize("github", [
    None,
    SimpleNamespace(),
    SimpleNamespace(get_branch_head_sha=AsyncMock(return_value=None)),
    SimpleNamespace(get_branch_head_sha=AsyncMock(return_value="")),
    SimpleNamespace(get_branch_head_sha=AsyncMock(return_value={"sha": "x"})),
    SimpleNamespace(get_branch_head_sha=AsyncMock(side_effect=RuntimeError("transport"))),
])
async def test_fetch_main_sha_unknown_is_none_never_an_error(github) -> None:
    assert await rebase.fetch_main_sha(github) is None


@pytest.mark.asyncio
async def test_github_service_reads_branch_head_over_rest(monkeypatch) -> None:
    from coordinare.services.github import GitHubService

    seen: dict = {}

    class _Resp:
        is_success = True
        status_code = 200

        def json(self):
            return {"ref": "refs/heads/main", "object": {"sha": _MAIN, "type": "commit"}}

    class _Client:
        def __init__(self, **_kw) -> None: ...
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_a) -> None: ...
        async def get(self, url, headers):
            seen.update(url=url, headers=headers)
            return _Resp()

    monkeypatch.setattr("coordinare.services.github.httpx.AsyncClient", _Client)
    svc = GitHubService.__new__(GitHubService)
    svc._org, svc._project_name = "acme", "repo"
    svc._rest_api_base = lambda: "https://api.github.com"  # type: ignore[method-assign]
    svc._current_token = AsyncMock(return_value="tok")  # type: ignore[method-assign]
    assert await svc.get_branch_head_sha("main") == _MAIN
    assert seen["url"] == "https://api.github.com/repos/acme/repo/git/ref/heads/main"

    svc._current_token = AsyncMock(side_effect=RuntimeError("app auth down"))  # type: ignore[method-assign]
    assert await svc.get_branch_head_sha("main") is None


# --- feedback contract (contracts/divergence-feedback.md) --------------------- #


def _feedback(**kw) -> dict:
    kwargs = {"conflicting": True}
    kwargs.update(kw)
    return build_divergence_feedback(_BRANCH, _MAIN, _HEAD, **kwargs)


@pytest.mark.parametrize(("conflicting", "verdict"), [
    (True, "conflicts with `main`"), (False, "is behind `main`"),
])
def test_feedback_names_branch_main_and_verdict(conflicting, verdict) -> None:
    body = _feedback(conflicting=conflicting)["body"]
    assert f"`{_BRANCH}`" in body and f"`{_MAIN[:8]}`" in body and verdict in body
    assert _MAIN not in body  # short SHA only in the prose


def test_feedback_carries_the_performer_contract() -> None:
    """The performer keys its setup rebase and its lease push on these fields."""
    entry = _feedback()
    assert entry["kind"] == "branch_divergence"
    assert entry["expected_head_sha"] == _HEAD and entry["main_sha"] == _MAIN
    assert entry["base_branch"] == "main" and entry["source"] == "coordinare"


def test_feedback_asks_for_a_rebase_and_leaves_publishing_to_the_performer() -> None:
    body = _feedback()["body"]
    assert "`git rebase origin/main`" in body and "`git rebase --continue`" in body
    assert "Leave no rebase in progress" in body
    assert "Do not push" in body and "lease" in body


def test_feedback_follows_the_default_branch() -> None:
    entry = _feedback(default_branch="trunk")
    assert "git rebase origin/trunk" in entry["body"] and "origin/main" not in entry["body"]
    assert entry["base_branch"] == "trunk"


def test_feedback_carries_no_credential_or_url() -> None:
    body = _feedback()["body"].lower()
    assert "https://" not in body and "token" not in body and "x-access-token" not in body


def test_daemon_and_performer_agree_on_the_feedback_kind() -> None:
    from performer import workspace as performer_workspace

    assert performer_workspace._DIVERGENCE_FEEDBACK_KIND == rebase.DIVERGENCE_FEEDBACK_KIND


def test_performer_recognises_the_daemon_built_entry() -> None:
    from performer import workspace as performer_workspace
    from performer.models import Score

    score = Score(card_id="c", title="t", description="d", acceptance_criteria=[],
                  repo_url="https://github.com/org/repo", branch=_BRANCH, github_token="x",
                  role="implementing", relay_feedback=[{"body": "review note"}, _feedback()])
    assert performer_workspace._rebase_request(score) == _feedback()


# --- request helper (FR-003, FR-007) ------------------------------------------ #


def _request(session: dict, **overrides) -> None:
    kwargs = {"card_id": "card", "branch": _BRANCH, "main_sha": _MAIN, "head_sha": _HEAD,
              "conflicting": True, "reason": "pre_dispatch"}
    kwargs.update(overrides)
    request_implementer_rebase(session, **kwargs)


def test_request_routes_session_to_implementer() -> None:
    session = {"phase": "monitoring_pr", "performer_stage": "reviewing",
               "agent_dispatch": {"session_id": "old"}, "agent_dispatch_at": "then"}
    _request(session)
    assert session["performer_stage"] == "implementing"
    assert session["phase"] == "dispatching"
    assert session["agent_dispatch"] == {} and session["agent_dispatch_at"] is None
    assert session["relay_feedback"] == [_feedback()]
    assert session["last_rebase_attempt"] == {
        "main_sha": _MAIN, "head_sha": _HEAD, "outcome": "delegated",
    }


def test_request_keeps_queued_feedback_behind_the_instruction() -> None:
    """047's prepare_conflict_resolution replaced the list and dropped whatever
    a reviewer had queued. The instruction goes first; nothing is lost."""
    session = {"relay_feedback": [{"body": "reviewer note"}, {"body": "qa note"}]}
    _request(session)
    assert [item["body"] for item in session["relay_feedback"][1:]] == ["reviewer note", "qa note"]


def test_request_is_idempotent_for_the_same_pair() -> None:
    session: dict = {}
    _request(session)
    _request(session)
    assert len(session["relay_feedback"]) == 1


def test_request_logs_trigger_and_dispatch_without_secrets() -> None:
    with structlog.testing.capture_logs() as logs:
        _request({}, reason="proactive_conflict", prev_main_sha="old")
    events = {e["event"]: e for e in logs}
    trig = events["rebase.triggered"]
    assert trig["reason"] == "proactive_conflict" and trig["outcome"] == "delegated"
    assert trig["prev_main_sha"] == "old" and trig["current_main_sha"] == _MAIN
    assert "rebase.performer_dispatched" in events


def test_marker_written_by_a_request_survives_persistence() -> None:
    """FR-011: no schema change — the existing persist filter accepts the new outcome."""
    from coordinare.daemon import _persist_last_rebase_attempt

    session: dict = {}
    _request(session)
    assert _persist_last_rebase_attempt(session["last_rebase_attempt"]) == session["last_rebase_attempt"]


# --- anti-thrash (FR-007) ----------------------------------------------------- #


def test_delegated_same_pair_is_not_requested_again() -> None:
    session: dict = {}
    _request(session)
    assert should_attempt_rebase(session, _MAIN, _HEAD) is False
    assert classify_pre_dispatch("CONFLICTING", "DIRTY", _HEAD, session, _MAIN) == "blocked_thrash"


@pytest.mark.parametrize(("main", "head"), [(_MAIN, "c" * 40), ("d" * 40, _HEAD)])
def test_delegated_but_main_or_head_moved_is_requested_again(main, head) -> None:
    session: dict = {}
    _request(session)
    assert should_attempt_rebase(session, main, head) is True
    assert classify_pre_dispatch("CONFLICTING", "DIRTY", head, session, main) == "request"


def test_resolved_branch_proceeds_despite_marker() -> None:
    session: dict = {}
    _request(session)
    assert classify_pre_dispatch("MERGEABLE", "CLEAN", "c" * 40, session, _MAIN) == "proceed"


# --- models ------------------------------------------------------------------- #


def test_delegated_outcome_in_round_summary() -> None:
    rr = RebaseRound(jobs=[
        RebaseJob(card_id="a", branch="b1", outcome=RebaseOutcome.DELEGATED),
        RebaseJob(card_id="b", branch="b2", outcome=RebaseOutcome.DEFERRED),
    ])
    assert rr.summary == "1 handed to implementer, 1 deferred"
    assert rr.to_dict()["jobs"][0]["outcome"] == "delegated"


def test_job_no_longer_carries_clone_paths() -> None:
    assert not {"repo_dir", "tmp_dir"} & set(RebaseJob.__dataclass_fields__)


# --- the daemon never clones, rebases or pushes (FR-001) ---------------------- #


_REMOVED = (
    "_run_git", "_git_env_for_token", "repo_url_from_config",
    "rebase_branch", "force_push_with_lease", "extract_conflict_info",
    "run_rebase_round", "prepare_conflict_resolution", "build_conflict_feedback",
    "build_conflict_block_comment",
)


@pytest.mark.parametrize("name", _REMOVED)
def test_daemon_side_rebase_entry_points_are_gone(name) -> None:
    assert not hasattr(rebase, name)


def test_the_service_runs_no_subprocess_at_all() -> None:
    """Not even ls-remote: the base head comes from the platform API."""
    source = inspect.getsource(rebase)
    for needle in ("subprocess", "create_subprocess", "tempfile", "shutil", '"ls-remote"',
                   '"clone"', '"push"', '"fetch"', '"checkout"', "--force"):
        assert needle not in source, needle


@pytest.mark.parametrize("module_name", [
    "coordinare.daemon",
    "coordinare.graph.nodes.check_board",
    "coordinare.graph.nodes.merge_pr",
    "coordinare.graph.nodes.dispatch_performer",
])
def test_former_call_sites_reference_no_removed_entry_point(module_name) -> None:
    import importlib

    source = inspect.getsource(importlib.import_module(module_name))
    for name in _REMOVED:
        assert name not in source, f"{module_name} still references {name}"
