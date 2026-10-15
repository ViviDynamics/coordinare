"""#155 final check: the checks icon never claims the current commit unless something checked it,
the lifecycle keeps the latest gate result, and recorded error text is bounded.

Block 1: the current PR head is the one monitor_pr reads each cycle while the card waits on its PR
(stored on the session as pr_live_head). head_after is the performer's last report and is not a
current head. Without a live head the icon is neutral and says the current commit was not checked.

Block 2: a ci_result is deduplicated only against the most recent ci_result of its card, so a HOLD,
then a BOUNCE, then a HOLD on one head keeps all three and the latest judgement is the last one.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.dashboard.card_pipeline import card_pipeline_block, live_head
from coordinare.dashboard.store import DashboardStore
from coordinare.graph.nodes import monitor_pr as monitor_pr_module
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.lib.redaction import MAX_SESSION_ERROR_CHARS, clip_session_error
from coordinare.services.card_lifecycle import CardLifecycleLog
from coordinare.services.card_timeline import (
    CHECKS_RED,
    CHECKS_RUNNING,
    LiveStage,
    checks_view,
    pipeline_strip,
)

HEAD_GATED = "a" * 40
HEAD_NEW = "b" * 40
GATE_AT = "2026-10-14T09:10:00+00:00"
DISPATCH_AT = "2026-10-14T09:00:00+00:00"


def _gate(result: str, at: str, head: str = HEAD_GATED[:12]) -> dict[str, Any]:
    return {"at": at, "kind": "ci_result", "stage": "implementing",
            "detail": {"result": result, "head": head}, "key": f"{head}:{result}"}


def _dispatch(at: str = DISPATCH_AT) -> dict[str, Any]:
    return {"at": at, "kind": "dispatched", "stage": "implementing", "detail": {}}


def _daemon(session: dict[str, Any], log: CardLifecycleLog | None = None) -> Any:
    state: dict[str, Any] = {"active_sessions": {"c1": session}, "symphony_states": {}, "lifecycle_sequence": []}
    if log is not None:
        state["card_lifecycle"] = log
    return SimpleNamespace(state=state)


# --- block 1: the current head ------------------------------------------------------- #


def test_a_live_head_that_differs_from_the_gated_head_is_stale_not_green() -> None:
    view = checks_view([_dispatch(), _gate("FORWARD", GATE_AT)], HEAD_NEW)
    assert view is not None
    assert view["current_checked"] is True
    assert view["stale"] is True


def test_without_a_live_head_the_current_commit_is_not_checked() -> None:
    view = checks_view([_dispatch(), _gate("FORWARD", GATE_AT)], None)
    assert view is not None
    assert view["current_checked"] is False
    assert view["stale"] is False


def test_head_after_alone_is_not_a_current_head() -> None:
    """P1d: the performer reported X, a push to Y was never reported. head_after is X, so it must not
    make the gate on X look current."""
    session = {"phase": "monitoring_pr", "performer_stage": "implementing",
               "current_card": {"id": "c1", "head_after": HEAD_GATED}}
    log = CardLifecycleLog()
    log.record("alpha", "c1", "dispatched", stage="implementing", at=DISPATCH_AT)
    log.record("alpha", "c1", "ci_result", stage="implementing", at=GATE_AT,
               detail={"result": "FORWARD", "head": HEAD_GATED[:12]}, key="x")
    checks = card_pipeline_block(_daemon(session, log))["alpha"]["c1"]["checks"]
    assert checks["state"] == "pass"
    assert checks["current_checked"] is False
    assert checks["stale"] is False


def test_the_live_head_is_read_from_the_session_only_while_the_card_is_monitoring_pr() -> None:
    waiting = {"phase": "monitoring_pr", "pr_live_head": {"head": HEAD_NEW, "checked_at": GATE_AT}}
    assert live_head(_daemon(waiting), "alpha", "c1") == HEAD_NEW
    moved_on = {"phase": "monitoring_performer", "pr_live_head": {"head": HEAD_NEW, "checked_at": GATE_AT}}
    assert live_head(_daemon(moved_on), "alpha", "c1") is None


def test_an_unchecked_head_in_a_strip_is_marked_so_the_icon_can_stay_neutral() -> None:
    strip = pipeline_strip([_dispatch(), _gate("HOLD", GATE_AT)], LiveStage(head=None))
    assert strip["checks"]["state"] == CHECKS_RUNNING
    assert strip["checks"]["current_checked"] is False


def test_a_bounce_on_a_live_head_that_differs_is_stale_and_its_state_is_kept() -> None:
    strip = pipeline_strip([_dispatch(), _gate("BOUNCE", GATE_AT)], LiveStage(head=HEAD_NEW))
    assert strip["checks"]["state"] == CHECKS_RED
    assert strip["checks"]["stale"] is True


@pytest.mark.asyncio
async def test_monitor_pr_stores_the_live_head_it_reads_on_the_session() -> None:
    """The per-cycle source: monitor_pr reads head_oid while the card waits on its PR, and mirrors it
    onto the session. No extra GitHub call."""
    from tests.unit.test_096_closer_approval_head import _GitHub, _state

    monitor_pr_module._merge_waits.clear()
    github = _GitHub([], head=HEAD_NEW)
    state = _state(github)
    session = {"phase": "monitoring_pr", "current_card": dict(state["current_card"])}
    state["active_sessions"] = {"CARD_96": session}
    state["phase"] = "monitoring_pr"
    await monitor_pr(state)
    assert session.get("pr_live_head", {}).get("head") == HEAD_NEW


# --- block 2: the lifecycle keeps the latest gate result ------------------------------ #


def test_a_hold_then_bounce_then_hold_on_one_head_keeps_all_three_results() -> None:
    log = CardLifecycleLog()
    log.record("alpha", "c1", "dispatched", stage="implementing", at=DISPATCH_AT)
    for result, at in [("HOLD", "2026-10-14T09:05:00+00:00"), ("BOUNCE", "2026-10-14T09:10:00+00:00"),
                       ("HOLD", "2026-10-14T09:15:00+00:00")]:
        assert log.record("alpha", "c1", "ci_result", stage="implementing", at=at,
                          detail={"result": result, "head": HEAD_GATED[:12]}, key=f"{HEAD_GATED[:12]}:{result}")
    results = [e["detail"]["result"] for e in log.events("alpha", "c1") if e["kind"] == "ci_result"]
    assert results == ["HOLD", "BOUNCE", "HOLD"]


def test_the_latest_judgement_is_the_one_the_icon_shows() -> None:
    log = CardLifecycleLog()
    log.record("alpha", "c1", "dispatched", stage="implementing", at=DISPATCH_AT)
    for result, at in [("HOLD", "2026-10-14T09:05:00+00:00"), ("BOUNCE", "2026-10-14T09:10:00+00:00"),
                       ("HOLD", "2026-10-14T09:15:00+00:00")]:
        log.record("alpha", "c1", "ci_result", stage="implementing", at=at,
                   detail={"result": result, "head": HEAD_GATED[:12]}, key=f"{HEAD_GATED[:12]}:{result}")
    view = checks_view(log.events("alpha", "c1"), HEAD_GATED)
    assert view is not None and view["state"] == CHECKS_RUNNING


def test_the_stage_strip_is_unchanged_by_the_extra_gate_results() -> None:
    """#147's timeline reads stages from dispatches and verdicts. A repeated gate result adds an entry to the
    lifecycle list and leaves every stage status as it was."""
    plain = [_dispatch()]
    judged = [_dispatch(), _gate("HOLD", "2026-10-14T09:05:00+00:00"), _gate("BOUNCE", "2026-10-14T09:10:00+00:00"),
              _gate("HOLD", "2026-10-14T09:15:00+00:00")]
    live = LiveStage(current="implementing", head=HEAD_GATED)
    assert pipeline_strip(judged, live)["statuses"] == pipeline_strip(plain, live)["statuses"]


def test_identical_results_across_poll_cycles_still_record_once() -> None:
    log = CardLifecycleLog()
    kept = [log.record("alpha", "c1", "ci_result", stage="implementing", at=f"2026-10-14T09:0{i}:00+00:00",
                       detail={"result": "HOLD", "head": HEAD_GATED[:12]}, key=f"{HEAD_GATED[:12]}:HOLD")
            for i in range(3)]
    assert kept == [True, False, False]
    assert len([e for e in log.events("alpha", "c1") if e["kind"] == "ci_result"]) == 1


def test_other_kinds_still_dedupe_against_the_whole_history() -> None:
    log = CardLifecycleLog()
    assert log.record("alpha", "c1", "merged", stage="closing_review", at=DISPATCH_AT, key="merged")
    assert not log.record("alpha", "c1", "merged", stage="closing_review", at=GATE_AT, key="merged")


def test_a_ci_result_repeat_is_compared_to_the_latest_one_not_to_an_older_one() -> None:
    log = CardLifecycleLog()
    log.record("alpha", "c1", "ci_result", stage="implementing", at="2026-10-14T09:00:00+00:00",
               detail={"result": "HOLD", "head": HEAD_GATED[:12]}, key="h:HOLD")
    log.record("alpha", "c1", "ci_result", stage="implementing", at="2026-10-14T09:01:00+00:00",
               detail={"result": "BOUNCE", "head": HEAD_GATED[:12]}, key="h:BOUNCE")
    assert log.record("alpha", "c1", "ci_result", stage="implementing", at="2026-10-14T09:02:00+00:00",
                      detail={"result": "HOLD", "head": HEAD_GATED[:12]}, key="h:HOLD") is True


# --- bounded error text --------------------------------------------------------------- #


def test_a_long_error_is_clipped_with_an_ellipsis_to_the_named_limit() -> None:
    clipped = clip_session_error("x" * 200_000)
    assert len(clipped) == MAX_SESSION_ERROR_CHARS
    assert clipped.endswith("...")


def test_a_short_error_is_unchanged() -> None:
    assert clip_session_error("GitHub 401 Bad credentials") == "GitHub 401 Bad credentials"


def test_the_snapshot_clips_a_stored_error_too() -> None:
    sym_state = SimpleNamespace(
        cycle_count=0, error_count=3, last_poll_at=None, last_error="y" * 200_000,
        board_snapshot=None, board_titles={}, board_issue_numbers={}, board_issue_urls={},
        board_pr_urls={}, session_skip_reasons={}, todo_waits={}, todo_capacity=None,
        active_sessions={}, active_card=None,
    )
    daemon = SimpleNamespace(state={
        "config": SimpleNamespace(github_org="ViviDynamics", project_name=""),
        "symphony_configs": {"alpha": SimpleNamespace(github_project_number=1, overrides=None)},
        "symphony_states": {"alpha": sym_state}, "env_cache": {},
    })
    [entry] = DashboardStore._build_symphonies_data(daemon, 1)
    assert len(entry["state"]["last_error"]) == MAX_SESSION_ERROR_CHARS


def test_the_daemon_clips_the_error_it_stores_on_a_failed_symphony() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "src" / "coordinare" / "daemon.py").read_text(encoding="utf-8")
    assert "s.last_error = clip_session_error(redact_state_value(str(exc)))" in source
