"""#155 review: the PR checks state is gate-time, so it carries the gated head, the gate time and
whether it is stale against the card's current head. The daemon count rides on each symphony.

The gate records are asserted in tests/unit/graph/nodes/test_155_gate_records.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.dashboard.card_pipeline import card_pipeline_block, live_head
from coordinare.dashboard.store import DashboardStore
from coordinare.services.card_lifecycle import CardLifecycleLog
from coordinare.services.card_timeline import (
    CHECKS_BLOCKED,
    CHECKS_GREEN,
    CHECKS_RED,
    CHECKS_RUNNING,
    CHECKS_UNKNOWN,
    LiveStage,
    checks_view,
    pipeline_strip,
)

HEAD_A = "a" * 40
HEAD_B = "b" * 40
GATE_AT = "2026-10-14T08:30:00+00:00"
DISPATCH_AT = "2026-10-14T08:32:00+00:00"


def _gate(result: str, head: str = HEAD_A[:12], at: str = GATE_AT) -> dict[str, Any]:
    return {"at": at, "kind": "ci_result", "stage": "implementing", "detail": {"result": result, "head": head}}


def _dispatch(at: str) -> dict[str, Any]:
    return {"at": at, "kind": "dispatched", "stage": "implementing", "detail": {}}


# --- the state each gate result stands for ----------------------------------------- #


@pytest.mark.parametrize(("result", "state"), [
    ("FORWARD", CHECKS_GREEN),
    ("BOUNCE", CHECKS_RED),
    ("HOLD", CHECKS_RUNNING),
    ("ENV_BLOCKED", CHECKS_BLOCKED),
    ("UNKNOWN", CHECKS_UNKNOWN),
])
def test_each_gate_result_has_its_own_state(result: str, state: str) -> None:
    view = checks_view([_gate(result)], HEAD_A)
    assert view is not None
    assert view["state"] == state


def test_an_env_blocked_hold_is_not_a_failing_check() -> None:
    view = checks_view([_gate("ENV_BLOCKED")], HEAD_A)
    assert view is not None and view["state"] == CHECKS_BLOCKED
    assert view["state"] != CHECKS_RED


def test_a_card_with_no_gate_has_no_checks_view() -> None:
    assert checks_view([_dispatch(DISPATCH_AT)], HEAD_A) is None


def test_the_view_carries_the_gated_head_and_the_gate_time() -> None:
    view = checks_view([_gate("BOUNCE")], HEAD_B)
    assert view is not None
    assert view["head"] == HEAD_A[:12]
    assert view["at"] == GATE_AT


# --- stale: the gate is about an earlier commit or an earlier dispatch ---------------- #


def test_a_new_push_after_a_bounce_makes_the_result_stale() -> None:
    events = [_dispatch("2026-10-14T08:00:00+00:00"), _gate("BOUNCE"), _dispatch(DISPATCH_AT)]
    view = checks_view(events, HEAD_B)
    assert view is not None
    assert view["state"] == CHECKS_RED
    assert view["stale"] is True


def test_a_re_dispatch_with_no_new_push_is_not_stale() -> None:
    events = [_gate("BOUNCE"), _dispatch(DISPATCH_AT)]
    view = checks_view(events, HEAD_A)
    assert view is not None and view["stale"] is False


def test_an_unknown_current_head_is_not_checked_rather_than_stale() -> None:
    # 155 final check: with no live head nothing judged the current commit, so the icon says so.
    events = [_gate("FORWARD"), _dispatch(DISPATCH_AT)]
    view = checks_view(events, None)
    assert view is not None and view["current_checked"] is False and view["stale"] is False


def test_an_unknown_current_head_and_a_gate_after_the_dispatch_is_not_stale() -> None:
    events = [_dispatch("2026-10-14T08:00:00+00:00"), _gate("FORWARD")]
    view = checks_view(events, None)
    assert view is not None and view["stale"] is False


def test_an_unknown_gated_head_is_compared_by_time_not_by_head() -> None:
    after_dispatch = checks_view([_gate("FORWARD", head=""), _dispatch(DISPATCH_AT)], HEAD_A)
    assert after_dispatch is not None and after_dispatch["stale"] is True
    before_dispatch = checks_view([_dispatch("2026-10-14T08:00:00+00:00"), _gate("FORWARD", head="")], HEAD_A)
    assert before_dispatch is not None and before_dispatch["stale"] is False


# --- the live head: read from the session, never from GitHub ------------------------- #


def test_the_strip_marks_the_checks_stale_against_the_live_head() -> None:
    strip = pipeline_strip([_gate("BOUNCE"), _dispatch(DISPATCH_AT)], LiveStage(head=HEAD_B))
    assert strip["checks"]["stale"] is True
    assert strip["checks"]["state"] == CHECKS_RED


def test_the_strip_without_a_live_head_has_the_checks_view_and_no_head() -> None:
    strip = pipeline_strip([_gate("FORWARD")], LiveStage())
    assert strip["checks"]["head"] == HEAD_A[:12]


def test_live_head_reads_the_head_monitor_pr_recorded_on_the_session() -> None:
    session = {"performer_stage": "implementing", "phase": "monitoring_pr",
               "pr_live_head": {"head": HEAD_B, "checked_at": DISPATCH_AT}}
    daemon = SimpleNamespace(state={"active_sessions": {"c1": session}, "symphony_states": {}})
    assert live_head(daemon, "alpha", "c1") == HEAD_B
    assert live_head(daemon, "alpha", "missing") is None


def test_the_snapshot_block_marks_a_bounced_card_stale_after_a_new_push() -> None:
    log = CardLifecycleLog()
    log.record("alpha", "c1", "ci_result", stage="implementing", at=GATE_AT,
               detail={"result": "BOUNCE", "head": HEAD_A[:12]}, key="x")
    log.record("alpha", "c1", "dispatched", stage="implementing", at=DISPATCH_AT)
    session = {"performer_stage": "implementing", "phase": "monitoring_pr",
               "pr_live_head": {"head": HEAD_B, "checked_at": DISPATCH_AT}}
    daemon = SimpleNamespace(state={"card_lifecycle": log, "lifecycle_sequence": [],
                                    "active_sessions": {"c1": session}, "symphony_states": {}})
    checks = card_pipeline_block(daemon)["alpha"]["c1"]["checks"]
    assert checks["state"] == CHECKS_RED
    assert checks["stale"] is True


def test_each_symphony_carries_the_daemon_wide_cycle_count() -> None:
    daemon = SimpleNamespace(state={
        "config": SimpleNamespace(github_org="ViviDynamics", project_name=""),
        "symphony_configs": {"alpha": SimpleNamespace(github_project_number=1, overrides=None)},
        "symphony_states": {}, "env_cache": {},
    })
    [entry] = DashboardStore._build_symphonies_data(daemon, 4)
    assert entry["daemon_cycles_completed"] == 4
