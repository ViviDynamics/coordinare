"""Spec 076 T114 — detect_multi_pr_divergence unit tests.

Covers FR-024: detection of > 1 open PR for a card matching the
canonical branch prefix.
"""
from __future__ import annotations

import pytest

from coordinare.services.dispatch_guard import detect_multi_pr_divergence


class _GithubStub:
    def __init__(self, prs: list[dict]) -> None:
        self._prs = prs
        self.calls: list[tuple[str, str, str]] = []

    async def list_prs_by_branch_prefix(self, owner, repo, prefix, *, state="OPEN", limit=20):
        self.calls.append((owner, repo, prefix))
        return [p for p in self._prs if p["head_ref"].startswith(prefix)]


def _state_with_card(card_id: str = "PVTI_X") -> dict:
    return {
        "active_sessions": {
            card_id: {
                "current_card": {"id": card_id, "title": "card"},
                "phase": "monitoring_performer",
            },
        },
        "current_card": {"id": card_id, "title": "card"},
    }


@pytest.mark.asyncio
async def test_no_prs_returns_none() -> None:
    state = _state_with_card()
    result = await detect_multi_pr_divergence(
        state, "PVTI_X",
        github_service=_GithubStub([]),
        owner="o", repo="r",
    )
    assert result is None


@pytest.mark.asyncio
async def test_one_pr_returns_none() -> None:
    state = _state_with_card()
    result = await detect_multi_pr_divergence(
        state, "PVTI_X",
        github_service=_GithubStub([
            {"number": 1, "url": "u1", "head_ref": "coordinare/PVTI_X/feat-a"},
        ]),
        owner="o", repo="r",
    )
    assert result is None


@pytest.mark.asyncio
async def test_two_prs_returns_divergence() -> None:
    """Today's exact incident: PR #133 and #148 both open under
    coordinare/PVTI_X/ → divergence detected."""
    state = _state_with_card()
    result = await detect_multi_pr_divergence(
        state, "PVTI_X",
        github_service=_GithubStub([
            {"number": 133, "url": "u1", "head_ref": "coordinare/PVTI_X/schema-old"},
            {"number": 148, "url": "u2", "head_ref": "coordinare/PVTI_X/feat-new"},
        ]),
        owner="o", repo="r",
    )
    assert result is not None
    assert result["card_id"] == "PVTI_X"
    assert sorted(result["pr_numbers"]) == [133, 148]
    assert result["canonical_branch_prefix"] == "coordinare/PVTI_X/"
    assert result["detected_at_trigger"] == "dispatch"
    assert result["response"] == "dispatch_refused"
    # Stashed on the session for audit
    assert state["active_sessions"]["PVTI_X"]["multi_pr_divergence"] == result


@pytest.mark.asyncio
async def test_three_prs_returns_divergence() -> None:
    state = _state_with_card()
    result = await detect_multi_pr_divergence(
        state, "PVTI_X",
        github_service=_GithubStub([
            {"number": 1, "url": "u1", "head_ref": "coordinare/PVTI_X/a"},
            {"number": 2, "url": "u2", "head_ref": "coordinare/PVTI_X/b"},
            {"number": 3, "url": "u3", "head_ref": "coordinare/PVTI_X/c"},
        ]),
        owner="o", repo="r",
    )
    assert result is not None
    assert len(result["pr_numbers"]) == 3


@pytest.mark.asyncio
async def test_prs_for_different_card_dont_trigger() -> None:
    """Only PRs with the card's prefix count.  A PR for PVTI_OTHER
    shouldn't trigger divergence on PVTI_X."""
    state = _state_with_card()
    result = await detect_multi_pr_divergence(
        state, "PVTI_X",
        github_service=_GithubStub([
            {"number": 1, "url": "u1", "head_ref": "coordinare/PVTI_X/feat-a"},
            {"number": 2, "url": "u2", "head_ref": "coordinare/PVTI_OTHER/feat-b"},
        ]),
        owner="o", repo="r",
    )
    # Only one matches PVTI_X prefix → no divergence
    assert result is None


@pytest.mark.asyncio
async def test_github_error_returns_none_not_exception() -> None:
    """If GitHub query raises, detection MUST return None — never
    propagate a transport error into the dispatch path."""

    class _BrokenGithub:
        async def list_prs_by_branch_prefix(self, *a, **kw):
            raise RuntimeError("github down")

    state = _state_with_card()
    result = await detect_multi_pr_divergence(
        state, "PVTI_X",
        github_service=_BrokenGithub(),
        owner="o", repo="r",
    )
    assert result is None


@pytest.mark.asyncio
async def test_trigger_restart_sets_card_blocked_response() -> None:
    """trigger=restart → response=card_blocked (not dispatch_refused)."""
    state = _state_with_card()
    result = await detect_multi_pr_divergence(
        state, "PVTI_X",
        github_service=_GithubStub([
            {"number": 1, "url": "u1", "head_ref": "coordinare/PVTI_X/a"},
            {"number": 2, "url": "u2", "head_ref": "coordinare/PVTI_X/b"},
        ]),
        owner="o", repo="r",
        trigger="restart",
    )
    assert result is not None
    assert result["detected_at_trigger"] == "restart"
    assert result["response"] == "card_blocked"


@pytest.mark.asyncio
async def test_missing_github_service_returns_none() -> None:
    state = _state_with_card()
    result = await detect_multi_pr_divergence(state, "PVTI_X")
    assert result is None
