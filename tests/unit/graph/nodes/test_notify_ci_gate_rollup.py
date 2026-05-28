"""Unit tests for CI-gate PR rollup comments emitted by notify.py (spec 075).

Covers T052 (signature determinism), T053 (PR-comment dedup), and
T054 (per-verdict gating).
"""

from __future__ import annotations

from typing import Any

import pytest

from coordinare.graph.nodes.notify import (
    CI_GATE_ROLLUP_MARKER_PREFIX,
    _ci_gate_signature,
    notify,
)
from coordinare.graph.state import initial_state
from tests.utils.fake_notification import FakeNotificationService


class _FakeGitHub:
    def __init__(self, existing_comments: list[dict[str, Any]] | None = None) -> None:
        self.existing = list(existing_comments or [])
        self.added: list[tuple[str, str]] = []

    async def get_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return list(self.existing)

    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]:
        self.added.append((subject_id, body))
        return {"id": "C1"}


def _decision(
    *,
    verdict: str = "bounce",
    head_sha: str = "a" * 40,
    required: list[str] | None = None,
    failed: list[dict[str, Any]] | None = None,
    pending: list[str] | None = None,
    source: str = "all_head_checks",
    bounce_count: int = 1,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "head_sha": head_sha,
        "required_checks": sorted(required or ["lint", "unit-tests"]),
        "failed_checks": failed or [{"name": "unit-tests", "conclusion": "failure"}],
        "pending_checks": pending or [],
        "resolver_source": source,
        "bounce_count_after": bounce_count,
        "decided_at": "2026-05-28T00:00:00.000Z",
    }


# ---------------------------------------------------------------------------
# T052 — signature determinism
# ---------------------------------------------------------------------------


def test_ci_gate_rollup_dedup_signature() -> None:
    """Identical (head_sha, verdict, required, failed-names) → same signature.

    Changing any of those four inputs flips the signature.
    """
    base = _decision()
    same = _decision()
    assert _ci_gate_signature(base) == _ci_gate_signature(same)

    # decided_at is not part of the signature
    drifted_time = dict(base, decided_at="2099-01-01T00:00:00.000Z")
    assert _ci_gate_signature(drifted_time) == _ci_gate_signature(base)

    diff_head = dict(base, head_sha="b" * 40)
    diff_verdict = dict(base, verdict="hold", failed_checks=[])
    diff_required = dict(base, required_checks=["build", "lint"])
    diff_failed = dict(
        base,
        failed_checks=[{"name": "integration", "conclusion": "failure"}],
    )
    seen = {
        _ci_gate_signature(base),
        _ci_gate_signature(diff_head),
        _ci_gate_signature(diff_verdict),
        _ci_gate_signature(diff_required),
        _ci_gate_signature(diff_failed),
    }
    assert len(seen) == 5


# ---------------------------------------------------------------------------
# T053 — PR-comment dedup
# ---------------------------------------------------------------------------


def _state_with_session(
    *,
    decision: dict[str, Any],
    github: _FakeGitHub,
) -> dict[str, Any]:
    state = initial_state()
    state["notification_service"] = FakeNotificationService()
    state["github_service"] = github
    state["current_card"] = {
        "id": "PROJ-1",
        "title": "Card",
        "status": "IN_PROGRESS",
        "pr_node_id": "PR_kw1",
        "issue_number": 42,
    }
    state["phase"] = "monitoring_performer"
    state["active_sessions"] = {
        "PROJ-1": {"latest_ci_gate_decision": decision},
    }
    return state


@pytest.mark.asyncio
async def test_ci_gate_comment_dedup() -> None:
    decision = _decision(verdict="bounce")
    sig = _ci_gate_signature(decision)
    marker = f"{CI_GATE_ROLLUP_MARKER_PREFIX}{sig} -->"

    # Existing comment carries the marker → must NOT post again.
    gh_existing = _FakeGitHub(existing_comments=[{"body": f"prior\n{marker}\n..."}])
    await notify(_state_with_session(decision=decision, github=gh_existing))
    assert gh_existing.added == []

    # No existing comments → posts once.
    gh_empty = _FakeGitHub(existing_comments=[])
    await notify(_state_with_session(decision=decision, github=gh_empty))
    assert len(gh_empty.added) == 1
    assert marker in gh_empty.added[0][1]


@pytest.mark.asyncio
async def test_ci_gate_comment_dedup_session_signature() -> None:
    """Same signature already stamped on session → skip even without PR scan."""
    decision = _decision(verdict="bounce")
    sig = _ci_gate_signature(decision)
    gh = _FakeGitHub(existing_comments=[])
    state = _state_with_session(decision=decision, github=gh)
    state["active_sessions"]["PROJ-1"]["ci_gate_rollup_signature"] = sig
    await notify(state)
    assert gh.added == []


# ---------------------------------------------------------------------------
# T054 — per-verdict gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_gate_comment_per_verdict_pass_silent() -> None:
    decision = _decision(verdict="pass", failed=[], bounce_count=0)
    gh = _FakeGitHub(existing_comments=[])
    await notify(_state_with_session(decision=decision, github=gh))
    assert gh.added == []


@pytest.mark.asyncio
async def test_ci_gate_comment_per_verdict_hold_posts() -> None:
    decision = _decision(
        verdict="hold",
        failed=[],
        pending=["unit-tests"],
        bounce_count=0,
    )
    gh = _FakeGitHub(existing_comments=[])
    await notify(_state_with_session(decision=decision, github=gh))
    assert len(gh.added) == 1
    assert "HOLD" in gh.added[0][1]


@pytest.mark.asyncio
async def test_ci_gate_comment_per_verdict_bounce_posts() -> None:
    decision = _decision(verdict="bounce", bounce_count=2)
    gh = _FakeGitHub(existing_comments=[])
    await notify(_state_with_session(decision=decision, github=gh))
    assert len(gh.added) == 1
    assert "BOUNCE" in gh.added[0][1]


@pytest.mark.asyncio
async def test_ci_gate_comment_per_verdict_escalate_posts() -> None:
    decision = _decision(verdict="escalate", bounce_count=3)
    gh = _FakeGitHub(existing_comments=[])
    await notify(_state_with_session(decision=decision, github=gh))
    assert len(gh.added) == 1
    assert "ESCALATE" in gh.added[0][1]
