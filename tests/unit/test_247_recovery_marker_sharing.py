"""#247 / spec 129 — per-cycle markers must be SHARED across the concurrent fanout.

`check_board` is a graph node, so it runs once per session. Several of its bookkeeping
keys are created with `state.setdefault(key, {})`, and each session gets a **shallow**
copy of the daemon state (`session_state = dict(self._state)`). When the key is absent
before the fanout, every session's `setdefault` therefore builds its own dict, and no
session can see any other's markers.

For `_recovery_attempts` that means two sessions attempt recovery of the same blocked card
in the same cycle — duplicate `find_pr_for_issue` / `get_pr_review_context` /
`move_card_or_warn` calls against the GitHub API — and the markers are then lost, because
the merge-back is first-writer-wins rather than a union.

Pre-seeding the key before the fanout fixes both halves at once: the shallow copies all
reference the SAME dict, so sessions see each other's marks immediately and the daemon's
own state carries them into the next cycle with no merge step at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.daemon import (
    SHARED_CYCLE_MARKER_KEYS,
    CoordinareDaemon,
    seed_shared_cycle_markers,
)


async def _no_sleep(_seconds: float) -> None:
    return None


def _make_daemon() -> CoordinareDaemon:
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})
    return CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
    )


def _session(card_id: str) -> dict:
    return {
        "current_card": {"id": card_id, "content_id": card_id, "title": "Test Card"},
        "phase": "monitoring_performer",
    }


def test_the_reported_divergence_is_real_without_seeding() -> None:
    """Reproduces #247: absent key + shallow copy == a private dict per session."""
    daemon_state: dict = {}

    s1 = dict(daemon_state)
    s2 = dict(daemon_state)
    s1.setdefault("_recovery_attempts", {})["card-A"] = "attempted"
    s2.setdefault("_recovery_attempts", {})["card-A"] = "attempted"

    assert s1["_recovery_attempts"] is not s2["_recovery_attempts"]
    assert "_recovery_attempts" not in daemon_state, "the daemon never saw either mark"


def test_seeding_makes_sessions_share_one_marker_map() -> None:
    """The fix: one object, so session 2 sees session 1's mark."""
    daemon_state: dict = {}
    seed_shared_cycle_markers(daemon_state)

    s1 = dict(daemon_state)
    s2 = dict(daemon_state)
    s1.setdefault("_recovery_attempts", {})["card-A"] = "attempted"

    assert s2["_recovery_attempts"] is s1["_recovery_attempts"]
    assert s2["_recovery_attempts"]["card-A"] == "attempted", (
        "session 2 must see session 1's mark, or both attempt the same card this cycle"
    )


def test_marks_reach_the_daemon_state_without_any_merge_step() -> None:
    """Because the object is shared, cross-cycle persistence needs no merge-back — which
    matters, since the merge is first-writer-wins and would drop all but one session."""
    daemon_state: dict = {}
    seed_shared_cycle_markers(daemon_state)

    dict(daemon_state).setdefault("_recovery_attempts", {})["card-A"] = "attempted"
    dict(daemon_state).setdefault("_recovery_attempts", {})["card-B"] = "attempted"

    assert daemon_state["_recovery_attempts"] == {
        "card-A": "attempted",
        "card-B": "attempted",
    }


def test_seeding_covers_every_shared_marker_key() -> None:
    """Guards against a new `setdefault` marker being added to check_board and quietly
    diverging. If this list and check_board disagree, the next one silently regresses."""
    daemon_state: dict = {}
    seed_shared_cycle_markers(daemon_state)

    for key in SHARED_CYCLE_MARKER_KEYS:
        assert key in daemon_state, f"{key} was not seeded"
        assert isinstance(daemon_state[key], dict)


def test_dep_announcements_is_covered() -> None:
    """#247 explicitly asked whether `_dep_announcements` had the same divergence. It did:
    same `state.get`/assign shape in the same node, also absent from the daemon."""
    assert "_dep_announcements" in SHARED_CYCLE_MARKER_KEYS


def test_recovery_attempts_is_covered() -> None:
    assert "_recovery_attempts" in SHARED_CYCLE_MARKER_KEYS


def test_seeding_preserves_existing_markers() -> None:
    """Seeding runs every cycle, so it must never clear marks already accumulated."""
    daemon_state: dict = {"_recovery_attempts": {"card-A": "attempted"}}
    before = daemon_state["_recovery_attempts"]

    seed_shared_cycle_markers(daemon_state)

    assert daemon_state["_recovery_attempts"] is before, "must not replace the live dict"
    assert daemon_state["_recovery_attempts"] == {"card-A": "attempted"}


def test_seeding_is_idempotent() -> None:
    daemon_state: dict = {}
    seed_shared_cycle_markers(daemon_state)
    first = {k: daemon_state[k] for k in SHARED_CYCLE_MARKER_KEYS}

    seed_shared_cycle_markers(daemon_state)

    for k, v in first.items():
        assert daemon_state[k] is v


def test_seeding_repairs_a_non_dict_value() -> None:
    """A restored snapshot could carry a null or a stale scalar under one of these keys.
    Seeding must leave a usable dict rather than let `setdefault` hand back a non-mapping
    that the node then tries to index."""
    daemon_state: dict = {"_recovery_attempts": None, "_dep_announcements": "stale"}

    seed_shared_cycle_markers(daemon_state)

    assert daemon_state["_recovery_attempts"] == {}
    assert daemon_state["_dep_announcements"] == {}


def test_check_board_marker_keys_match_the_seeded_set() -> None:
    """The real coupling: every `setdefault`-style marker key in check_board that is meant
    to be shared must appear in SHARED_CYCLE_MARKER_KEYS. Reads the source so a newly
    added marker fails here instead of diverging silently in production.
    """
    import re
    from pathlib import Path

    src = Path("src/coordinare/graph/nodes/check_board.py").read_text()
    # Keys the node creates or reads as per-cycle bookkeeping dicts.
    found = set(re.findall(r'state\.setdefault\(\s*"(_[a-z_]+)"\s*,\s*\{\}', src))
    unseeded = found - set(SHARED_CYCLE_MARKER_KEYS)

    assert not unseeded, (
        f"check_board setdefaults {sorted(unseeded)} as a per-cycle dict but the daemon "
        "does not seed it, so each session in the fanout gets a private copy (#247)"
    )


# --- the integration test: does the DAEMON actually share the maps? -------------------


@pytest.mark.asyncio
async def test_the_real_fanout_hands_every_session_the_same_marker_map() -> None:
    """The fix has to be WIRED, not merely available.

    Drives the real `_invoke_multi_session` with two eligible sessions and captures the
    state each session's graph invocation actually received. Both must hold the same
    marker dict object; if the daemon stops seeding, they will be separate objects (or
    absent) and this fails — which a unit test of the helper alone would not catch.
    """
    seen: list[dict] = []

    async def _capture(state: dict) -> dict:
        seen.append(state)
        # Mimic check_board's own bookkeeping so cross-session visibility is exercised.
        card = (state.get("current_card") or {}).get("id", "?")
        state.setdefault("_recovery_attempts", {})[card] = "attempted"
        return state

    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=_capture)

    daemon = _make_daemon()
    daemon._graph = graph
    from types import SimpleNamespace
    daemon._state["config"] = SimpleNamespace(max_concurrent_cards=2)
    daemon._state["active_sessions"] = {"card-a": _session("card-a"), "card-b": _session("card-b")}
    daemon._state["board_snapshot"] = {"IN_PROGRESS": ["card-a", "card-b"]}
    daemon._state["_board_cache"] = {
        "snapshot": {"IN_PROGRESS": ["card-a", "card-b"]},
        "titles": {}, "descriptions": {}, "issue_numbers": {},
        "issue_urls": {}, "content_node_ids": {},
    }
    daemon._state["github_service"] = None  # skip the pre-poll

    await daemon._invoke_multi_session()

    assert len(seen) == 2, f"expected both sessions to tick, got {len(seen)}"
    first, second = (s.get("_recovery_attempts") for s in seen)
    assert first is not None and second is not None, "the daemon did not seed the map"
    assert first is second, (
        "each session got its own marker dict, so neither can see the other's attempt "
        "and both recover the same blocked card in one cycle (#247)"
    )
    # And both marks landed, visible to the daemon for the next cycle.
    assert daemon._state["_recovery_attempts"] == {
        "card-a": "attempted",
        "card-b": "attempted",
    }
