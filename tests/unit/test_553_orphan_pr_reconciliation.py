"""Unit tests for 553 — orphaned PR reconciliation for completed cards (issue #78)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coordinare.config import OrphanPrConfig, ProjectConfiguration
from coordinare.services.orphan_pr import is_terminal_card_status, resolve_pr_to_card
from coordinare.services.github import CLOSE_PULL_REQUEST_MUTATION, LIST_OPEN_PULL_REQUESTS_QUERY, GitHubService
from coordinare.services.fake_github import FakeGitHubService

_BASE = {
    "project_name": "Demo",
    "github_org": "acme",
    "github_project_number": 1,
    "github_token": "tok",
    "human_reviewers": ["alice"],
}


# ---------------------------------------------------------------------------
# Task 1 — config block: opt-in, default off, independent switches
# ---------------------------------------------------------------------------


def test_orphan_pr_config_defaults_off() -> None:
    cfg = ProjectConfiguration(**_BASE)
    assert isinstance(cfg.orphan_pr, OrphanPrConfig)
    assert cfg.orphan_pr.enabled is False
    assert cfg.orphan_pr.completion_reconciliation is False
    assert cfg.orphan_pr.sweep is False


def test_orphan_pr_config_independent_switches() -> None:
    cfg = ProjectConfiguration(
        **_BASE, orphan_pr=OrphanPrConfig(enabled=True, sweep=True)
    )
    assert cfg.orphan_pr.enabled is True
    assert cfg.orphan_pr.sweep is True
    assert cfg.orphan_pr.completion_reconciliation is False

    cfg2 = ProjectConfiguration(
        **_BASE,
        orphan_pr=OrphanPrConfig(enabled=True, completion_reconciliation=True),
    )
    assert cfg2.orphan_pr.completion_reconciliation is True
    assert cfg2.orphan_pr.sweep is False


def test_orphan_pr_config_rejects_unknown_keys() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProjectConfiguration(
            **_BASE,
            orphan_pr=OrphanPrConfig(enabled=True, unknown_switch=True),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Task 2 — GitHub surface: list_open_pull_requests + close_pull_request
# ---------------------------------------------------------------------------


def _fake_with_prs(tmp_path) -> FakeGitHubService:
    fake = FakeGitHubService(bare_repo_path=tmp_path, org="acme", project_name="repo")
    fake.seed_card(
        "card1", status="Done", title="add collapse", body="do it",
        issue_number=19, issue_node_id="I_card1",
    )
    fake.open_pr(issue_item_id="card1", head_ref="coordinare/PVTI_1/add-collapse")
    fake.open_pr(issue_item_id="card1", head_ref="coordinare/PVTI_1/superseded")
    return fake


@pytest.mark.asyncio
async def test_fake_list_open_pull_requests_shape(tmp_path) -> None:
    fake = _fake_with_prs(tmp_path)
    prs = await fake.list_open_pull_requests("acme", "repo")
    assert len(prs) == 2
    for pr in prs:
        assert set(pr) == {"id", "number", "url", "head_ref", "body"}
        assert pr["head_ref"].startswith("coordinare/PVTI_1/")


@pytest.mark.asyncio
async def test_fake_close_pull_request_removes_from_open_list(tmp_path) -> None:
    fake = _fake_with_prs(tmp_path)
    result = await fake.close_pull_request("PR_2")
    assert result == {"closed": True, "id": "PR_2"}
    prs = await fake.list_open_pull_requests("acme", "repo")
    assert [pr["id"] for pr in prs] == ["PR_1"]
    assert await fake.branch_has_open_pr("coordinare/PVTI_1/superseded") is False


@pytest.mark.asyncio
async def test_fake_close_pull_request_unknown_id(tmp_path) -> None:
    fake = _fake_with_prs(tmp_path)
    result = await fake.close_pull_request("PR_missing")
    assert result == {"closed": False, "id": "PR_missing"}


@pytest.mark.asyncio
async def test_real_list_open_pull_requests_parses_nodes() -> None:
    service = GitHubService(token="test", org="acme", project_number=1)
    service._guarded_execute = AsyncMock(return_value={  # type: ignore[method-assign]
        "repository": {
            "pullRequests": {
                "nodes": [{
                    "id": "PR_k1", "number": 21, "url": "https://github.com/acme/repo/pull/21",
                    "headRefName": "conductor/PVTI_x/add-collapse", "body": "closes #19",
                }],
            },
        },
    })
    prs = await service.list_open_pull_requests("acme", "repo")
    assert prs == [{
        "id": "PR_k1", "number": 21, "url": "https://github.com/acme/repo/pull/21",
        "head_ref": "conductor/PVTI_x/add-collapse", "body": "closes #19",
    }]
    assert "pullRequests" in LIST_OPEN_PULL_REQUESTS_QUERY


@pytest.mark.asyncio
async def test_real_list_open_pull_requests_error_returns_empty() -> None:
    service = GitHubService(token="test", org="acme", project_number=1)
    service._guarded_execute = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    assert await service.list_open_pull_requests("acme", "repo") == []


@pytest.mark.asyncio
async def test_real_close_pull_request_parses_state() -> None:
    service = GitHubService(token="test", org="acme", project_number=1)
    service._guarded_execute = AsyncMock(return_value={  # type: ignore[method-assign]
        "closePullRequest": {"pullRequest": {"id": "PR_k1", "state": "CLOSED"}},
    })
    result = await service.close_pull_request("PR_k1")
    assert result == {"closed": True, "id": "PR_k1"}
    assert "closePullRequest" in CLOSE_PULL_REQUEST_MUTATION


# ---------------------------------------------------------------------------
# Task 3 — attribution: open PR → completed card
# ---------------------------------------------------------------------------

_CARDS = {
    "PVTI_1": {
        "id": "PVTI_1", "status": "DONE", "issue_number": 19,
        "content_node_id": "I_1", "title": "add collapse", "pr_url": "https://fake/pr/22",
    },
    "PVTI_2": {
        "id": "PVTI_2", "status": "TODO", "issue_number": 20,
        "content_node_id": "I_2", "title": "other", "pr_url": None,
    },
}


def test_attribution_via_canonical_session_branch() -> None:
    pr = {"id": "PR_a", "number": 3, "head_ref": "coordinare/PVTI_1/add-collapse", "body": ""}
    att = resolve_pr_to_card(pr, _CARDS)
    assert att.card_item_id == "PVTI_1"
    assert att.method == "session_branch"


def test_attribution_via_legacy_session_branch() -> None:
    pr = {"id": "PR_a", "number": 21, "head_ref": "conductor/PVTI_1/080-t037-add-collapse", "body": ""}
    att = resolve_pr_to_card(pr, _CARDS)
    assert att.card_item_id == "PVTI_1"
    assert att.method == "session_branch"


def test_attribution_via_body_issue_reference() -> None:
    pr = {"id": "PR_a", "number": 3, "head_ref": "feature/foo", "body": "Closes #19."}
    att = resolve_pr_to_card(pr, _CARDS)
    assert att.card_item_id == "PVTI_1"
    assert att.method == "body_reference"


def test_attribution_via_body_repo_qualified_reference() -> None:
    pr = {"id": "PR_a", "number": 3, "head_ref": "feature/foo", "body": "see acme/repo#20"}
    att = resolve_pr_to_card(pr, _CARDS)
    assert att.card_item_id == "PVTI_2"
    assert att.method == "body_reference"


def test_attribution_unresolved() -> None:
    pr = {"id": "PR_a", "number": 3, "head_ref": "feature/foo", "body": "no refs here"}
    att = resolve_pr_to_card(pr, _CARDS)
    assert att.card_item_id is None
    assert att.method == "unresolved"


def test_terminal_card_status_recognition() -> None:
    assert is_terminal_card_status("DONE")
    assert is_terminal_card_status("Closed")
    assert not is_terminal_card_status("TODO")
    assert not is_terminal_card_status("IN_REVIEW")
    assert not is_terminal_card_status(None)

