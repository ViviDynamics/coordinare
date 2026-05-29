"""Verifies that the in-flight guard wired into ``dispatch_performer``
(spec 076 T034, T061) prevents a duplicate dispatch when a session for
the same (card_id, performer_stage) is already live.
"""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state


class _LiveService:
    """A service that says any session is alive."""

    def has_live_session(self, session_id: str) -> bool:
        return True

    async def dispatch_card(self, *args, **kwargs):  # pragma: no cover
        msg = "dispatch_card MUST NOT be called when the in-flight guard refuses"
        raise AssertionError(msg)


@pytest.mark.asyncio
async def test_guard_refuses_dispatch_when_session_live() -> None:
    """FR-001: when agent_dispatch.session_id is set AND service reports
    live, dispatch_performer MUST NOT spawn a fresh dispatch."""
    state = initial_state()
    state["current_card"] = {"id": "PVTI_X", "title": "card", "status": "IN_PROGRESS"}
    state["performer_stage"] = "implementing"
    state["agent_dispatch"] = {"session_id": "uuid-already-live"}
    state["performer_services"] = {"implementing": _LiveService()}

    result = await dispatch_performer(state)

    # State returned unmodified (the guard short-circuits before any
    # state mutation in the dispatch body).
    assert result is state
    assert result.get("agent_dispatch") == {"session_id": "uuid-already-live"}
    # `_LiveService.dispatch_card` raised AssertionError if it had been
    # called — passing reaching this line means the guard worked.


@pytest.mark.asyncio
async def test_guard_allows_dispatch_when_no_session() -> None:
    """The guard MUST NOT block dispatch when agent_dispatch is empty.

    We don't fully exercise dispatch here (no github_service etc.) —
    the test just confirms the function does NOT short-circuit on the
    guard path when there's nothing in flight.  The body will then take
    its own missing-prerequisites branch.
    """
    state = initial_state()
    state["current_card"] = {"id": "PVTI_FRESH", "title": "fresh", "status": "TODO"}
    state["performer_stage"] = "implementing"
    state["agent_dispatch"] = {}  # nothing in flight
    state["performer_services"] = {"implementing": _LiveService()}
    # No github_service → body will hit missing-prerequisites branch and set phase=idle

    result = await dispatch_performer(state)

    # Body's missing-prereq branch fired (proves the guard let us through)
    assert result.get("phase") == "idle"


def test_owner_repo_from_pr_url() -> None:
    """Helper for parsing GitHub PR URLs.  Returns (owner, repo) or
    (None, None) on malformed inputs."""
    from coordinare.graph.nodes.dispatch_performer import _owner_repo_from_pr_url

    assert _owner_repo_from_pr_url("https://github.com/foo/bar/pull/123") == ("foo", "bar")
    assert _owner_repo_from_pr_url("https://github.com/foo/bar/pull/1") == ("foo", "bar")
    # Malformed inputs return None
    assert _owner_repo_from_pr_url("not a url") == (None, None)
    assert _owner_repo_from_pr_url("https://gitlab.com/foo/bar/pull/1") == (None, None)
    assert _owner_repo_from_pr_url(None) == (None, None)
    assert _owner_repo_from_pr_url(12345) == (None, None)
    assert _owner_repo_from_pr_url("https://github.com/foo") == (None, None)


@pytest.mark.asyncio
async def test_multi_pr_check_crash_does_not_block_dispatch() -> None:
    """A crashing multi-PR detection MUST NOT prevent dispatch (that
    would re-introduce a class of dispatch failures).  The crash is
    logged via dispatch_performer.multi_pr_divergence_check_crashed."""
    from coordinare.graph.nodes.dispatch_performer import dispatch_performer
    from coordinare.graph.state import initial_state

    class _BrokenGithub:
        async def list_prs_by_branch_prefix(self, *a, **kw):
            raise RuntimeError("github exploded")

    class _DeadSvc:
        def has_live_session(self, session_id):
            return False

        async def dispatch_card(self, *a, **kw):
            return {"status": "ok", "session_id": "fresh", "job_id": "j", "accepted": True}

    state = initial_state()
    state["current_card"] = {
        "id": "PVTI_X",
        "title": "card",
        "status": "IN_PROGRESS",
        "pr_url": "https://github.com/x/y/pull/1",
    }
    state["performer_stage"] = "implementing"
    state["agent_dispatch"] = {}
    state["performer_services"] = {"implementing": _DeadSvc()}
    state["github_service"] = _BrokenGithub()

    result = await dispatch_performer(state)

    # The body's missing-prereq branch fires (because github isn't a real
    # github service); what matters is we reached the body without crashing.
    assert result.get("phase") == "idle"


@pytest.mark.asyncio
async def test_guard_allows_dispatch_when_session_dead() -> None:
    """A stale session_id (service reports not live) MUST NOT block
    dispatch.  The stale-session reconciliation path is a separate
    concern (T055/T057) and runs through check_board, not the guard."""

    class _DeadService:
        def has_live_session(self, session_id: str) -> bool:
            return False

        async def dispatch_card(self, *args, **kwargs):  # pragma: no cover
            return {"status": "ok", "session_id": "fresh-uuid", "job_id": "job-1", "accepted": True}

    state = initial_state()
    state["current_card"] = {"id": "PVTI_STALE", "title": "stale", "status": "IN_PROGRESS"}
    state["performer_stage"] = "implementing"
    state["agent_dispatch"] = {"session_id": "old-stale-uuid"}
    state["performer_services"] = {"implementing": _DeadService()}

    result = await dispatch_performer(state)

    # Guard allowed; body's missing-prereq branch fired (no github_service)
    assert result.get("phase") == "idle"
