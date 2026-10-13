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


def test_config_defaults_off() -> None:
    assert ProjectConfiguration.model_fields["implementer_rebase"].default_factory is ImplementerRebaseConfig
    assert ImplementerRebaseConfig().enabled is False


def test_config_rejects_unknown_keys() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ImplementerRebaseConfig(enabled=True, daemon_side=True)  # type: ignore[call-arg]


@pytest.mark.parametrize(("config", "expected"), [
    (None, False),
    (SimpleNamespace(), False),
    (SimpleNamespace(implementer_rebase=None), False),
    (SimpleNamespace(implementer_rebase=ImplementerRebaseConfig()), False),
    (SimpleNamespace(implementer_rebase=ImplementerRebaseConfig(enabled=True)), True),
])
def test_enabled_reads_anything_unreadable_as_off(config, expected) -> None:
    assert implementer_rebase_enabled(config) is expected


# --- feedback contract (contracts/divergence-feedback.md) --------------------- #


@pytest.mark.parametrize(("conflicting", "verdict"), [
    (True, "conflicts with `main`"), (False, "is behind `main`"),
])
def test_feedback_names_branch_main_and_verdict(conflicting, verdict) -> None:
    body = build_divergence_feedback(_BRANCH, _MAIN, conflicting=conflicting)["body"]
    assert f"`{_BRANCH}`" in body and f"`{_MAIN[:8]}`" in body and verdict in body
    assert _MAIN not in body  # short SHA only


def test_feedback_asks_for_a_merge_and_forbids_history_rewrite() -> None:
    """The performer push path never force-pushes over an existing branch (165),
    so a rebased branch could not be published. The instruction must say merge."""
    body = build_divergence_feedback(_BRANCH, _MAIN, conflicting=True)["body"]
    assert "`git merge origin/main`" in body
    assert "Do not `git rebase`" in body and "do not force-push" in body
    assert "commit the merge" in body


def test_feedback_follows_the_default_branch() -> None:
    body = build_divergence_feedback(_BRANCH, _MAIN, conflicting=True, default_branch="trunk")["body"]
    assert "git merge origin/trunk" in body and "origin/main" not in body


def test_feedback_carries_no_credential_or_url() -> None:
    body = build_divergence_feedback(_BRANCH, _MAIN, conflicting=True)["body"].lower()
    assert "https://" not in body and "token" not in body and "x-access-token" not in body


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
    assert session["relay_feedback"] == [build_divergence_feedback(_BRANCH, _MAIN, conflicting=True)]
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
    "rebase_branch", "force_push_with_lease", "extract_conflict_info",
    "run_rebase_round", "prepare_conflict_resolution", "build_conflict_feedback",
    "build_conflict_block_comment",
)


@pytest.mark.parametrize("name", _REMOVED)
def test_daemon_side_rebase_entry_points_are_gone(name) -> None:
    assert not hasattr(rebase, name)


def test_only_git_invocation_left_in_the_service_is_ls_remote() -> None:
    """Every git call in the module goes through _run_git; its only caller must
    be the read-only ls-remote in fetch_main_sha."""
    source = inspect.getsource(rebase)
    callers = [
        name for name, fn in inspect.getmembers(rebase, inspect.isfunction)
        if fn.__module__ == rebase.__name__ and name != "_run_git"
        and "_run_git(" in inspect.getsource(fn)
    ]
    assert callers == ["fetch_main_sha"]
    assert '["ls-remote", repo_url' in inspect.getsource(rebase.fetch_main_sha)
    for verb in ('"clone"', '"rebase"', '"push"', '"fetch"', '"checkout"', "--force"):
        assert verb not in source


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
