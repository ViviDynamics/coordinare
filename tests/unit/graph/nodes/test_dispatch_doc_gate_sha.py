"""Spec 125 — documenting gate keyed to the last documentation pass (D1-D5)
and the shared PR-data fetch (F1/F2).

Contract: specs/125-stage-verdict-memory/contracts/skip-decision.md
"""
from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state


class _Service:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def check_health(self) -> dict[str, Any]:
        return {"status": "accepted"}

    async def dispatch_card(
        self, card_context: dict[str, Any], workspace_info: Any = None
    ) -> dict[str, Any]:
        self.dispatched.append(card_context)
        return {"status": "accepted", "session_id": "sess-1"}


class _GitHub:
    def __init__(
        self,
        *,
        head_ref_oid: str = "head777",
        compare_files: list[str] | None = None,
        compare_exc: Exception | None = None,
        diff_files: list[str] | None = None,
        diff_exc: Exception | None = None,
    ) -> None:
        self._head_ref_oid = head_ref_oid
        self._compare_files = compare_files if compare_files is not None else []
        self._compare_exc = compare_exc
        self._diff_files = diff_files if diff_files is not None else ["src/app.py"]
        self._diff_exc = diff_exc
        self.compare_calls: list[tuple[str, str]] = []
        self.get_pr_diff_calls: list[str] = []
        self.mergeability_calls: list[str] = []

    async def check_mergeability(self, pr_id: str) -> dict[str, Any]:
        self.mergeability_calls.append(pr_id)
        return {"head_ref_oid": self._head_ref_oid, "mergeable": True}

    async def compare_changed_files(
        self, pr_url: str, base_sha: str, head_sha: str
    ) -> list[str]:
        self.compare_calls.append((base_sha, head_sha))
        if self._compare_exc is not None:
            raise self._compare_exc
        return list(self._compare_files)

    async def get_pr_diff(self, pr_url: str) -> tuple[str, list[str]]:
        self.get_pr_diff_calls.append(pr_url)
        if self._diff_exc is not None:
            raise self._diff_exc
        raw = "".join(
            f"diff --git a/{p} b/{p}\n+x\n" for p in self._diff_files
        )
        return raw, list(self._diff_files)

    async def move_card(self, item_id: str, status: str) -> None:
        return None


_CARD = {
    "id": "ITEM_DOC",
    "status": "TODO",
    "pr_url": "https://github.com/acme/repo/pull/9",
    "pr_node_id": "PR_node_9",
}

# A prior documenting pass at S_doc; current live head differs so the
# verdict-cache does NOT skip and the SHA-keyed gate decides.
_DOC_VERDICT = {
    "head_sha": "docpass000",
    "verdict": "docs_committed",
    "recorded_at": "2026-07-04T10:00:00+00:00",
}


def _state(github: _GitHub, svc: _Service, **overrides: Any) -> dict[str, Any]:
    state = initial_state()
    state["current_card"] = dict(_CARD)
    state["github_service"] = github
    state["performer_services"] = {"documenting": svc, "closing_review": _Service()}
    state["performer_stage"] = "documenting"
    state["lifecycle_sequence"] = ["documenting", "closing_review"]
    state["stage_verdicts"] = {"documenting": dict(_DOC_VERDICT)}
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# D1/D2 — compare between last documented SHA and live head decides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_only_delta_since_last_pass_skips() -> None:
    svc = _Service()
    github = _GitHub(compare_files=["src/app.py", "spec/app_spec.rb"])
    state = _state(github, svc)

    with capture_logs() as logs:
        await dispatch_performer(state)

    assert svc.dispatched == []
    assert state["performer_stage"] == "closing_review"
    assert github.compare_calls == [("docpass000", "head777")]
    skips = [
        e for e in logs
        if e.get("event") == "dispatch_performer.stage_skipped"
        and e.get("reason") == "no_doc_changes_since_last_pass"
    ]
    assert len(skips) == 1
    # The whole-PR fallback fetch never ran (D1 answered the question).
    assert github.get_pr_diff_calls == []


@pytest.mark.asyncio
async def test_docs_delta_since_last_pass_dispatches() -> None:
    svc = _Service()
    github = _GitHub(compare_files=["docs/wiki/setup.md", "src/app.py"])
    state = _state(github, svc)

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1


# ---------------------------------------------------------------------------
# D3 — compare failure falls back to the 123 whole-PR gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_failure_falls_back_to_whole_pr_gate_skip() -> None:
    svc = _Service()
    github = _GitHub(
        compare_exc=RuntimeError("compare truncated"),
        diff_files=["src/app.py"],  # whole PR touches no docs/ -> 123 skip
    )
    state = _state(github, svc)

    with capture_logs() as logs:
        await dispatch_performer(state)

    assert svc.dispatched == []
    assert [
        e for e in logs
        if e.get("event") == "dispatch_performer.stage_skipped"
        and e.get("reason") == "no_doc_changes"
    ]


@pytest.mark.asyncio
async def test_compare_failure_with_docs_in_pr_dispatches() -> None:
    svc = _Service()
    github = _GitHub(
        compare_exc=RuntimeError("boom"),
        diff_files=["docs/wiki/setup.md"],
    )
    state = _state(github, svc)

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1


# ---------------------------------------------------------------------------
# D4 — no prior doc pass: 123 whole-PR gate semantics unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_prior_pass_uses_whole_pr_gate() -> None:
    svc = _Service()
    github = _GitHub(diff_files=["src/app.py"])
    state = _state(github, svc, stage_verdicts={})

    await dispatch_performer(state)

    assert svc.dispatched == []
    assert github.compare_calls == []


# ---------------------------------------------------------------------------
# D5 — everything unavailable: dispatch (never skip on an unknown diff)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_and_diff_both_failing_dispatches() -> None:
    svc = _Service()
    github = _GitHub(
        compare_exc=RuntimeError("boom"),
        diff_exc=RuntimeError("boom"),
    )
    state = _state(github, svc)

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1


# ---------------------------------------------------------------------------
# D0 — pending relay feedback vetoes the doc gates (explicit work queued)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_feedback_dispatches_documenting() -> None:
    svc = _Service()
    github = _GitHub(compare_files=["src/app.py"], diff_files=["src/app.py"])
    state = _state(
        github, svc,
        relay_feedback=[{"body": "update the setup doc", "author_login": "coordinare"}],
    )

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1


# ---------------------------------------------------------------------------
# F1/F2 — one get_pr_diff per dispatch evaluation, shared with injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_diff_fetch_serves_gate_and_injection() -> None:
    # No prior pass -> whole-PR gate needs changed_files; docs present -> gate
    # dispatches; tech_writer is a diff-review role -> prompt injection needs
    # the raw diff. Both must come from ONE get_pr_diff call.
    svc = _Service()
    github = _GitHub(diff_files=["docs/wiki/setup.md", "src/app.py"])
    state = _state(github, svc, stage_verdicts={})

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert len(github.get_pr_diff_calls) == 1
    assert "docs/wiki/setup.md" in svc.dispatched[0].get("pr_diff", "")


@pytest.mark.asyncio
async def test_diff_fetch_failure_omits_injection_and_dispatches() -> None:
    svc = _Service()
    github = _GitHub(diff_exc=RuntimeError("boom"))
    state = _state(github, svc, stage_verdicts={})

    await dispatch_performer(state)

    assert len(svc.dispatched) == 1
    assert "pr_diff" not in svc.dispatched[0]
