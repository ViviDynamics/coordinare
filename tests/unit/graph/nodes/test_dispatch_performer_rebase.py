"""097/555 — pre-dispatch divergence guard in dispatch_performer.

Tests `_pre_dispatch_rebase_guard` directly (returns True=proceed-with-dispatch /
False=skip-this-cycle and mutates state), plus a wiring test that the dispatch
node skips `_dispatch_performer_body` when the guard returns False.

555: the guard never rebases. On divergence it queues an instruction for the
implementer; every "cannot tell" path dispatches normally (fail open).

NOTE: the guard sources the branch from the PR (`head_ref_name`), NOT
`workspace_branch` (which is unset at pre-dispatch time) — so the test state does
NOT set workspace_branch; the conflicting verdicts carry a `head_ref_name`.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from coordinare.graph.nodes.dispatch_performer import _pre_dispatch_rebase_guard

_BRANCH = "coordinare/item-2"
_FETCH = "coordinare.services.rebase.fetch_main_sha"


class _GH:
    def __init__(self, verdict: dict, *, pr_state: str = "OPEN") -> None:
        self._verdict = verdict
        self._pr_state = pr_state
        self.merge_calls = 0

    async def _current_token(self) -> str:
        return "tok"

    async def check_mergeability(self, pr_id: str) -> dict:
        self.merge_calls += 1
        return self._verdict

    async def get_pr_review_context(self, pr_id: str) -> dict:
        if self._pr_state == "ERROR":
            raise RuntimeError("lifecycle outage")
        return {"state": self._pr_state}


def _state(verdict: dict | None, *, pr: bool = True, marker: dict | None = None,
           relay: list | None = None, stage: str = "implementing",
           enabled: bool = True, pr_state: str = "OPEN") -> dict:
    card = {"id": "ITEM_2"}
    if pr:
        card.update({"pr_url": "https://github.com/acme/repo/pull/2", "pr_node_id": "PR_NODE_2"})
    s: dict = {
        "current_card": card,
        "phase": "dispatching",  # NOTE: no workspace_branch — realistic pre-dispatch
        "performer_stage": stage,
        "config": SimpleNamespace(
            github_org="acme", project_name="repo",
            implementer_rebase=SimpleNamespace(enabled=enabled),
        ),
        "github_service": _GH(verdict or {}, pr_state=pr_state),
        "last_known_main_sha": "main-sha-0",
    }
    if marker is not None:
        s["last_rebase_attempt"] = marker
    if relay is not None:
        s["relay_feedback"] = relay
    return s


def _conflicting(head: str = "hX") -> dict:
    return {"mergeable_raw": "CONFLICTING", "merge_state_status": "DIRTY",
            "head_ref_oid": head, "head_ref_name": _BRANCH}


# --- opt-in and prerequisites ------------------------------------------------- #


@pytest.mark.asyncio
async def test_disabled_never_classifies_and_proceeds() -> None:
    """555 FR-008: default-off — a conflicting branch dispatches normally."""
    st = _state(_conflicting(), enabled=False)
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert st["github_service"].merge_calls == 0
    assert not st.get("relay_feedback") and "last_rebase_attempt" not in st


@pytest.mark.asyncio
async def test_config_without_the_key_is_disabled() -> None:
    st = _state(_conflicting())
    st["config"] = SimpleNamespace(github_org="acme", project_name="repo")
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert st["github_service"].merge_calls == 0


@pytest.mark.asyncio
async def test_no_pr_skips_guard_and_proceeds() -> None:
    """A card with no open PR (first run) → guard N/A, dispatch proceeds."""
    st = _state(None, pr=False)
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert st["github_service"].merge_calls == 0


@pytest.mark.asyncio
async def test_relay_feedback_dispatch_is_let_through() -> None:
    """A feedback-driven dispatch (incl. the divergence instruction) proceeds
    without the guard intercepting — so the resolution path is never starved."""
    st = _state(_conflicting(), relay=[{"body": "**Branch out of date with main** ..."}])
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert st["github_service"].merge_calls == 0  # not even consulted


@pytest.mark.asyncio
async def test_current_branch_proceeds_untouched() -> None:
    st = _state({"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN",
                 "head_ref_oid": "h0", "head_ref_name": _BRANCH})
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert not st.get("relay_feedback") and "last_rebase_attempt" not in st


# --- fail open (555 FR-005) --------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_mergeability_proceeds() -> None:
    """Was a hold in 097. An uncomputed verdict must never wedge dispatch."""
    st = _state({"mergeable_raw": "UNKNOWN", "merge_state_status": "UNKNOWN",
                 "head_ref_oid": "h0", "head_ref_name": _BRANCH})
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert not st.get("relay_feedback") and "last_rebase_attempt" not in st


@pytest.mark.asyncio
async def test_missing_head_oid_proceeds() -> None:
    st = _state({"mergeable_raw": "CONFLICTING", "head_ref_oid": "", "head_ref_name": _BRANCH})
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert not st.get("relay_feedback")


@pytest.mark.asyncio
async def test_mergeability_lookup_error_proceeds() -> None:
    st = _state(_conflicting())
    st["github_service"].check_mergeability = AsyncMock(side_effect=RuntimeError("transport"))
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert not st.get("relay_feedback") and "last_rebase_attempt" not in st


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", None])
async def test_main_unknown_without_credentials_proceeds(token) -> None:
    """The garden shape: no usable credential, main never learned → dispatch."""
    st = _state(_conflicting())
    st["last_known_main_sha"] = None
    st["github_service"]._current_token = AsyncMock(return_value=token)
    with patch(_FETCH, new=AsyncMock()) as fetch:
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    fetch.assert_not_called()
    assert st["github_service"].merge_calls == 0


@pytest.mark.asyncio
async def test_main_unreadable_proceeds() -> None:
    st = _state(_conflicting())
    st["last_known_main_sha"] = None
    with patch("coordinare.services.rebase.repo_url_from_config", return_value="https://github.com/acme/repo.git"), \
         patch(_FETCH, new=AsyncMock(side_effect=RuntimeError("no .netrc"))):
        assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert not st.get("relay_feedback")


@pytest.mark.asyncio
async def test_token_lookup_error_proceeds() -> None:
    st = _state(_conflicting())
    st["last_known_main_sha"] = None
    st["github_service"]._current_token = AsyncMock(side_effect=RuntimeError("app auth down"))
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True


@pytest.mark.asyncio
async def test_conflicting_unknown_branch_proceeds_without_marker() -> None:
    """Was a hold + FAILED marker in 097. A branch coordinare cannot identify as
    its own gets no instruction and no marker, and dispatch goes ahead."""
    st = _state({"mergeable_raw": "CONFLICTING", "head_ref_oid": "hX", "head_ref_name": "feature/x"})
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert not st.get("relay_feedback") and "last_rebase_attempt" not in st


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_state", ["CLOSED", "MERGED", "ERROR", ""])
async def test_pr_not_open_or_unreadable_proceeds_without_request(pr_state) -> None:
    st = _state(_conflicting(), pr_state=pr_state)
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert not st.get("relay_feedback") and "last_rebase_attempt" not in st


# --- divergence → implementer (555 FR-003) ------------------------------------ #


@pytest.mark.asyncio
async def test_conflicting_implementer_stage_dispatches_now_with_instruction() -> None:
    st = _state(_conflicting(head="hX"))
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    body = st["relay_feedback"][0]["body"]
    assert "Branch out of date with main" in body and _BRANCH in body
    assert "conflicts with `main`" in body
    assert st["performer_stage"] == "implementing" and st["phase"] == "dispatching"
    assert st["last_rebase_attempt"] == {
        "main_sha": "main-sha-0", "head_sha": "hX", "outcome": "delegated",
    }


@pytest.mark.asyncio
async def test_behind_branch_is_handed_to_implementer() -> None:
    st = _state({"mergeable_raw": "MERGEABLE", "merge_state_status": "BEHIND",
                 "head_ref_oid": "hB", "head_ref_name": _BRANCH})
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert "is behind `main`" in st["relay_feedback"][0]["body"]


@pytest.mark.asyncio
async def test_other_stage_is_rerouted_and_dispatches_next_cycle() -> None:
    """A reviewer was due: reroute to the implementer, defer exactly one cycle
    (check_inflight must re-validate the new stage), then pass on the feedback."""
    st = _state(_conflicting(), stage="reviewing")
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    assert st["performer_stage"] == "implementing"
    assert st["agent_dispatch"] == {} and st["agent_dispatch_at"] is None
    calls = st["github_service"].merge_calls
    # Next cycle: the queued instruction passes the gate without a second lookup.
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert st["github_service"].merge_calls == calls


@pytest.mark.asyncio
async def test_divergence_is_bounded_to_one_request_per_main_and_head() -> None:
    """555 FR-007: one implementer turn per (main, head), then held; a moved
    head or main re-enables the request."""
    st = _state(_conflicting(head="hX"))
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    st["relay_feedback"] = []  # the dispatch consumed and acknowledged it

    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False  # same pair: held
    assert not st["relay_feedback"]

    st["github_service"]._verdict = _conflicting(head="hY")  # implementer pushed
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is True
    assert st["last_rebase_attempt"]["head_sha"] == "hY"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["blocked", "failed", "delegated"])
async def test_blocked_thrash_holds_including_pre_555_markers(outcome) -> None:
    st = _state(_conflicting(head="hX"),
                marker={"main_sha": "main-sha-0", "head_sha": "hX", "outcome": outcome})
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    assert not st.get("relay_feedback")


@pytest.mark.asyncio
async def test_existing_feedback_for_another_stage_is_preserved() -> None:
    """Feedback queued behind a different stage's batch must survive the reroute."""
    st = _state(_conflicting(), stage="reviewing")
    st["dispatched_feedback"] = {"stage": "implementing", "items": [{"body": "earlier"}]}
    assert await _pre_dispatch_rebase_guard(st, "ITEM_2") is False
    assert st["dispatched_feedback"]["items"] == [{"body": "earlier"}]
    assert len(st["relay_feedback"]) == 1


@pytest.mark.asyncio
async def test_observability_secret_free() -> None:
    """rebase.triggered(reason=pre_dispatch) carries only ids/sha/outcome."""
    import structlog.testing

    st = _state(_conflicting())
    with structlog.testing.capture_logs() as logs:
        await _pre_dispatch_rebase_guard(st, "ITEM_2")
    rec = next(e for e in logs if e["event"] == "rebase.triggered")
    assert rec["reason"] == "pre_dispatch" and rec["outcome"] == "delegated"
    assert any(e["event"] == "rebase.performer_dispatched" for e in logs)
    blob = " ".join(str(v) for v in rec.values()).lower()
    assert "tok" not in blob and "token" not in blob and "secret" not in blob


# --- call-site wiring: dispatch_performer routes on the guard's bool ---------- #


@pytest.mark.asyncio
async def test_dispatch_node_skips_body_when_guard_holds() -> None:
    """The dispatch node must NOT run _dispatch_performer_body when the guard
    returns False (held/rerouted), and MUST run it when the guard returns True."""
    from coordinare.graph.nodes.dispatch_performer import dispatch_performer
    from tests.unit.graph.nodes.test_dispatch_performer import _base_state

    base = _base_state(performer_stage="implementing")

    with patch("coordinare.graph.nodes.dispatch_performer._pre_dispatch_rebase_guard",
               new=AsyncMock(return_value=False)), \
         patch("coordinare.graph.nodes.dispatch_performer._dispatch_performer_body",
               new=AsyncMock()) as body:
        await dispatch_performer(dict(base))
    body.assert_not_called()

    with patch("coordinare.graph.nodes.dispatch_performer._pre_dispatch_rebase_guard",
               new=AsyncMock(return_value=True)), \
         patch("coordinare.graph.nodes.dispatch_performer._dispatch_performer_body",
               new=AsyncMock(return_value={"phase": "monitoring_performer"})) as body:
        await dispatch_performer(dict(base))
    body.assert_called_once()
