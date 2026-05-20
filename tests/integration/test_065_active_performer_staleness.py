"""065 US1 — integration test exploiting the Active Performers staleness bug.

Background
----------
``CoordinareDaemon`` only invokes ``DashboardStore.broadcaster.broadcast()``
at the end of each successful processing cycle (``daemon.py:1655``).  Mutations
to ``daemon.state["active_sessions"]`` that happen *inside* a cycle — a card
kicked back to TODO, a performer advancing through stages, a new dispatch —
are therefore invisible to subscribed SSE clients until the next cycle
boundary.  When cycles are long (multi-card orchestration, a slow GitHub
poll, the 064 ``pr-checks`` gate's 90 s wait), the Active Performers panel
renders a snapshot of the world that is tens of seconds old, including the
exact bug captured in tmp/img_5.png: a performer tile still showing a card
that has already been moved back to TODO.

What this test asserts
----------------------
The contract: when ``active_sessions`` is mutated, a corresponding broadcast
MUST be observable on the broadcaster within a small bounded delay (≤1 s in
this test).  The mechanism — direct watcher, coalesced queue flush,
event-driven invalidation — is implementation-defined; the contract is
end-to-end timing.

This test is expected to FAIL on current ``main`` (no mid-cycle broadcast
plumbing exists) and PASS once the 065 US1 fix lands.  That is the point:
it locks in the missing behaviour as a regression target.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

from coordinare.dashboard import DashboardStore

# ---------------------------------------------------------------------------
# Helpers — minimal daemon/metrics/health shaped just enough for build_snapshot
# ---------------------------------------------------------------------------


def _session(card_id: str, title: str, stage: str) -> dict[str, Any]:
    """Return a session dict shaped the way build_snapshot expects."""
    return {
        "current_card": {
            "title": title,
            "issue_number": int(card_id.rsplit("_", 1)[-1])
            if card_id.rsplit("_", 1)[-1].isdigit()
            else None,
            "issue_url": f"https://github.com/org/repo/issues/{card_id}",
        },
        "phase": "monitoring_performer",
        "performer_stage": stage,
        "card_tokens_total": 0,
        "card_cost_estimate": 0.0,
    }


def _make_daemon(active_sessions: dict[str, dict]) -> MagicMock:
    daemon = MagicMock()
    daemon.state = {
        "phase": "monitoring_performer",
        "error_count": 0,
        "active_sessions": active_sessions,
    }
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = True
    daemon.running = True
    return daemon


def _make_metrics() -> MagicMock:
    m = MagicMock()
    m.cycles_completed_total._value.get.return_value = 0
    m.build_info.labels.return_value._value.get.return_value = {
        "started_at": "2026-05-17T09:00:00+00:00"
    }
    return m


def _make_health() -> MagicMock:
    h = MagicMock()
    probe = MagicMock()
    probe.subsystem_name = "github"
    probe.status.value = "healthy"
    probe.is_required = True
    probe.checked_at.isoformat.return_value = "2026-05-17T09:00:00+00:00"
    probe.details = None
    h.snapshot.return_value.probes = [probe]
    return h


def _parse(raw: str) -> dict:
    data_line = next(line for line in raw.splitlines() if line.startswith("data:"))
    return json.loads(data_line[len("data:") :].strip())


def _session_titles(payload: dict) -> set[str]:
    return {s["card_title"] for s in payload.get("active_sessions", [])}


def _session_stages(payload: dict) -> dict[str, str]:
    return {s["card_id"]: s["performer_stage"] for s in payload.get("active_sessions", [])}


# ---------------------------------------------------------------------------
# Exploit
# ---------------------------------------------------------------------------


def test_mid_cycle_active_session_mutation_is_broadcast_within_1s() -> None:
    """Exploit: two cards in flight; mid-cycle one is kicked back to TODO and
    the other advances stage; assert the broadcaster delivers an SSE event
    reflecting the *new* state within 1 s of the mutation, without waiting
    for a cycle-end broadcast."""

    async def _run() -> dict:
        store = DashboardStore()
        active = {
            "PVTI_A": _session("PVTI_A", "Fix the auth bug", "implementing"),
            "PVTI_B": _session("PVTI_B", "Add keyboard nav", "architecting"),
        }
        daemon = _make_daemon(active)
        metrics = _make_metrics()
        health = _make_health()

        gen = store.sse_stream(daemon, metrics, health)
        try:
            # Initial connect snapshot (analogous to a freshly-opened browser
            # tab): contains both A and B.
            initial_raw = await gen.__anext__()
            initial = _parse(initial_raw)
            assert _session_titles(initial) == {"Fix the auth bug", "Add keyboard nav"}, (
                f"setup precondition failed: {_session_titles(initial)}"
            )

            # ---- mid-cycle mutation ----------------------------------------
            # Card A is kicked back to TODO (its session is removed from
            # active_sessions). Card B advances from 'architecting' to
            # 'implementing'. Neither of these is a cycle boundary — they are
            # the kind of in-cycle state change that nodes inside
            # _conduct_single_symphony perform.
            del active["PVTI_A"]
            active["PVTI_B"]["performer_stage"] = "implementing"
            active["PVTI_B"]["current_card"]["title"] = "Add keyboard nav (rev 2)"

            # ---- the assertion --------------------------------------------
            # The fix must guarantee a broadcast within 1 s of the mutation.
            # On current main, no broadcast ever fires (the daemon's only
            # broadcast call site is at cycle-end), so wait_for times out and
            # the test fails — which is exactly the bug.
            next_raw = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
            return _parse(next_raw)
        finally:
            await gen.aclose()

    payload = asyncio.run(_run())

    # The post-mutation broadcast must reflect the new state, not the prior.
    assert _session_titles(payload) == {"Add keyboard nav (rev 2)"}, (
        f"removed session lingered or update missed: {_session_titles(payload)}"
    )
    assert _session_stages(payload) == {"PVTI_B": "implementing"}, (
        f"stale performer_stage in broadcast: {_session_stages(payload)}"
    )


def test_cycle_end_broadcast_still_reflects_active_sessions() -> None:
    """Control: the existing cycle-end broadcast path keeps working. Confirms
    the exploit above is isolating the *timing* bug, not a regression in
    how build_snapshot serialises active_sessions."""

    async def _run() -> dict:
        store = DashboardStore()
        daemon = _make_daemon(
            {"PVTI_B": _session("PVTI_B", "Add keyboard nav", "reviewing")}
        )
        gen = store.sse_stream(daemon, _make_metrics(), _make_health())
        try:
            await gen.__anext__()  # consume initial
            # Simulate daemon.py:1655 — cycle-end broadcast with the current
            # snapshot. This is the only broadcast call site today, so this
            # path must continue to work.
            snap = store.build_snapshot(daemon, _make_metrics(), _make_health())
            store.broadcaster.broadcast(snap)
            raw = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        finally:
            await gen.aclose()
        return _parse(raw)

    payload = asyncio.run(_run())
    assert _session_titles(payload) == {"Add keyboard nav"}
    assert _session_stages(payload) == {"PVTI_B": "reviewing"}
