"""#147: the timeline's assembly from recorded sources. Pure: the sources are passed in.

Covers skipped stages, several runs in one stage, durations from recorded timestamps, ledger
cost and an unknown cost, the missing sources (no transcripts, no events, no ledger), and the
board strip. The endpoint, its scope and its redaction are in tests/unit/test_147_card_pipeline_api.py.
"""
from __future__ import annotations

from typing import Any

from coordinare.lifecycle import ROLE_TO_STAGE
from coordinare.services.card_timeline import (
    PIPELINE_STAGES,
    STATUS_CURRENT,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NOT_RECORDED,
    STATUS_PENDING,
    STATUS_RAN,
    STATUS_SKIPPED,
    CardRef,
    LiveStage,
    RunIndex,
    UsageLedger,
    build_timeline,
    pipeline_strip,
    stage_for_run,
    stage_statuses,
)
from coordinare.vocabulary import STAGES

T0 = "2026-10-14T09:00:00+00:00"
T1 = "2026-10-14T09:05:00+00:00"
T2 = "2026-10-14T09:09:00+00:00"


def _event(kind: str, stage: str | None = None, at: str = T0, **detail: Any) -> dict[str, Any]:
    return {"at": at, "kind": kind, "stage": stage, "detail": detail}


def _run(run: str, stage: str | None, duration: float | None, outcome: str | None = "completed") -> dict[str, Any]:
    return {"run": run, "stage": stage, "started_at": T0, "ended_at": T1, "duration_seconds": duration,
            "outcome": outcome}


def _timeline(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "symphony": "alpha-sym",
        "card_id": "PVTI_1",
        "events": [],
        "current_stage": None,
        "lifecycle": None,
        "runs": None,
        "runs_reason": "transcripts are not configured on this daemon",
        "runs_truncated": False,
        "usage": None,
        "usage_reason": "transcripts are disabled, so token and cost figures are not tracked",
    }
    args.update(overrides)
    return build_timeline(
        CardRef(symphony=args["symphony"], card_id=args["card_id"]),
        args["events"],
        LiveStage(current=args["current_stage"], lifecycle=tuple(args["lifecycle"] or ())),
        RunIndex(runs=args["runs"], reason=args["runs_reason"], truncated=args["runs_truncated"]),
        UsageLedger(stages=args["usage"], reason=args["usage_reason"]),
    )


def _stage(timeline: dict[str, Any], code: str) -> dict[str, Any]:
    return next(stage for stage in timeline["stages"] if stage["stage"] == code)


def test_the_pipeline_is_the_eight_stages_of_the_code_with_one_label_each() -> None:
    assert set(PIPELINE_STAGES) == set(ROLE_TO_STAGE.values()) - {"advocate"}
    assert len(PIPELINE_STAGES) == 8
    for stage in PIPELINE_STAGES:
        assert stage in STAGES, f"{stage} has no vocabulary label"
    assert PIPELINE_STAGES[0] == "assessing"
    assert PIPELINE_STAGES[-1] == "closing_review"


def test_a_stage_the_card_got_past_without_running_is_skipped() -> None:
    events = [_event("dispatched", "implementing")]
    statuses = stage_statuses(events, current_stage=None)
    assert statuses["assessing"] == STATUS_SKIPPED
    assert statuses["architecting"] == STATUS_SKIPPED
    assert statuses["implementing"] == STATUS_RAN
    assert statuses["reviewing"] == STATUS_PENDING


def test_a_stage_outside_the_configured_lifecycle_is_skipped_for_every_card() -> None:
    events = [_event("dispatched", "assessing"), _event("dispatched", "implementing")]
    lifecycle = ["assessing", "implementing", "reviewing", "closing_review"]
    statuses = stage_statuses(events, current_stage=None, lifecycle=lifecycle)
    assert statuses["architecting"] == STATUS_SKIPPED
    assert statuses["security"] == STATUS_SKIPPED
    assert statuses["qa"] == STATUS_SKIPPED
    assert statuses["assessing"] == STATUS_DONE
    assert statuses["closing_review"] == STATUS_PENDING


def test_the_live_stage_is_current_and_the_stages_before_it_are_done() -> None:
    events = [_event("dispatched", "implementing"), _event("verdict", "architecting", result="pass", marker="plan_ready")]
    statuses = stage_statuses(events, current_stage="reviewing")
    assert statuses["reviewing"] == STATUS_CURRENT
    assert statuses["implementing"] == STATUS_DONE
    assert statuses["architecting"] == STATUS_DONE


def test_the_latest_verdict_decides_a_stage() -> None:
    events = [
        _event("dispatched", "reviewing"),
        _event("verdict", "reviewing", result="fail", marker="changes_requested"),
        _event("dispatched", "implementing"),
        _event("verdict", "reviewing", result="pass", marker="approved", at=T1),
    ]
    assert stage_statuses(events, current_stage=None)["reviewing"] == STATUS_DONE
    failed = [_event("dispatched", "reviewing"), _event("verdict", "reviewing", result="fail", marker="changes_requested")]
    assert stage_statuses(failed, current_stage=None)["reviewing"] == STATUS_FAILED


def test_a_merged_card_is_done_where_it_ran_and_skipped_where_it_did_not() -> None:
    events = [_event("dispatched", "assessing"), _event("dispatched", "implementing"), _event("merged", None)]
    statuses = stage_statuses(events, current_stage=None)
    assert statuses["assessing"] == STATUS_DONE
    assert statuses["implementing"] == STATUS_DONE
    assert statuses["reviewing"] == STATUS_SKIPPED
    assert statuses["closing_review"] == STATUS_SKIPPED


def test_a_card_with_no_recorded_events_shows_not_recorded_everywhere() -> None:
    statuses = stage_statuses([], current_stage=None)
    assert set(statuses.values()) == {STATUS_NOT_RECORDED}
    timeline = _timeline(events=[], runs=[])
    assert all(stage["status"] == STATUS_NOT_RECORDED for stage in timeline["stages"])
    assert any("No lifecycle events" in note for note in timeline["not_recorded"])


def test_several_runs_in_one_stage_are_counted_and_their_durations_summed() -> None:
    runs = [
        _run("r1", "implementing", 60.0),
        _run("r2", "implementing", 30.5),
        _run("r3", "reviewing", 12.0),
    ]
    timeline = _timeline(events=[_event("dispatched", "implementing")], runs=runs, runs_reason=None)
    implementing = _stage(timeline, "implementing")
    assert implementing["run_count"] == 2
    assert implementing["run_count_source"] == "transcripts"
    assert implementing["duration_seconds"] == 90.5
    assert implementing["duration_partial"] is False
    assert [r["run"] for r in implementing["runs"]] == ["r1", "r2"]
    assert _stage(timeline, "reviewing")["run_count"] == 1


def test_a_run_with_no_recorded_duration_makes_the_stage_total_partial() -> None:
    runs = [_run("r1", "qa", None, outcome=None), _run("r2", "qa", 40.0)]
    qa = _stage(_timeline(events=[_event("dispatched", "qa")], runs=runs, runs_reason=None), "qa")
    assert qa["run_count"] == 2
    assert qa["duration_seconds"] == 40.0
    assert qa["duration_partial"] is True
    only_unknown = [_run("r1", "qa", None)]
    alone = _stage(_timeline(events=[_event("dispatched", "qa")], runs=only_unknown, runs_reason=None), "qa")
    assert alone["duration_seconds"] is None
    assert alone["duration_partial"] is True


def test_without_transcripts_the_run_count_comes_from_the_recorded_dispatches() -> None:
    events = [_event("dispatched", "implementing"), _event("dispatched", "implementing", at=T1)]
    timeline = _timeline(events=events, runs=None)
    implementing = _stage(timeline, "implementing")
    assert implementing["run_count"] == 2
    assert implementing["run_count_source"] == "lifecycle"
    assert implementing["duration_seconds"] is None
    assert timeline["runs_available"] is False
    assert any("transcripts" in note for note in timeline["not_recorded"])


def test_no_source_for_run_counts_is_not_recorded_rather_than_zero() -> None:
    timeline = _timeline(events=[_event("verdict", "reviewing", result="pass", marker="approved")], runs=None)
    reviewing = _stage(timeline, "reviewing")
    assert reviewing["run_count"] is None
    assert reviewing["run_count_source"] is None
    assert reviewing["duration_seconds"] is None


def test_unassigned_and_truncated_runs_are_named() -> None:
    runs = [_run("r1", None, 5.0), _run("r2", "assessing", 5.0)]
    timeline = _timeline(events=[_event("dispatched", "assessing")], runs=runs, runs_reason=None, runs_truncated=True)
    assert timeline["unassigned_runs"] == 1
    assert any("newest runs" in note for note in timeline["not_recorded"])


def test_ledger_cost_is_shown_per_stage_and_an_unknown_cost_says_why() -> None:
    usage = {
        "implementing": {"tokens": {"total": 2050}, "cost": {"usd": 0.13}, "tracked": True},
        "reviewing": {"tokens": {"total": 900}, "cost": {"usd": None, "unknown_reason": "no price"}, "tracked": True},
    }
    timeline = _timeline(events=[_event("dispatched", "implementing")], runs=None, usage=usage, usage_reason=None)
    implementing = _stage(timeline, "implementing")
    assert implementing["usage"]["cost"]["usd"] == 0.13
    assert implementing["usage_reason"] is None
    reviewing = _stage(timeline, "reviewing")
    assert reviewing["usage"]["cost"]["usd"] is None
    assert reviewing["usage"]["cost"]["unknown_reason"] == "no price"
    qa = _stage(timeline, "qa")
    assert qa["usage"] is None
    assert "No token usage" in qa["usage_reason"]


def test_an_untracked_ledger_gives_the_reason_on_every_stage() -> None:
    timeline = _timeline(events=[_event("dispatched", "implementing")], usage=None)
    assert all(stage["usage"] is None for stage in timeline["stages"])
    assert all("not tracked" in stage["usage_reason"] for stage in timeline["stages"])
    assert any("token and cost" in note for note in timeline["not_recorded"])


def test_events_come_back_oldest_first() -> None:
    events = [_event("merged", None, at=T2), _event("dispatched", "implementing", at=T0), _event("ci_result", "implementing", at=T1)]
    timeline = _timeline(events=events)
    assert [e["kind"] for e in timeline["events"]] == ["dispatched", "ci_result", "merged"]
    assert timeline["merged"] is True


def test_a_run_is_mapped_to_its_pipeline_stage_from_its_role() -> None:
    assert stage_for_run("implementer") == "implementing"
    assert stage_for_run("implementing") == "implementing"
    assert stage_for_run("closer") == "closing_review"
    assert stage_for_run("tech_writer") == "documenting"
    assert stage_for_run("advocate") is None
    assert stage_for_run("conductor:assess") is None
    assert stage_for_run("") is None
    assert stage_for_run(None) is None


def test_the_board_strip_has_one_entry_per_stage_and_names_the_current_one() -> None:
    strip = pipeline_strip([_event("dispatched", "implementing")], LiveStage(current="implementing"))
    assert strip["current"] == "implementing"
    assert [item["stage"] for item in strip["statuses"]] == list(PIPELINE_STAGES)
    by_stage = {item["stage"]: item["status"] for item in strip["statuses"]}
    assert by_stage["implementing"] == STATUS_CURRENT
    assert by_stage["assessing"] == STATUS_SKIPPED
    assert pipeline_strip([], LiveStage(current="not_a_stage"))["current"] is None


def test_the_timeline_names_the_card_and_the_current_stage() -> None:
    timeline = _timeline(events=[_event("dispatched", "reviewing")], current_stage="reviewing")
    assert timeline["symphony"] == "alpha-sym"
    assert timeline["card_id"] == "PVTI_1"
    assert timeline["current_stage"] == "reviewing"
    assert _stage(timeline, "reviewing")["status"] == STATUS_CURRENT
