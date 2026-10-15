"""266: the env bootstrap is visible: its state in words, its history, its feed entries and its holds.

The derivation is pure, so the states are checked against hand-built ``EnvCacheState`` values with a fixed
clock. The recorders are checked through the same code paths the daemon and the env cache run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.daemon import _close_raised_dispatch, _record_env_bootstrap_holds
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.activity_log import ActivityLog
from coordinare.services.bootstrap_history import (
    ACT_BUDGET_RESET,
    ACT_DISPATCHED,
    ACT_EXHAUSTED,
    ACT_FAILED,
    ACT_READY,
    ACT_RETRY,
    HISTORY_LIMIT,
    TAIL_LINES,
    AttemptEnd,
    AttemptStart,
    BootstrapHistory,
    BudgetNow,
    failure_detail,
    history_of,
    record_activity,
    report_completion,
)
from coordinare.services.bootstrap_status import (
    ENV_BOOTSTRAP_HOLD,
    NOT_STARTED_LABEL,
    STATE_BOOTSTRAPPING,
    STATE_EXHAUSTED,
    STATE_FAILED,
    STATE_NOT_CONFIGURED,
    STATE_NOT_STARTED,
    STATE_PASSED_NOT_READY,
    STATE_READY,
    STATE_VERIFYING,
    STATE_WAITING,
    derive_status,
    hold_text,
    symphony_view,
)
from coordinare.services.env_cache import BOOTSTRAP_RETRY_COOLDOWN_S, EnvCacheService
from coordinare.services.system_health import SYSTEM_HEALTH, SystemHealthInputs, build_system_health

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
GEMFILE_LINE = "verify.sh non-zero: Could not locate Gemfile"
SECRET = "ghp_" + "a" * 36
FIVE = 5
THREE = 3


def _cache(**fields: Any) -> EnvCacheState:
    base: dict[str, Any] = {
        "symphony_name": "dettmore",
        "sanitised_name": "dettmore-1",
        "cache_dir": Path("/devenv/dettmore-1"),
    }
    base.update(fields)
    return EnvCacheState(**base)


def _begin(history: BootstrapHistory, symphony: str, **fields: Any) -> Any:
    """Open an attempt the way the daemon does, with the fields the tests vary."""
    values: dict[str, Any] = {"started_at": NOW, "attempt": 1, "backend": "", "model": "", "performer_id": ""}
    values.update(fields)
    return history.begin(symphony, AttemptStart(**values))


def _finish(history: BootstrapHistory, symphony: str, **fields: Any) -> Any:
    """Close the running attempt with the outcome the tests vary."""
    values: dict[str, Any] = {"attempt": 1, "infra": False}
    values.update(fields)
    return history.finish(symphony, AttemptEnd(**values))


def _report(state: dict[str, Any], symphony: str, **fields: Any) -> None:
    """Report a completion with its budget, as the env cache does."""
    end = {key: fields[key] for key in ("succeeded", "reason", "commit", "attempt", "infra", "ended_at")}
    budget = {key: fields[key] for key in ("max_attempts", "exhausted_now", "retry_at")}
    report_completion(
state, symphony, AttemptEnd(**end), BudgetNow(**budget))


def _derive(cache: EnvCacheState | None, *, running: Any = None, configured: bool = True) -> dict[str, Any]:
    return derive_status(cache, configured=configured, running=running, max_attempts=THREE, now=NOW)


# --- the states, the attempt counts and the next retry --------------------------------------------------


def test_not_configured_when_the_symphony_has_no_env_bootstrap_performer() -> None:
    view = _derive(None, configured=False)
    assert view["state"] == STATE_NOT_CONFIGURED


def test_not_started_when_no_bootstrap_has_run() -> None:
    assert _derive(None)["state"] == STATE_NOT_STARTED
    assert _derive(_cache())["state"] == STATE_NOT_STARTED


def test_bootstrapping_reports_attempt_n_of_m_and_the_start_and_elapsed_time() -> None:
    started = NOW - timedelta(seconds=90)
    cache = _cache(bootstrap_in_flight=True, bootstrap_attempts=1, readme_sha="abc1234def5678")
    running = _running(started)
    view = _derive(cache, running=running)
    assert view["state"] == STATE_BOOTSTRAPPING
    assert view["attempt"] == 2
    assert view["max_attempts"] == THREE
    assert view["label"] == "Bootstrapping, run 2 of 3"
    assert view["started_at"] == started.isoformat()
    assert view["elapsed_seconds"] == 90
    assert view["commit"] == "abc1234"
    assert view["backend"] == "claude_code"
    assert view["model"] == "sonnet"
    assert view["performer_id"] == "perf-1"
    assert view["pod"] == "pod-abc"
    assert view["job_id"] == "job-9"


def test_verifying_when_the_workload_has_finished_and_the_verify_is_running() -> None:
    running = _running(NOW - timedelta(seconds=30))
    running.verifying = True
    view = _derive(_cache(bootstrap_in_flight=True), running=running)
    assert view["state"] == STATE_VERIFYING
    assert view["label"] == "Verifying, run 1 of 3"


def test_ready_names_the_commit_and_the_time_it_became_ready() -> None:
    ready_at = NOW - timedelta(hours=2)
    cache = _cache(
        last_bootstrap_succeeded=True, cache_dir_ready=True, readme_sha="fedcba9876543",
        last_bootstrap_at=ready_at,
    )
    view = _derive(cache)
    assert view["state"] == STATE_READY
    assert view["label"] == "Ready for commit fedcba9"
    assert view["ready_since"] == ready_at.isoformat()


def test_failed_when_the_retry_is_due_now_and_the_reason_is_the_failing_line() -> None:
    cache = _cache(
        last_bootstrap_succeeded=False, bootstrap_attempts=1, readme_sha="abc1234",
        last_bootstrap_at=NOW - timedelta(seconds=BOOTSTRAP_RETRY_COOLDOWN_S * 4),
        last_bootstrap_error="verify.sh non-zero: rbenv: ruby: command not found\nThe ruby command exists in 3.4.2",
    )
    view = _derive(cache)
    assert view["state"] == STATE_FAILED
    assert view["attempt"] == 1
    assert view["label"] == "Failed on run 1 of 3"
    assert view["reason"] == "verify.sh non-zero: rbenv: ruby: command not found"
    assert view["reason_tail"][-1] == "The ruby command exists in 3.4.2"


def test_waiting_to_retry_names_the_next_attempt_time_while_the_backoff_runs() -> None:
    failed_at = NOW - timedelta(seconds=10)
    cache = _cache(
        last_bootstrap_succeeded=False, bootstrap_attempts=1, last_bootstrap_at=failed_at,
        last_bootstrap_error=GEMFILE_LINE,
    )
    view = _derive(cache)
    # The backoff doubles with each failure: base x 2^attempts. One failure waits base x 2.
    due = failed_at + timedelta(seconds=BOOTSTRAP_RETRY_COOLDOWN_S * 2)
    assert view["state"] == STATE_WAITING
    assert view["attempt"] == 2
    assert view["next_attempt_at"] == due.isoformat()
    assert view["label"] == "Waiting to retry, run 2 of 3"


def test_budget_exhausted_says_what_resets_it() -> None:
    cache = _cache(
        last_bootstrap_succeeded=False, bootstrap_attempts=THREE, bootstrap_exhausted=True,
        last_bootstrap_error=GEMFILE_LINE, last_bootstrap_at=NOW,
    )
    view = _derive(cache)
    assert view["state"] == STATE_EXHAUSTED
    assert view["label"] == "Budget exhausted after 3 failed runs"
    assert view["reset_hint"] == "A new commit to the env spec files resets the budget."
    assert view["reason"] == GEMFILE_LINE


def test_a_health_failure_on_a_ready_cache_reads_as_failed() -> None:
    cache = _cache(last_bootstrap_succeeded=True, cache_dir_ready=True, runtime_health_failed=True)
    view = _derive(cache)
    assert view["state"] == STATE_FAILED
    assert "health check" in str(view["reason"])


def test_the_infrastructure_budget_is_named_when_it_is_what_ran_out() -> None:
    cache = _cache(
        last_bootstrap_succeeded=False, bootstrap_exhausted=True, infra_failures=5,
        last_bootstrap_error="docker could not run the verify",
    )
    view = _derive(cache)
    assert view["state"] == STATE_EXHAUSTED
    assert view["infra_failures"] == FIVE
    assert view["label"] == "Budget exhausted after 5 infrastructure failures"
    assert "docker daemon" in str(view["reset_hint"])


def _running(started: datetime) -> Any:
    from coordinare.services.bootstrap_history import BootstrapAttempt

    return BootstrapAttempt(
        number=1, attempt=2, commit="abc1234def5678", started_at=started, backend="claude_code",
        model="sonnet", performer_id="perf-1", pod="pod-abc", job_id="job-9",
    )


# --- the hold a card waits on --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cache", "expected"),
    [
        (_cache(bootstrap_in_flight=True, bootstrap_attempts=1), "Waiting for the environment bootstrap (run 2 of 3)"),
        (_cache(last_bootstrap_succeeded=True, cache_dir_ready=True), None),
    ],
)
def test_the_held_card_reason_says_the_attempt(cache: EnvCacheState, expected: str | None) -> None:
    assert hold_text(_derive(cache)) == expected


def test_a_failed_hold_names_the_failing_line_and_an_exhausted_one_names_the_reset() -> None:
    failed = hold_text(_derive(_cache(
        last_bootstrap_succeeded=False, bootstrap_attempts=1, last_bootstrap_at=NOW - timedelta(hours=1),
        last_bootstrap_error=GEMFILE_LINE,
    )))
    assert failed == f"Environment bootstrap failed (run 1 of 3): {GEMFILE_LINE}"
    exhausted = hold_text(_derive(_cache(
        last_bootstrap_succeeded=False, bootstrap_attempts=THREE, bootstrap_exhausted=True,
        last_bootstrap_error=GEMFILE_LINE,
    )))
    assert exhausted is not None
    assert "budget exhausted after 3 failed runs" in exhausted
    assert exhausted.endswith("A new commit to the env spec files resets the budget.")


def test_a_selected_card_is_held_with_the_bootstrap_words_and_a_card_with_a_reason_keeps_it() -> None:
    state: dict[str, Any] = {
        "current_symphony": "dettmore",
        "symphony_configs": {"dettmore": SimpleNamespace(env_bootstrap_performer_id="perf-1")},
        "env_cache": {"dettmore": _cache(bootstrap_in_flight=True, bootstrap_attempts=1)},
        "bootstrap_history": BootstrapHistory(),
        "coordinare_config": None,
    }
    skip_reasons: dict[str, dict[str, Any]] = {"card-2": {"reason": "pipeline_capacity", "detail": None}}
    _record_env_bootstrap_holds(state, {"card-1", "card-2"}, skip_reasons)
    held = skip_reasons["card-1"]
    assert held["reason"] == ENV_BOOTSTRAP_HOLD
    assert held["detail"] == "Waiting for the environment bootstrap (run 2 of 3)"
    assert held["bootstrap_state"] == STATE_BOOTSTRAPPING
    assert skip_reasons["card-2"]["reason"] == "pipeline_capacity"


# --- redaction of verify output -------------------------------------------------------------------------


def test_failure_detail_redacts_a_secret_in_the_verify_output_before_it_is_kept() -> None:
    failing, tail = failure_detail(f"step ok\nexport GITHUB_TOKEN={SECRET}\nverify.sh non-zero: bundle check failed")
    assert failing == "verify.sh non-zero: bundle check failed"
    joined = "\n".join([failing or "", *tail])
    assert SECRET not in joined
    assert "ghp_" + "a" * 36 not in joined


def test_failure_detail_keeps_only_the_last_lines_and_clips_each_one() -> None:
    lines = [f"line {i}" for i in range(TAIL_LINES * 3)]
    lines.append("x" * 2000)
    failing, tail = failure_detail("\n".join(lines))
    assert failing is not None
    assert len(tail) == TAIL_LINES
    assert all(len(line) <= 500 for line in tail)


def test_failure_detail_of_nothing_is_none() -> None:
    assert failure_detail(None) == (None, [])
    assert failure_detail("   \n  ") == (None, [])


# --- history bounds -------------------------------------------------------------------------------------


def test_history_keeps_only_the_newest_attempts_per_symphony() -> None:
    history = BootstrapHistory(limit=THREE)
    for index in range(FIVE):
        _begin(history,
            "dettmore", commit=f"sha{index}", started_at=NOW + timedelta(minutes=index), attempt=index + 1,
            backend="b", model="m", performer_id="p",
        )
        _finish(history,
            "dettmore", ended_at=NOW + timedelta(minutes=index, seconds=5), succeeded=False,
            reason=GEMFILE_LINE, commit=f"sha{index}", attempt=index + 1,
        )
    recent = history.recent("dettmore", limit=HISTORY_LIMIT)
    assert [record.commit for record in recent] == ["sha4", "sha3", "sha2"]
    assert all(record.outcome == "failed" for record in recent)


def test_history_is_per_symphony_and_recent_respects_its_limit() -> None:
    history = BootstrapHistory()
    _begin(history, "a", commit="one", started_at=NOW, attempt=1, backend="", model="", performer_id="")
    _begin(history, "b", commit="two", started_at=NOW, attempt=1, backend="", model="", performer_id="")
    assert [r.commit for r in history.recent("a", limit=5)] == ["one"]
    assert [r.commit for r in history.recent("b", limit=5)] == ["two"]
    assert history.recent("a", limit=0) == []
    assert history.recent("never", limit=5) == []


def test_finish_without_a_running_attempt_records_a_closed_one() -> None:
    history = BootstrapHistory()
    record = _finish(history,
        "dettmore", ended_at=NOW, succeeded=True, reason=None, commit="abc", attempt=1,
    )
    assert record.outcome == "succeeded"
    assert record.ended_at == NOW
    assert history.open_attempt("dettmore") is None


def test_the_symphony_view_carries_its_recent_attempts_newest_first() -> None:
    history = BootstrapHistory()
    state: dict[str, Any] = {"bootstrap_history": history, "env_cache": {}}
    _begin(history, "dettmore", commit="one", started_at=NOW, attempt=1, backend="", model="", performer_id="")
    _finish(history, "dettmore", ended_at=NOW, succeeded=False, reason=GEMFILE_LINE, commit="one", attempt=1)
    _begin(history, "dettmore", commit="two", started_at=NOW, attempt=2, backend="", model="", performer_id="")
    view = symphony_view(state, "dettmore", configured=True, now=NOW)
    assert [row["commit"] for row in view["history"]] == ["two", "one"]
    assert view["history"][1]["reason"] == GEMFILE_LINE
    assert view["history"][0]["running"] is True


def test_history_of_a_state_without_one_is_none() -> None:
    assert history_of({}) is None


# --- the feed entries -----------------------------------------------------------------------------------


def _state_with_feed() -> tuple[dict[str, Any], ActivityLog]:
    log = ActivityLog()
    return {"activity_log": log, "bootstrap_history": BootstrapHistory()}, log


def test_a_failed_attempt_with_retry_and_then_exhaustion_is_in_the_feed() -> None:
    state, log = _state_with_feed()
    _begin(state["bootstrap_history"],
        "dettmore", commit="abc1234", started_at=NOW, attempt=1, backend="claude_code", model="sonnet",
        performer_id="perf-1",
    )
    _report(
        state, "dettmore", succeeded=False, reason=f"verify {SECRET}\n{GEMFILE_LINE}", commit="abc1234",
        attempt=1, max_attempts=THREE, exhausted_now=False, infra=False, ended_at=NOW,
        retry_at=NOW + timedelta(minutes=4),
    )
    _report(
        state, "dettmore", succeeded=False, reason=GEMFILE_LINE, commit="abc1234", attempt=3,
        max_attempts=THREE, exhausted_now=True, infra=False, ended_at=NOW, retry_at=None,
    )
    types = [entry["activity_type"] for entry in log.snapshot()]
    assert types == [ACT_FAILED, ACT_RETRY, ACT_EXHAUSTED]
    failed = next(entry for entry in log.snapshot() if entry["activity_type"] == ACT_FAILED)
    assert GEMFILE_LINE in failed["text"]
    assert "Verify failed" in failed["text"]
    assert SECRET not in str(log.snapshot())
    assert all(entry["card_id"] == "" and entry["card_title"] == "dettmore" for entry in log.snapshot())


def test_dispatch_budget_reset_and_ready_are_in_the_feed() -> None:
    state, log = _state_with_feed()
    record_activity(state, "dettmore", ACT_DISPATCHED, "Bootstrap run 1 of 3 started on claude_code.")
    record_activity(state, "dettmore", ACT_BUDGET_RESET, "Budget reset for commit abc1234.")
    _report(
        state, "dettmore", succeeded=True, reason=None, commit="abc1234", attempt=1, max_attempts=THREE,
        exhausted_now=False, infra=False, ended_at=NOW, retry_at=None,
    )
    types = [entry["activity_type"] for entry in log.snapshot()]
    assert types == [ACT_DISPATCHED, ACT_BUDGET_RESET, ACT_READY]


def test_the_feed_is_best_effort_and_a_missing_feed_changes_nothing() -> None:
    record_activity({}, "dettmore", ACT_READY, "no feed")
    state: dict[str, Any] = {"bootstrap_history": BootstrapHistory()}
    _report(
        state, "dettmore", succeeded=False, reason=GEMFILE_LINE, commit="abc", attempt=1, max_attempts=THREE,
        exhausted_now=False, infra=False, ended_at=NOW, retry_at=None,
    )
    assert state["bootstrap_history"].recent("dettmore", limit=5)[0].outcome == "failed"


def test_the_completion_path_of_the_env_cache_records_history_and_feed_with_redaction() -> None:
    config = SimpleNamespace(global_config=SimpleNamespace(env_bootstrap_max_attempts=THREE))
    service = EnvCacheService(config)
    cache = _cache(bootstrap_in_flight=True, readme_sha="abc1234", bootstrap_attempts=0)
    state, log = _state_with_feed()
    state["env_cache"] = {"dettmore": cache}
    _begin(state["bootstrap_history"],
        "dettmore", commit="abc1234", started_at=NOW, attempt=1, backend="claude_code", model="sonnet",
        performer_id="perf-1",
    )
    service.on_bootstrap_complete("dettmore", False, state, error=f"env token {SECRET}\n{GEMFILE_LINE}")
    assert cache.bootstrap_attempts == 1
    assert cache.last_bootstrap_succeeded is False
    assert SECRET not in str(cache.last_bootstrap_error)
    assert SECRET not in str(log.snapshot())
    assert cache.last_bootstrap_at is not None
    view = symphony_view(state, "dettmore", configured=True, now=cache.last_bootstrap_at)
    assert view["state"] == STATE_WAITING
    assert view["reason"] == GEMFILE_LINE
    assert view["history"][0]["outcome"] == "failed"
    assert view["history"][0]["attempt"] == 1
    assert [entry["activity_type"] for entry in log.snapshot()] == [ACT_FAILED, ACT_RETRY]


def test_the_last_failure_exhausts_the_budget_and_says_so_in_the_feed() -> None:
    config = SimpleNamespace(global_config=SimpleNamespace(env_bootstrap_max_attempts=1))
    service = EnvCacheService(config)
    cache = _cache(bootstrap_in_flight=True, readme_sha="abc1234", bootstrap_attempts=0)
    state, log = _state_with_feed()
    state["env_cache"] = {"dettmore": cache}
    service.on_bootstrap_complete("dettmore", False, state, error=GEMFILE_LINE)
    assert cache.bootstrap_exhausted is True
    assert [entry["activity_type"] for entry in log.snapshot()] == [ACT_EXHAUSTED]
    assert symphony_view(state, "dettmore", configured=True, now=NOW)["state"] == STATE_EXHAUSTED


# --- the health row -------------------------------------------------------------------------------------


def _row(failures: tuple[tuple[str, str], ...], failed: tuple[str, ...]) -> dict[str, Any]:
    inputs = SystemHealthInputs(
        now=NOW, health={}, poll_interval_seconds=30, webhooks_enabled=False, performer_modes=(),
        active_performers=0, disk_free_fraction=0.6, env_symphonies=1, env_failed=failed, env_ready=0,
        env_failures=failures,
    )
    rows = build_system_health(SYSTEM_HEALTH, inputs)["rows"]
    return next(row for row in rows if row["key"] == "disk_env_cache")


def test_the_health_row_names_each_failing_symphony_with_its_reason() -> None:
    row = _row((("dettmore", GEMFILE_LINE),), ("dettmore",))
    assert row["status"] == "degraded"
    assert row["reason"] == f"env cache bootstrap failed for 1 symphony: dettmore: {GEMFILE_LINE}"
    assert row["env_failures"] == [{"symphony": "dettmore", "reason": GEMFILE_LINE}]


def test_the_health_row_carries_every_failing_symphony_for_the_browser_to_scope() -> None:
    row = _row((("a", GEMFILE_LINE), ("b", "rbenv: ruby: command not found")), ("a", "b"))
    assert row["reason"].endswith(f"a: {GEMFILE_LINE}; b: rbenv: ruby: command not found")
    assert [item["symphony"] for item in row["env_failures"]] == ["a", "b"]


def test_the_health_row_without_a_reason_keeps_the_count() -> None:
    row = _row((), ("dettmore",))
    assert row["reason"] == "env cache bootstrap failed for 1 symphony"
    assert row["env_failures"] == []


# --- the states added after review -----------------------------------------------------------------------


def test_a_new_commit_clears_the_old_failure_and_says_it_is_waiting_to_bootstrap() -> None:
    cache = _cache(readme_sha="oldsha1234", last_bootstrap_succeeded=None, last_bootstrap_error=None)
    view = _derive(cache)
    assert view["state"] == STATE_NOT_STARTED
    assert view["label"] == "Waiting to bootstrap the new commit"
    assert view["commit"] is None
    assert view["reason"] is None


def test_a_passed_run_whose_cache_is_not_ready_has_its_own_label() -> None:
    view = _derive(_cache(last_bootstrap_succeeded=True, cache_dir_ready=False, readme_sha="abc1234"))
    assert view["state"] == STATE_PASSED_NOT_READY
    assert view["label"] == "Passed, not ready"
    assert view["label"] != NOT_STARTED_LABEL
    assert hold_text(view) == "Waiting for the environment bootstrap to finish for the current commit"


def test_infrastructure_failures_are_counted_beside_the_run_not_in_it() -> None:
    cache = _cache(
        bootstrap_in_flight=True, bootstrap_attempts=1, infra_failures=2, readme_sha="abc1234",
    )
    view = _derive(cache, running=_running(NOW))
    assert view["label"] == "Bootstrapping, run 2 of 3 · 2 infrastructure failures"
    assert view["attempt"] == 2


def test_a_dispatch_that_raises_closes_its_run_as_failed_through_the_one_exit() -> None:
    state, log = _state_with_feed()
    _begin(state["bootstrap_history"], "dettmore", commit="abc1234", started_at=NOW, attempt=1,
           backend="claude_code", model="sonnet", performer_id="perf-1")
    _close_raised_dispatch(state, "dettmore", RuntimeError("container start refused"))
    record = state["bootstrap_history"].recent("dettmore", limit=1)[0]
    assert record.outcome == "failed"
    assert record.reason is not None and "container start refused" in record.reason
    assert state["bootstrap_history"].open_attempt("dettmore") is None
    assert [entry["activity_type"] for entry in log.snapshot()] == [ACT_FAILED]


def test_a_dispatch_that_raises_with_no_open_run_changes_nothing() -> None:
    state, log = _state_with_feed()
    _close_raised_dispatch(state, "dettmore", RuntimeError("boom"))
    assert log.snapshot() == []


# --- the feed's symphony field ---------------------------------------------------------------------------


def test_a_bootstrap_feed_entry_carries_its_symphony_and_the_stage_in_its_json() -> None:
    state, log = _state_with_feed()
    record_activity(state, "beta", ACT_READY, "Ready for commit abc1234")
    entry = log.snapshot()[0]
    assert entry["symphony"] == "beta"
    assert entry["stage"] == "env_bootstrap"
    assert entry["card_id"] == ""


def test_the_symphony_field_keeps_two_symphonies_same_text_rows_apart() -> None:
    state, log = _state_with_feed()
    record_activity(state, "alpha", ACT_FAILED, "Bootstrap failed on run 1 of 3.")
    record_activity(state, "beta", ACT_FAILED, "Bootstrap failed on run 1 of 3.")
    assert [entry["symphony"] for entry in log.snapshot()] == ["alpha", "beta"]


# --- the held card names the gate's own predicate ---------------------------------------------------------


def test_a_symphony_the_gate_holds_for_no_reason_names_no_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinare.daemon.env_cache_current", lambda state, cache: True)
    state: dict[str, Any] = {
        "current_symphony": "dettmore",
        "symphony_configs": {"dettmore": SimpleNamespace(env_bootstrap_performer_id="perf-1")},
        "env_cache": {"dettmore": _cache(last_bootstrap_succeeded=True, cache_dir_ready=True)},
        "bootstrap_history": BootstrapHistory(),
    }
    skip_reasons: dict[str, dict[str, Any]] = {}
    _record_env_bootstrap_holds(state, {"card-1"}, skip_reasons)
    assert skip_reasons == {}


def test_a_card_is_named_only_when_the_gate_predicate_says_it_is_held() -> None:
    state: dict[str, Any] = {
        "current_symphony": "dettmore",
        "symphony_configs": {"dettmore": SimpleNamespace(env_bootstrap_performer_id="perf-1")},
        "env_cache": {"dettmore": _cache(bootstrap_in_flight=True, bootstrap_attempts=1)},
        "bootstrap_history": BootstrapHistory(),
    }
    skip_reasons: dict[str, dict[str, Any]] = {}
    _record_env_bootstrap_holds(state, {"card-1"}, skip_reasons)
    assert skip_reasons["card-1"]["reason"] == ENV_BOOTSTRAP_HOLD
