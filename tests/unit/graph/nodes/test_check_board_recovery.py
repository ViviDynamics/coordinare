"""129 (US1): check_board blocked-card auto-recovery (stale-review + env increments)."""
from __future__ import annotations

import types
from typing import Any

import pytest

from coordinare.graph.nodes.check_board import _attempt_blocked_card_recovery

_UNSET = object()


class _FakeGitHub:
    def __init__(
        self, ctx: dict[str, Any], *, move_raises: bool = False, pr: Any = _UNSET
    ) -> None:
        self._ctx = ctx
        self.moved: list[tuple[str, str]] = []
        self._move_raises = move_raises
        self._pr = {"pr_node_id": "PR1"} if pr is _UNSET else pr

    async def find_pr_for_issue(self, issue_node: str) -> dict[str, Any]:
        return self._pr

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        return self._ctx

    async def move_card(self, card_id: str, column: str) -> None:
        if self._move_raises:
            raise RuntimeError("move failed")
        self.moved.append((card_id, column))


class _Notify:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def dispatch(self, e: Any) -> None:
        self.events.append(e)


def _ctx(threads, *, decision="CHANGES_REQUESTED", head="newsha", commit="oldsha"):
    return {
        "reviews": [{"id": "RVW_1", "author_login": "jason", "state": "CHANGES_REQUESTED", "commit_oid": commit}],
        "review_threads": threads,
        "head_oid": head,
        "review_decision": decision,
    }


def _state(notify):
    return {
        "human_reviewers": ["jason"],
        "notification_service": notify,
    }


_BOARD = {"content_node_ids": {"CARD_1": "ISSUE_1"}}


@pytest.mark.asyncio
async def test_disabled_by_default_no_op(monkeypatch) -> None:
    monkeypatch.delenv("COORDINARE_BLOCKED_RECOVERY", raising=False)
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    await _attempt_blocked_card_recovery(_state(notify), gh, ["CARD_1"], _BOARD)
    assert gh.moved == [] and notify.events == []  # gate off → nothing happens


@pytest.mark.asyncio
async def test_stale_addressed_recovers_to_in_review(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    st = _state(notify)
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD)
    assert ("CARD_1", "IN_REVIEW") in gh.moved
    assert len(notify.events) == 1 and notify.events[0].event_type.value == "card_auto_recovered"
    assert st["_recovery_attempts"].get("CARD_1") is True  # anti-thrash marker


@pytest.mark.asyncio
async def test_genuine_changes_requested_not_recovered(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    # threads unresolved → not stale-addressed → stays blocked (human-gate safety)
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": False, "review_id": "RVW_1"}]))
    notify = _Notify()
    await _attempt_blocked_card_recovery(_state(notify), gh, ["CARD_1"], _BOARD)
    assert gh.moved == [] and notify.events == []


@pytest.mark.asyncio
async def test_anti_thrash_one_attempt_per_run(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    st = _state(_Notify())
    st["_recovery_attempts"] = {"CARD_1": True}  # already attempted this run
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD)
    assert gh.moved == []  # skipped by anti-thrash


@pytest.mark.asyncio
async def test_failsafe_on_move_error(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]), move_raises=True)
    # must NOT raise
    await _attempt_blocked_card_recovery(_state(_Notify()), gh, ["CARD_1"], _BOARD)


@pytest.mark.asyncio
async def test_no_pr_no_recovery(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]))
    # board without a content node for the card → can't find issue/PR → skip
    await _attempt_blocked_card_recovery(_state(_Notify()), gh, ["CARD_1"], {"content_node_ids": {}})
    assert gh.moved == []


@pytest.mark.asyncio
async def test_recovered_card_pruned_from_snapshot_and_blocked_list(monkeypatch) -> None:
    # Adversarial finding: a recovered card must be dropped from the in-memory
    # board snapshot AND the live blocked list so the downstream blocked-handling
    # branch does not re-adopt it as BLOCKED in the same cycle.
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    st = _state(notify)
    st["board_snapshot"] = {"BLOCKED": ["CARD_1", "CARD_2"]}
    blocked = st["board_snapshot"]["BLOCKED"]  # caller aliases the snapshot list
    await _attempt_blocked_card_recovery(st, gh, blocked, _BOARD)
    assert ("CARD_1", "IN_REVIEW") in gh.moved
    assert "CARD_1" not in st["board_snapshot"]["BLOCKED"]  # pruned from snapshot
    assert "CARD_1" not in blocked  # pruned from the live list (identity preserved)
    assert "CARD_2" in st["board_snapshot"]["BLOCKED"]  # untouched card stays


# ---- 129a: ENV_BLOCKED recovery gatherer --------------------------------------

_ENV_BLOCKED = {"head_sha": "", "pattern_id": "P1", "cause": "c", "action": "a"}
_NO_CONTENT_BOARD = {"content_node_ids": {}}  # no issue node → PR/stale path skipped


def _env_cache(*, ready=True, health_failed=False, boot_ok=True):
    return types.SimpleNamespace(
        cache_dir_ready=ready,
        runtime_health_failed=health_failed,
        last_bootstrap_succeeded=boot_ok,
    )


def _env_state(notify, *, env_blocked=None, prev="IN_PROGRESS", cache=None, symphony="sym"):
    st = _state(notify)
    st["current_symphony"] = symphony
    st["env_cache"] = {symphony: cache} if cache is not None else {}
    st["active_sessions"] = {
        "CARD_1": {
            "env_blocked": env_blocked,
            "current_card": {"id": "CARD_1", "previous_status": prev},
            "performer_stage": "implementing",
        }
    }
    return st


@pytest.mark.asyncio
async def test_env_blocked_recovers_when_cache_healthy(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]))
    notify = _Notify()
    st = _env_state(notify, env_blocked=_ENV_BLOCKED, prev="IN_PROGRESS", cache=_env_cache())
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _NO_CONTENT_BOARD)
    assert ("CARD_1", "IN_PROGRESS") in gh.moved
    assert len(notify.events) == 1
    assert notify.events[0].event_type.value == "card_auto_recovered"
    assert st["_recovery_attempts"].get("CARD_1") is True


@pytest.mark.asyncio
async def test_env_blocked_stays_when_cache_unhealthy(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]))
    st = _env_state(_Notify(), env_blocked=_ENV_BLOCKED, cache=_env_cache(health_failed=True))
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _NO_CONTENT_BOARD)
    assert gh.moved == []  # runtime_health_failed still set → not recovered


@pytest.mark.asyncio
async def test_env_blocked_stays_when_bootstrap_not_yet_succeeded(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]))
    st = _env_state(_Notify(), env_blocked=_ENV_BLOCKED, cache=_env_cache(boot_ok=None))
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _NO_CONTENT_BOARD)
    assert gh.moved == []


@pytest.mark.asyncio
async def test_env_blocked_no_cache_state_stays(monkeypatch) -> None:
    # No env-cache entry for the symphony → cannot confirm recovery → stay blocked.
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]))
    st = _env_state(_Notify(), env_blocked=_ENV_BLOCKED, cache=None)
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _NO_CONTENT_BOARD)
    assert gh.moved == []


@pytest.mark.asyncio
async def test_env_blocked_resumes_prior_review_stage(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]))
    st = _env_state(_Notify(), env_blocked=_ENV_BLOCKED, prev="IN_REVIEW", cache=_env_cache())
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _NO_CONTENT_BOARD)
    assert ("CARD_1", "IN_REVIEW") in gh.moved


@pytest.mark.asyncio
async def test_no_env_marker_no_review_is_noop(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]))
    st = _env_state(_Notify(), env_blocked=None, cache=_env_cache())  # no block reason detectable
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _NO_CONTENT_BOARD)
    assert gh.moved == []


@pytest.mark.asyncio
async def test_multi_reason_env_cleared_but_review_not_addressed_stays(monkeypatch) -> None:
    # Card is BOTH env-blocked (recovered) AND has an unaddressed human CR:
    # ALL reasons must clear (FR-005) → still blocked.
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": False, "review_id": "RVW_1"}]))
    st = _env_state(_Notify(), env_blocked=_ENV_BLOCKED, cache=_env_cache())
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD)  # _BOARD → PR/stale path active
    assert gh.moved == []


@pytest.mark.asyncio
async def test_env_recovered_but_review_state_unreadable_stays_blocked(monkeypatch) -> None:
    # Adversarial finding (HIGH): a PR exists but its review state is unreadable
    # (lookup returns a PR without a usable node id). Recovering on the env reason
    # alone could bypass a genuine unaddressed human CR → must stay blocked.
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]), pr={"unexpected": "shape"})  # truthy PR, no pr_node_id
    st = _env_state(_Notify(), env_blocked=_ENV_BLOCKED, cache=_env_cache())
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD)  # _BOARD has issue node
    assert gh.moved == []


@pytest.mark.asyncio
async def test_env_recovered_no_pr_still_recovers(monkeypatch) -> None:
    # A genuinely PR-less env-blocked card (find_pr returns None) has no human CR
    # gate → env recovery is safe and proceeds.
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]), pr=None)
    st = _env_state(_Notify(), env_blocked=_ENV_BLOCKED, cache=_env_cache())
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD)
    assert ("CARD_1", "IN_PROGRESS") in gh.moved


@pytest.mark.asyncio
async def test_multi_reason_both_clear_recovers_to_in_review(monkeypatch) -> None:
    # Both env recovered AND stale review addressed → recover to the latest implied
    # stage (IN_REVIEW), with a reason-specific dedup key.
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    st = _env_state(notify, env_blocked=_ENV_BLOCKED, prev="IN_PROGRESS", cache=_env_cache())
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD)
    assert ("CARD_1", "IN_REVIEW") in gh.moved  # max(IN_PROGRESS, IN_REVIEW)
    assert len(notify.events) == 1
    dk = notify.events[0].dedup_key
    assert "env_blocked" in dk and "stale_review" in dk  # both reasons in the key


@pytest.mark.asyncio
async def test_notification_target_matches_decision_stage(monkeypatch) -> None:
    # Adversarial finding: notification target must reflect the actual recovery
    # target stage, not a hardcoded "IN_REVIEW".
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    await _attempt_blocked_card_recovery(_state(notify), gh, ["CARD_1"], _BOARD)
    assert len(notify.events) == 1
    moved_target = dict(gh.moved)["CARD_1"]
    assert notify.events[0].payload["target"] == moved_target
