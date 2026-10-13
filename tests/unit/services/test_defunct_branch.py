"""89 — defunct branch cleanup after the conductor closes a PR."""
from __future__ import annotations

import pytest

from coordinare.config import ProjectConfiguration
from coordinare.services.defunct_branch import delete_defunct_branch, resolve_branch

_BASE = {
    "project_name": "Demo",
    "github_org": "acme",
    "github_project_number": 1,
    "github_token": "tok",
    "human_reviewers": ["alice"],
}


# ---------------------------------------------------------------------------
# Config — opt-in, default off (SC-003 pattern)
# ---------------------------------------------------------------------------


def test_config_default_off() -> None:
    cfg = ProjectConfiguration(**_BASE)
    assert cfg.delete_defunct_branches is False


# ---------------------------------------------------------------------------
# resolve_branch — first non-empty string among candidates
# ---------------------------------------------------------------------------


def test_resolve_branch_first_non_empty() -> None:
    assert resolve_branch("", None, "coordinare/PVTI_1/x", "fallback") == "coordinare/PVTI_1/x"


def test_resolve_branch_all_empty() -> None:
    assert resolve_branch("", None) == ""


# ---------------------------------------------------------------------------
# delete_defunct_branch — non-raising, decision-logged
# ---------------------------------------------------------------------------


class _RecordingGitHub:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_branch(self, branch_name: str) -> None:
        self.deleted.append(branch_name)


class _RaisingGitHub:
    def __init__(self) -> None:
        self.attempts = 0

    async def delete_branch(self, branch_name: str) -> None:
        self.attempts += 1
        raise RuntimeError("ref already gone")


class _OpenPRGitHub(_RecordingGitHub):
    """Double with the branch_has_open_pr capability (like the real service)."""

    def __init__(self, result: bool | None) -> None:
        super().__init__()
        self._open_pr_result = result
        self.open_pr_probes = 0

    async def branch_has_open_pr(self, branch_name: str) -> bool | None:
        self.open_pr_probes += 1
        return self._open_pr_result


async def _delete(github: object, branch: str = "coordinare/PVTI_1/x") -> bool:
    return await delete_defunct_branch(
        github, branch, card_id="PVTI_1", source="merge", enabled=True,
        head_repo="acme/repo", expected_repo="acme/repo",
    )


@pytest.mark.asyncio
async def test_delete_enabled_removes_branch() -> None:
    github = _RecordingGitHub()
    ok = await _delete(github)
    assert ok is True
    assert github.deleted == ["coordinare/PVTI_1/x"]


@pytest.mark.asyncio
async def test_delete_disabled_is_noop() -> None:
    github = _RecordingGitHub()
    ok = await delete_defunct_branch(
        github, "coordinare/PVTI_1/x", card_id="PVTI_1", source="merge", enabled=False,
        head_repo="acme/repo", expected_repo="acme/repo",
    )
    assert ok is False
    assert github.deleted == []


@pytest.mark.asyncio
async def test_delete_empty_branch_is_noop() -> None:
    github = _RecordingGitHub()
    ok = await delete_defunct_branch(
        github, "", card_id="PVTI_1", source="merge", enabled=True,
        head_repo="acme/repo", expected_repo="acme/repo",
    )
    assert ok is False
    assert github.deleted == []


@pytest.mark.asyncio
async def test_delete_failure_is_swallowed() -> None:
    """delete_branch is contractually non-raising; doubles that raise are
    still swallowed so a cleanup failure never breaks the merge path."""
    github = _RaisingGitHub()
    ok = await _delete(github)
    assert ok is False
    assert github.attempts == 1


@pytest.mark.asyncio
async def test_delete_without_delete_branch_method_is_noop() -> None:
    """Doubles/protocols without delete_branch must not explode the caller."""

    class _NoDelete:
        pass

    ok = await delete_defunct_branch(
        _NoDelete(), "coordinare/PVTI_1/x", card_id="PVTI_1", source="merge", enabled=True,
        head_repo="acme/repo", expected_repo="acme/repo",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# 89 review fixes — head-repo identity + live-PR preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_fork_head_repo_mismatch_is_skipped() -> None:
    """A fork PR's head ref lives in another repository; deleting by name in
    the base repo could remove an unrelated same-named branch."""
    github = _RecordingGitHub()
    ok = await delete_defunct_branch(
        github, "coordinare/PVTI_1/x", card_id="PVTI_1", source="merge", enabled=True,
        head_repo="other/repo", expected_repo="acme/repo",
    )
    assert ok is False
    assert github.deleted == []


@pytest.mark.asyncio
async def test_delete_unknown_head_repo_is_skipped() -> None:
    """No head-repo identity from the inventory: preserve the branch."""
    github = _RecordingGitHub()
    ok = await delete_defunct_branch(
        github, "coordinare/PVTI_1/x", card_id="PVTI_1", source="orphan", enabled=True,
        head_repo="", expected_repo="acme/repo",
    )
    assert ok is False
    assert github.deleted == []


@pytest.mark.asyncio
async def test_delete_unknown_configured_repo_is_skipped() -> None:
    """No configured-repo identity: cannot verify, so preserve."""
    github = _RecordingGitHub()
    ok = await delete_defunct_branch(
        github, "coordinare/PVTI_1/x", card_id="PVTI_1", source="orphan", enabled=True,
        head_repo="acme/repo", expected_repo="",
    )
    assert ok is False
    assert github.deleted == []


@pytest.mark.asyncio
async def test_delete_preserved_when_another_open_pr_uses_branch() -> None:
    """A branch can back more than one PR: only delete when no live PR uses it."""
    github = _OpenPRGitHub(True)
    ok = await _delete(github)
    assert ok is False
    assert github.open_pr_probes == 1
    assert github.deleted == []


@pytest.mark.asyncio
async def test_delete_preserved_when_open_pr_probe_is_unknown() -> None:
    """An indeterminate open-PR probe (None) preserves the branch too."""
    github = _OpenPRGitHub(None)
    ok = await _delete(github)
    assert ok is False
    assert github.deleted == []


@pytest.mark.asyncio
async def test_delete_proceeds_when_no_open_pr_uses_branch() -> None:
    github = _OpenPRGitHub(False)
    ok = await _delete(github)
    assert ok is True
    assert github.deleted == ["coordinare/PVTI_1/x"]
