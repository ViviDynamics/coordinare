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


@pytest.mark.asyncio
async def test_delete_enabled_removes_branch() -> None:
    github = _RecordingGitHub()
    ok = await delete_defunct_branch(
        github, "coordinare/PVTI_1/x", card_id="PVTI_1", source="merge", enabled=True,
    )
    assert ok is True
    assert github.deleted == ["coordinare/PVTI_1/x"]


@pytest.mark.asyncio
async def test_delete_disabled_is_noop() -> None:
    github = _RecordingGitHub()
    ok = await delete_defunct_branch(
        github, "coordinare/PVTI_1/x", card_id="PVTI_1", source="merge", enabled=False,
    )
    assert ok is False
    assert github.deleted == []


@pytest.mark.asyncio
async def test_delete_empty_branch_is_noop() -> None:
    github = _RecordingGitHub()
    ok = await delete_defunct_branch(
        github, "", card_id="PVTI_1", source="merge", enabled=True,
    )
    assert ok is False
    assert github.deleted == []


@pytest.mark.asyncio
async def test_delete_failure_is_swallowed() -> None:
    """delete_branch is contractually non-raising; doubles that raise are
    still swallowed so a cleanup failure never breaks the merge path."""
    github = _RaisingGitHub()
    ok = await delete_defunct_branch(
        github, "coordinare/PVTI_1/x", card_id="PVTI_1", source="orphan", enabled=True,
    )
    assert ok is False
    assert github.attempts == 1


@pytest.mark.asyncio
async def test_delete_without_delete_branch_method_is_noop() -> None:
    """Doubles/protocols without delete_branch must not explode the caller."""

    class _NoDelete:
        pass

    ok = await delete_defunct_branch(
        _NoDelete(), "coordinare/PVTI_1/x", card_id="PVTI_1", source="merge", enabled=True,
    )
    assert ok is False
