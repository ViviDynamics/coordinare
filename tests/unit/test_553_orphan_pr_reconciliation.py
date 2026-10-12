"""Unit tests for 553 — orphaned PR reconciliation for completed cards (issue #78)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from coordinare.config import OrphanPrConfig, ProjectConfiguration
from coordinare.services.board_provider import MoveOutcome
from coordinare.services.fake_github import FakeGitHubService
from coordinare.services.github import (
    CLOSE_PULL_REQUEST_MUTATION,
    LIST_OPEN_PULL_REQUESTS_QUERY,
    GitHubService,
)
from coordinare.services.orphan_pr import (
    build_close_comment,
    close_superseded_prs,
    is_terminal_card_status,
    resolve_pr_to_card,
    run_orphan_pr_sweep,
)

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
        **_BASE, orphan_pr=OrphanPrConfig(enabled=True, sweep=True),
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


# ---------------------------------------------------------------------------
# Task 4 — comment assembly (FR-004: explanatory, names the superseding PR)
# ---------------------------------------------------------------------------

_CARD_DONE = {
    "id": "PVTI_1", "status": "DONE", "issue_number": 19,
    "content_node_id": "I_1", "title": "add collapse",
    "pr_url": "https://github.com/acme/repo/pull/22",
}


def test_close_comment_names_superseding_pr() -> None:
    body = build_close_comment(_CARD_DONE, "session_branch")
    assert "https://github.com/acme/repo/pull/22" in body
    assert "#19" in body
    assert "add collapse" in body
    assert "coordinare-attribution" in body
    assert "session_branch" in body
    assert "DONE" in body


def test_close_comment_without_superseding_pr() -> None:
    card = dict(_CARD_DONE, pr_url=None)
    body = build_close_comment(card, "session_branch")
    assert "coordinare-attribution" in body
    assert "#19" in body
    assert "DONE" in body
    assert "pull/" not in body


# ---------------------------------------------------------------------------
# Task 5 — completion-time reconciliation (US-1)
# ---------------------------------------------------------------------------


def _fake_repo(tmp_path) -> FakeGitHubService:
    return FakeGitHubService(bare_repo_path=tmp_path, org="acme", project_name="repo")


@pytest.mark.asyncio
async def test_completion_two_pr_card_closes_loser(tmp_path) -> None:
    fake = _fake_repo(tmp_path)
    fake.seed_card(
        "PVTI_1", status="DONE", title="add collapse", body="do it",
        issue_number=19, issue_node_id="I_1",
    )
    winner = fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/add-collapse")
    # open_pr creates no commits, so the fake's local merge cannot run; mark
    # the winner merged the same way test_151_identity does.
    fake._prs[winner]["merged"] = True
    fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/retry-attempt")
    card = {
        "id": "PVTI_1", "status": "DONE", "issue_number": 19,
        "content_node_id": "I_1", "title": "add collapse",
        "pr_url": f"https://fake/acme/repo/pull/{int(winner.split('_')[1])}",
    }
    open_prs = await fake.list_open_pull_requests("acme", "repo")
    assert len(open_prs) == 1
    decisions = await close_superseded_prs(fake, card, open_prs)
    assert [d.outcome for d in decisions] == ["closed"]
    assert decisions[0].pr_url == open_prs[0]["url"]
    assert decisions[0].comment_posted is True
    prs = await fake.list_open_pull_requests("acme", "repo")
    assert prs == []


@pytest.mark.asyncio
async def test_completion_single_pr_card_noop(tmp_path) -> None:
    fake = _fake_repo(tmp_path)
    fake.seed_card(
        "PVTI_1", status="DONE", title="add collapse", body="do it",
        issue_number=19, issue_node_id="I_1",
    )
    winner = fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/add-collapse")
    fake._prs[winner]["merged"] = True
    card = {
        "id": "PVTI_1", "status": "DONE", "issue_number": 19,
        "content_node_id": "I_1", "title": "add collapse",
        "pr_url": f"https://fake/acme/repo/pull/{int(winner.split('_')[1])}",
    }
    open_prs = await fake.list_open_pull_requests("acme", "repo")
    assert open_prs == []  # winner merged → not listed
    decisions = await close_superseded_prs(fake, card, open_prs)
    assert decisions == []


@pytest.mark.asyncio
async def test_completion_nonterminal_card_never_closes(tmp_path) -> None:
    fake = _fake_repo(tmp_path)
    fake.seed_card(
        "PVTI_1", status="IN_REVIEW", title="add collapse", body="do it",
        issue_number=19, issue_node_id="I_1",
    )
    fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/add-collapse")
    card = {
        "id": "PVTI_1", "status": "IN_REVIEW", "issue_number": 19,
        "content_node_id": "I_1", "title": "add collapse", "pr_url": None,
    }
    open_prs = await fake.list_open_pull_requests("acme", "repo")
    decisions = await close_superseded_prs(fake, card, open_prs)
    assert [d.outcome for d in decisions] == ["skipped-active"]
    assert await fake.list_open_pull_requests("acme", "repo") == open_prs


# ---------------------------------------------------------------------------
# Task 6 — per-cycle janitor sweep (US-2)
# ---------------------------------------------------------------------------


def _done_card_fixture(tmp_path):
    fake = _fake_repo(tmp_path)
    fake.seed_card(
        "PVTI_1", status="DONE", title="add collapse", body="do it",
        issue_number=19, issue_node_id="I_1",
    )
    return fake


@pytest.mark.asyncio
async def test_sweep_closes_done_card_prs_including_legacy_branches(tmp_path) -> None:
    fake = _done_card_fixture(tmp_path)
    fake.open_pr(issue_item_id="PVTI_1", head_ref="conductor/PVTI_1/080-t037-add-collapse")
    winner = fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/add-collapse")
    fake._prs[winner]["merged"] = True
    board = await fake.poll_board()
    decisions = await run_orphan_pr_sweep(fake, board, OrphanPrConfig(enabled=True, sweep=True))
    assert [d.outcome for d in decisions] == ["closed"]
    assert decisions[0].basis == "session_branch"
    assert decisions[0].comment_posted is True
    assert await fake.list_open_pull_requests("acme", "repo") == []


@pytest.mark.asyncio
async def test_sweep_multi_pr_same_card_closes_all_losers(tmp_path) -> None:
    fake = _done_card_fixture(tmp_path)
    fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/attempt-one")
    fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/attempt-two")
    winner = fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/add-collapse")
    fake._prs[winner]["merged"] = True
    board = await fake.poll_board()
    decisions = await run_orphan_pr_sweep(fake, board, OrphanPrConfig(enabled=True, sweep=True))
    assert sorted(d.outcome for d in decisions) == ["closed", "closed"]
    assert await fake.list_open_pull_requests("acme", "repo") == []


@pytest.mark.asyncio
async def test_sweep_leaves_active_card_prs_open(tmp_path) -> None:
    fake = _fake_repo(tmp_path)
    fake.seed_card(
        "PVTI_2", status="IN_REVIEW", title="live work", body="do it",
        issue_number=20, issue_node_id="I_2",
    )
    fake.open_pr(issue_item_id="PVTI_2", head_ref="coordinare/PVTI_2/live")
    board = await fake.poll_board()
    decisions = await run_orphan_pr_sweep(fake, board, OrphanPrConfig(enabled=True, sweep=True))
    assert [d.outcome for d in decisions] == ["skipped-active"]
    assert len(await fake.list_open_pull_requests("acme", "repo")) == 1


@pytest.mark.asyncio
async def test_sweep_skips_unresolvable_prs(tmp_path) -> None:
    fake = _fake_repo(tmp_path)
    fake.seed_card(
        "PVTI_1", status="DONE", title="add collapse", body="do it",
        issue_number=19, issue_node_id="I_1",
    )
    fake.open_pr(issue_item_id="ghost", head_ref="feature/human-authored", body="no refs")
    board = await fake.poll_board()
    decisions = await run_orphan_pr_sweep(fake, board, OrphanPrConfig(enabled=True, sweep=True))
    assert [d.outcome for d in decisions] == ["skipped-unresolved"]
    assert len(await fake.list_open_pull_requests("acme", "repo")) == 1


@pytest.mark.asyncio
async def test_sweep_is_idempotent(tmp_path) -> None:
    fake = _done_card_fixture(tmp_path)
    fake.open_pr(issue_item_id="PVTI_1", head_ref="conductor/PVTI_1/080-t037-add-collapse")
    winner = fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/add-collapse")
    fake._prs[winner]["merged"] = True
    board = await fake.poll_board()
    first = await run_orphan_pr_sweep(fake, board, OrphanPrConfig(enabled=True, sweep=True))
    assert [d.outcome for d in first] == ["closed"]
    second = await run_orphan_pr_sweep(fake, board, OrphanPrConfig(enabled=True, sweep=True))
    assert second == []  # nothing open left → no double comments, no re-closes


@pytest.mark.asyncio
async def test_sweep_disabled_never_reads_github(tmp_path, monkeypatch) -> None:
    fake = _done_card_fixture(tmp_path)
    fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/add-collapse")
    board = await fake.poll_board()
    called = AsyncMock(side_effect=AssertionError("must not read when disabled"))
    monkeypatch.setattr(fake, "list_open_pull_requests", called)
    decisions = await run_orphan_pr_sweep(fake, board, OrphanPrConfig(enabled=False))
    assert decisions == []
    called.assert_not_awaited()



# ---------------------------------------------------------------------------
# Task 7 — wire-in: merge path + per-cycle sweep call sites
# ---------------------------------------------------------------------------


class _MergeGitHub:
    def __init__(self, open_prs: list[dict[str, Any]]) -> None:
        self._open_prs = open_prs
        self.closed: list[str] = []
        self.comments: list[str] = []
        self.list_calls = 0
        self._org = "acme"
        self._project_name = "repo"

    async def check_mergeability(self, pr_id: str) -> dict[str, Any]:
        return {"mergeable": True}

    async def squash_merge(self, pr_id: str) -> dict[str, Any]:
        return {"merged": True, "merge_commit": {"oid": "abc1234def5678", "messageHeadline": "t"}}

    async def move_card(self, item_id: str, status: str) -> MoveOutcome:
        _ = item_id, status
        return MoveOutcome.ok()

    async def list_open_pull_requests(self, owner: str, repo: str, first: int = 50) -> list[dict[str, Any]]:
        self.list_calls += 1
        return self._open_prs

    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]:
        self.comments.append(subject_id)
        return {"comment": "OK"}

    async def close_pull_request(self, pr_id: str) -> dict[str, Any]:
        self.closed.append(pr_id)
        return {"closed": True}


_LOSER = {
    "id": "PR_LOSER",
    "number": 21,
    "url": "https://github.com/acme/repo/pull/21",
    "head_ref": "coordinare/PVTI_1/loser",
    "base_ref": "main",
}


@pytest.mark.asyncio
async def test_merge_pr_closes_superseded_prs_when_enabled() -> None:
    from coordinare.graph.nodes.merge_pr import merge_pr
    from coordinare.graph.state import initial_state

    github = _MergeGitHub([dict(_LOSER)])
    state = initial_state()
    state["github_service"] = github
    state["config"] = SimpleNamespace(
        orphan_pr=OrphanPrConfig(enabled=True, completion_reconciliation=True),
    )
    state["current_card"] = {
        "id": "PVTI_1",
        "pr_node_id": "PR_WINNER",
        "pr_url": "https://github.com/acme/repo/pull/22",
        "status": "IN_REVIEW",
    }

    result = await merge_pr(state)

    assert result["current_card"]["status"] == "DONE"
    assert github.closed == ["PR_LOSER"]
    assert github.list_calls == 1


@pytest.mark.asyncio
async def test_merge_pr_disabled_performs_no_reconciliation_reads() -> None:
    from coordinare.graph.nodes.merge_pr import merge_pr
    from coordinare.graph.state import initial_state

    github = _MergeGitHub([dict(_LOSER)])
    state = initial_state()
    state["github_service"] = github
    state["config"] = SimpleNamespace(orphan_pr=OrphanPrConfig())
    state["current_card"] = {
        "id": "PVTI_1",
        "pr_node_id": "PR_WINNER",
        "pr_url": "https://github.com/acme/repo/pull/22",
        "status": "IN_REVIEW",
    }

    await merge_pr(state)

    assert github.closed == []
    assert github.list_calls == 0


@pytest.mark.asyncio
async def test_check_board_calls_sweep_with_fresh_board(monkeypatch: pytest.MonkeyPatch) -> None:
    import coordinare.graph.nodes.check_board as check_board_module
    from coordinare.graph.state import initial_state

    github = SimpleNamespace(
        poll_board=AsyncMock(return_value={"snapshot": {"DONE": ["c1"]}}),
    )
    config = SimpleNamespace(
        max_concurrent_cards=1,
        orphan_pr=OrphanPrConfig(enabled=True, sweep=True),
    )
    state = initial_state()
    state["github_service"] = github
    state["config"] = config

    seen: list[tuple[Any, Any, Any, Any]] = []

    class StopSweepError(Exception):
        pass

    async def _spy(g: Any, board: Any, cfg: Any, active_card_ids: Any = None) -> list[Any]:
        seen.append((g, board, cfg, active_card_ids))
        raise StopSweepError

    monkeypatch.setattr(check_board_module, "run_orphan_pr_sweep", _spy)
    with pytest.raises(StopSweepError):
        await check_board_module.check_board(state)

    github.poll_board.assert_awaited_once()
    assert len(seen) == 1
    assert seen[0][0] is github
    assert seen[0][2] is config.orphan_pr
    assert seen[0][3] == set()


@pytest.mark.asyncio
async def test_check_board_sweep_disabled_never_reads_github(monkeypatch: pytest.MonkeyPatch) -> None:
    import coordinare.graph.nodes.check_board as check_board_module
    from coordinare.graph.state import initial_state

    github = SimpleNamespace(
        poll_board=AsyncMock(return_value={"snapshot": {"DONE": ["c1"]}}),
        list_open_pull_requests=AsyncMock(side_effect=AssertionError("must not read")),
    )
    state = initial_state()
    state["github_service"] = github
    state["config"] = SimpleNamespace(max_concurrent_cards=1)

    class StopAfterError(Exception):
        pass

    async def _checkpoint(*args: Any) -> None:
        raise StopAfterError

    monkeypatch.setattr(check_board_module, "_maybe_rebase_on_main_change", _checkpoint)
    with pytest.raises(StopAfterError):
        await check_board_module.check_board(state)

    github.poll_board.assert_awaited_once()
    github.list_open_pull_requests.assert_not_awaited()


# ---------------------------------------------------------------------------
# Review round 1 — Copilot findings (qualifier validation, live-session
# guard, merged=False gate, pagination, self-URL comment, bare-close guard)
# ---------------------------------------------------------------------------


def test_resolve_rejects_cross_repo_qualified_reference() -> None:
    cards = {
        "PVTI_1": {"id": "PVTI_1", "status": "DONE", "issue_number": 19, "title": "x"},
    }
    base = {
        "id": "PR_9", "number": 21, "url": "https://github.com/acme/repo/pull/21",
        "head_ref": "feature/misc",
    }
    attribution = resolve_pr_to_card(
        {**base, "body": "Fixes other/repo#19"}, cards, expected_qualifier="acme/repo",
    )
    assert attribution.card_item_id is None

    same_repo = resolve_pr_to_card(
        {**base, "body": "Fixes acme/repo#19"}, cards, expected_qualifier="acme/repo",
    )
    assert same_repo.card_item_id == "PVTI_1"

    unqualified = resolve_pr_to_card(
        {**base, "body": "Fixes #19"}, cards, expected_qualifier="acme/repo",
    )
    assert unqualified.card_item_id == "PVTI_1"


@pytest.mark.asyncio
async def test_sweep_skips_cards_with_live_sessions(tmp_path: Any) -> None:
    fake = FakeGitHubService(bare_repo_path=tmp_path, org="acme", project_name="repo")
    loser = fake.open_pr(
        issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/redo",
    )
    fake._prs[loser]["merged"] = True
    orphan = fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/loser")
    board = {
        "snapshot": {"DONE": ["PVTI_1"]},
        "titles": {"PVTI_1": "Demo"},
        "issue_numbers": {"PVTI_1": 1},
    }
    cfg = OrphanPrConfig(enabled=True, sweep=True)

    decisions = await run_orphan_pr_sweep(
        fake, board, cfg, active_card_ids={"PVTI_1"},
    )

    assert [d.outcome for d in decisions] == ["skipped-active"]
    assert fake._prs[orphan]["closed"] is False


@pytest.mark.asyncio
async def test_merge_pr_skips_reconciliation_when_merge_reported_not_merged() -> None:
    from coordinare.graph.nodes.merge_pr import merge_pr
    from coordinare.graph.state import initial_state

    class _NotMergedGitHub(_MergeGitHub):
        async def squash_merge(self, pr_id: str) -> dict[str, Any]:
            return {"merged": False}

    github = _NotMergedGitHub([dict(_LOSER)])
    state = initial_state()
    state["github_service"] = github
    state["config"] = SimpleNamespace(
        orphan_pr=OrphanPrConfig(enabled=True, completion_reconciliation=True),
    )
    state["current_card"] = {
        "id": "PVTI_1",
        "pr_node_id": "PR_WINNER",
        "pr_url": "https://github.com/acme/repo/pull/22",
        "status": "IN_REVIEW",
    }

    await merge_pr(state)

    assert github.closed == []
    assert github.list_calls == 0


@pytest.mark.asyncio
async def test_merge_pr_skips_reconciliation_when_board_move_refused() -> None:
    from coordinare.graph.nodes.merge_pr import merge_pr
    from coordinare.graph.state import initial_state

    class _RefusingBoard:
        async def move_card(self, item_id: str, status: str) -> MoveOutcome:
            _ = item_id, status
            return MoveOutcome.refused("workflow forbids")

    github = _MergeGitHub([dict(_LOSER)])
    state = initial_state()
    state["github_service"] = github
    state["board_provider"] = _RefusingBoard()
    state["config"] = SimpleNamespace(
        orphan_pr=OrphanPrConfig(enabled=True, completion_reconciliation=True),
    )
    state["current_card"] = {
        "id": "PVTI_1",
        "pr_node_id": "PR_WINNER",
        "pr_url": "https://github.com/acme/repo/pull/22",
        "status": "IN_REVIEW",
    }

    await merge_pr(state)

    assert github.closed == []
    assert github.list_calls == 0


@pytest.mark.asyncio
async def test_comment_failure_defers_close_to_next_cycle(tmp_path: Any) -> None:
    fake = FakeGitHubService(bare_repo_path=tmp_path, org="acme", project_name="repo")
    fake._prs["loser"] = {
        "id": "loser", "number": 21, "url": "https://fake/acme/repo/pull/21",
        "head_ref": "coordinare/PVTI_1/loser", "base_ref": "main",
        "body": "", "comments": [], "reviews": [], "merged": False, "closed": False,
    }

    async def _boom(subject_id: str, body: str) -> dict[str, Any]:
        raise RuntimeError("transient")

    fake.add_comment = _boom  # type: ignore[method-assign]
    card = {
        "id": "PVTI_1", "status": "DONE", "issue_number": 1, "title": "Demo",
        "pr_url": "https://fake/acme/repo/pull/22",
    }
    decisions = await close_superseded_prs(fake, card, [fake._prs["loser"]])

    assert len(decisions) == 1
    assert decisions[0].outcome == "skipped-comment-failed"
    assert decisions[0].closed_ok is False


@pytest.mark.asyncio
async def test_sweep_closes_board_pr_url_orphan_without_self_reference(tmp_path: Any) -> None:
    fake = FakeGitHubService(bare_repo_path=tmp_path, org="acme", project_name="repo")
    orphan = fake.open_pr(issue_item_id="PVTI_1", head_ref="coordinare/PVTI_1/loser")
    orphan_url = fake._prs[orphan]["url"]
    posted: list[str] = []

    async def _capture(subject_id: str, body: str) -> dict[str, Any]:
        posted.append(body)
        return {"comment": "OK"}

    fake.add_comment = _capture  # type: ignore[method-assign]
    board = {
        "snapshot": {"DONE": ["PVTI_1"]},
        "titles": {"PVTI_1": "Demo"},
        "issue_numbers": {"PVTI_1": 1},
        "pr_urls": {"PVTI_1": orphan_url},
    }
    cfg = OrphanPrConfig(enabled=True, sweep=True)
    decisions = await run_orphan_pr_sweep(fake, board, cfg, active_card_ids=set())

    assert [d.outcome for d in decisions] == ["closed"]
    assert fake._prs[orphan]["closed"] is True
    assert len(posted) == 1
    assert "completed via" not in posted[0]


@pytest.mark.asyncio
async def test_list_open_pull_requests_paginates_with_cap() -> None:
    from coordinare.services.github import GitHubService

    service = GitHubService(token="test", org="acme", project_number=1)

    def _page(nums: list[int], has_next: bool, cursor: str = "CUR") -> dict[str, Any]:
        return {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {
                            "id": f"PR_{n}",
                            "number": n,
                            "url": f"https://github.com/acme/repo/pull/{n}",
                            "headRefName": f"coordinare/PVTI_{n}/x",
                            "body": "",
                        }
                        for n in nums
                    ],
                    "pageInfo": {"endCursor": cursor, "hasNextPage": has_next},
                },
            },
        }

    pages = [
        _page(list(range(100, 200)), True),
        _page(list(range(200, 290)), False),
    ]
    calls: list[dict[str, Any]] = []

    async def _execute(_query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append(variables)
        return pages[len(calls) - 1]

    service._guarded_execute = _execute  # type: ignore[method-assign]
    prs = await service.list_open_pull_requests("acme", "repo")

    assert [p["number"] for p in prs] == list(range(100, 290))
    assert len(calls) == 2
    assert "after" not in calls[0]
    assert calls[1]["after"] == "CUR"


@pytest.mark.asyncio
async def test_list_open_pull_requests_warns_on_truncation() -> None:
    from coordinare.services.github import GitHubService

    service = GitHubService(token="test", org="acme", project_number=1)
    truncated_page = {
        "repository": {
            "pullRequests": {
                "nodes": [
                    {"id": f"PR_{n}", "number": n, "url": f"u{n}", "headRefName": "b", "body": ""}
                    for n in range(300, 400)
                ],
                "pageInfo": {"endCursor": "MORE", "hasNextPage": True},
            },
        },
    }

    async def _execute(_query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return truncated_page

    service._guarded_execute = _execute  # type: ignore[method-assign]
    prs = await service.list_open_pull_requests("acme", "repo")

    assert len(prs) == 200  # 2 pages x 100 = cap
