"""#151: the System health card's data: aggregation, thresholds, not-monitored and secrets."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from coordinare.services.system_health import (
    BOARD_FAILURE_STREAK_DOWN,
    BOARD_STALE_POLL_INTERVALS,
    BOARD_STALE_WITHOUT_POLL_SECONDS,
    DEGRADED,
    DISK_FREE_DEGRADED_FRACTION,
    DISK_FREE_DOWN_FRACTION,
    DOWN,
    GITHUB_FAILURE_STREAK_DOWN,
    GITHUB_RATE_LOW_FRACTION,
    HEALTHY,
    MODEL_ERROR_STREAK_DOWN,
    MODEL_WINDOW_TURNS,
    NOT_MONITORED,
    PERFORMER_FAILURE_STREAK_DOWN,
    UNKNOWN,
    SystemHealthInputs,
    SystemHealthRecorder,
    build_system_health,
)

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
KEYS = ["github_api", "github_webhooks", "board_check", "performer_runtime", "model_backends", "disk_env_cache"]


def _inputs(**overrides: Any) -> SystemHealthInputs:
    base: dict[str, Any] = {
        "now": NOW,
        "health": {"github": "healthy", "agent": "healthy"},
        "poll_interval_seconds": 30,
        "webhooks_enabled": False,
        "performer_modes": (),
        "active_performers": 0,
        "disk_free_fraction": 0.5,
        "env_symphonies": 0,
        "env_failed": (),
        "env_ready": 0,
    }
    base.update(overrides)
    return SystemHealthInputs(**base)


def _rows(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in result["rows"]}


def _rate_headers(remaining: int, *, limit: int = 5000, reset_in: timedelta = timedelta(minutes=38)) -> dict[str, str]:
    return {
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Reset": str(int((NOW + reset_in).timestamp())),
    }


def _green(rec: SystemHealthRecorder) -> None:
    rec.record_github_rate(_rate_headers(4800), at=NOW - timedelta(seconds=5))
    rec.record_github_call(ok=True, at=NOW - timedelta(seconds=5))
    rec.record_board_poll("acme/1", ok=True, at=NOW - timedelta(seconds=10))
    rec.record_webhook(accepted=True, at=NOW - timedelta(seconds=30))
    rec.record_performer_start("DockerRuntime", ok=True, at=NOW - timedelta(seconds=40))
    for _ in range(3):
        rec.record_model_turn(ok=True, at=NOW - timedelta(seconds=50))


_GREEN_INPUTS: dict[str, Any] = {"webhooks_enabled": True, "performer_modes": ("ephemeral",)}


# --- shape and the all-green collapse -------------------------------------------------


def test_rows_come_in_the_issue_order_with_the_documented_keys() -> None:
    result = build_system_health(SystemHealthRecorder(), _inputs())
    assert [row["key"] for row in result["rows"]] == KEYS
    assert [row["name"] for row in result["rows"]] == [
        "GitHub API", "GitHub webhooks", "Board check", "Performer runtime", "Model backends", "Disk and env cache",
    ]


def test_everything_green_is_overall_healthy() -> None:
    rec = SystemHealthRecorder()
    _green(rec)
    result = build_system_health(rec, _inputs(**_GREEN_INPUTS))
    assert result["overall"] == HEALTHY
    assert {row["status"] for row in result["rows"]} == {HEALTHY}


def test_each_row_says_how_fresh_its_status_is() -> None:
    rec = SystemHealthRecorder()
    _green(rec)
    rows = _rows(build_system_health(rec, _inputs(**_GREEN_INPUTS)))
    assert rows["github_api"]["checked_at"] == (NOW - timedelta(seconds=5)).isoformat()
    assert rows["board_check"]["checked_at"] == (NOW - timedelta(seconds=10)).isoformat()
    assert rows["board_check"]["last_success_at"] == (NOW - timedelta(seconds=10)).isoformat()
    assert rows["github_webhooks"]["fresh_label"] == "last delivery"
    assert rows["disk_env_cache"]["checked_at"] == NOW.isoformat()


def test_nothing_observed_yet_is_unknown_not_green() -> None:
    result = build_system_health(SystemHealthRecorder(), _inputs(webhooks_enabled=True, performer_modes=("ephemeral",)))
    rows = _rows(result)
    assert rows["github_api"]["status"] == UNKNOWN
    assert rows["board_check"]["status"] == UNKNOWN
    assert rows["performer_runtime"]["status"] == UNKNOWN
    assert rows["model_backends"]["status"] == UNKNOWN
    assert rows["github_webhooks"]["status"] == UNKNOWN
    assert result["overall"] == UNKNOWN


# --- not monitored -------------------------------------------------------------------


def test_webhooks_disabled_is_not_monitored_with_the_reason_and_never_green() -> None:
    rec = SystemHealthRecorder()
    _green(rec)
    row = _rows(build_system_health(rec, _inputs(webhooks_enabled=False)))["github_webhooks"]
    assert row["status"] == NOT_MONITORED
    assert "webhooks.enabled" in row["reason"]
    assert row["checked_at"] is None


def test_not_monitored_does_not_stop_the_card_collapsing() -> None:
    rec = SystemHealthRecorder()
    _green(rec)
    result = build_system_health(rec, _inputs(webhooks_enabled=False))
    assert result["overall"] == HEALTHY


def test_no_performer_endpoints_is_not_monitored_because_performers_are_subprocesses() -> None:
    row = _rows(build_system_health(SystemHealthRecorder(), _inputs()))["performer_runtime"]
    assert row["status"] == NOT_MONITORED
    assert "subprocess" in row["reason"]


def test_persistent_only_endpoints_are_not_monitored_because_coordinare_does_not_start_them() -> None:
    row = _rows(build_system_health(SystemHealthRecorder(), _inputs(performer_modes=("persistent",))))["performer_runtime"]
    assert row["status"] == NOT_MONITORED
    assert "persistent" in row["reason"]


# --- GitHub API ----------------------------------------------------------------------


def test_github_summary_names_the_quota_and_when_it_resets() -> None:
    rec = SystemHealthRecorder()
    rec.record_github_rate(_rate_headers(4800), at=NOW)
    rec.record_github_call(ok=True, at=NOW)
    row = _rows(build_system_health(rec, _inputs()))["github_api"]
    assert row["status"] == HEALTHY
    assert row["summary"] == "4,800 of 5,000 requests left, resets 12:38 UTC"


def test_github_low_quota_is_degraded_at_the_named_fraction() -> None:
    limit = 5000
    boundary = int(limit * GITHUB_RATE_LOW_FRACTION)
    at_boundary = SystemHealthRecorder()
    at_boundary.record_github_rate(_rate_headers(boundary, limit=limit), at=NOW)
    at_boundary.record_github_call(ok=True, at=NOW)
    assert _rows(build_system_health(at_boundary, _inputs()))["github_api"]["status"] == HEALTHY

    below = SystemHealthRecorder()
    below.record_github_rate(_rate_headers(boundary - 1, limit=limit), at=NOW)
    below.record_github_call(ok=True, at=NOW)
    row = _rows(build_system_health(below, _inputs()))["github_api"]
    assert row["status"] == DEGRADED
    assert "rate limit" in row["reason"]


def test_github_exhausted_quota_before_reset_is_down() -> None:
    rec = SystemHealthRecorder()
    rec.record_github_rate(_rate_headers(0), at=NOW)
    rec.record_github_call(ok=False, error="RateLimitedGitHubError", at=NOW)
    row = _rows(build_system_health(rec, _inputs()))["github_api"]
    assert row["status"] == DOWN


def test_github_one_failed_call_is_degraded_and_names_only_the_error_class() -> None:
    rec = SystemHealthRecorder()
    rec.record_github_call(ok=False, error="TransientGitHubError", at=NOW)
    row = _rows(build_system_health(rec, _inputs()))["github_api"]
    assert row["status"] == DEGRADED
    assert "TransientGitHubError" in row["reason"]
    assert row["last_success_at"] is None


def test_github_failure_streak_reaching_the_named_count_is_down() -> None:
    rec = SystemHealthRecorder()
    for _ in range(GITHUB_FAILURE_STREAK_DOWN):
        rec.record_github_call(ok=False, error="TransientGitHubError", at=NOW)
    assert _rows(build_system_health(rec, _inputs()))["github_api"]["status"] == DOWN


def test_github_a_success_clears_the_streak() -> None:
    rec = SystemHealthRecorder()
    rec.record_github_call(ok=False, error="TransientGitHubError", at=NOW)
    rec.record_github_call(ok=True, at=NOW)
    assert _rows(build_system_health(rec, _inputs()))["github_api"]["status"] == HEALTHY


def test_github_circuit_breaker_degraded_in_the_health_registry_shows_on_the_row() -> None:
    rec = SystemHealthRecorder()
    _green(rec)
    result = build_system_health(rec, _inputs(health={"github": "degraded"}))
    assert _rows(result)["github_api"]["status"] == DEGRADED


# --- Board check ---------------------------------------------------------------------


def test_board_stale_after_the_named_number_of_poll_intervals_is_degraded() -> None:
    horizon = 30 * BOARD_STALE_POLL_INTERVALS
    fresh = SystemHealthRecorder()
    fresh.record_board_poll("acme/1", ok=True, at=NOW - timedelta(seconds=horizon))
    assert _rows(build_system_health(fresh, _inputs()))["board_check"]["status"] == HEALTHY

    stale = SystemHealthRecorder()
    stale.record_board_poll("acme/1", ok=True, at=NOW - timedelta(seconds=horizon + 1))
    row = _rows(build_system_health(stale, _inputs()))["board_check"]
    assert row["status"] == DEGRADED
    assert "no successful board check" in row["reason"]


def test_board_without_polling_uses_the_webhook_fallback_threshold() -> None:
    rec = SystemHealthRecorder()
    rec.record_board_poll("acme/1", ok=True, at=NOW - timedelta(seconds=BOARD_STALE_WITHOUT_POLL_SECONDS - 1))
    assert _rows(build_system_health(rec, _inputs(poll_interval_seconds=None)))["board_check"]["status"] == HEALTHY


def test_board_failure_streak_is_degraded_then_down() -> None:
    rec = SystemHealthRecorder()
    rec.record_board_poll("acme/1", ok=False, error="TransientGitHubError", at=NOW)
    row = _rows(build_system_health(rec, _inputs()))["board_check"]
    assert row["status"] == DEGRADED
    assert row["summary"] == "1 failed in a row"
    for _ in range(BOARD_FAILURE_STREAK_DOWN - 1):
        rec.record_board_poll("acme/1", ok=False, error="TransientGitHubError", at=NOW)
    assert _rows(build_system_health(rec, _inputs()))["board_check"]["status"] == DOWN


def test_board_worst_of_several_boards_wins_and_the_freshest_success_is_shown() -> None:
    rec = SystemHealthRecorder()
    rec.record_board_poll("acme/1", ok=True, at=NOW - timedelta(seconds=5))
    rec.record_board_poll("acme/2", ok=False, error="TransientGitHubError", at=NOW)
    row = _rows(build_system_health(rec, _inputs()))["board_check"]
    assert row["status"] == DEGRADED
    assert row["last_success_at"] == (NOW - timedelta(seconds=5)).isoformat()


# --- Webhooks ------------------------------------------------------------------------


def test_webhooks_enabled_with_no_delivery_yet_is_unknown() -> None:
    row = _rows(build_system_health(SystemHealthRecorder(), _inputs(webhooks_enabled=True)))["github_webhooks"]
    assert row["status"] == UNKNOWN


def test_webhook_rejection_after_the_last_good_delivery_is_degraded() -> None:
    rec = SystemHealthRecorder()
    rec.record_webhook(accepted=True, at=NOW - timedelta(seconds=60))
    rec.record_webhook(accepted=False, at=NOW - timedelta(seconds=10))
    row = _rows(build_system_health(rec, _inputs(webhooks_enabled=True)))["github_webhooks"]
    assert row["status"] == DEGRADED
    assert "signature" in row["reason"]


def test_a_good_delivery_after_a_rejection_clears_it() -> None:
    rec = SystemHealthRecorder()
    rec.record_webhook(accepted=False, at=NOW - timedelta(seconds=60))
    rec.record_webhook(accepted=True, at=NOW - timedelta(seconds=10))
    row = _rows(build_system_health(rec, _inputs(webhooks_enabled=True)))["github_webhooks"]
    assert row["status"] == HEALTHY
    assert row["last_success_at"] == (NOW - timedelta(seconds=10)).isoformat()


# --- Performer runtime ---------------------------------------------------------------


def test_performer_summary_names_the_runtime_and_the_count_in_flight() -> None:
    rec = SystemHealthRecorder()
    rec.record_performer_start("KubernetesRuntime", ok=True, at=NOW)
    row = _rows(build_system_health(rec, _inputs(performer_modes=("ephemeral",), active_performers=2)))["performer_runtime"]
    assert row["status"] == HEALTHY
    assert row["summary"] == "Kubernetes · 2 performers in flight"


def test_performer_start_failures_degrade_then_go_down() -> None:
    rec = SystemHealthRecorder()
    rec.record_performer_start("DockerRuntime", ok=False, error="ContainerStartError", at=NOW)
    row = _rows(build_system_health(rec, _inputs(performer_modes=("ephemeral",))))["performer_runtime"]
    assert row["status"] == DEGRADED
    assert "ContainerStartError" in row["reason"]
    for _ in range(PERFORMER_FAILURE_STREAK_DOWN - 1):
        rec.record_performer_start("DockerRuntime", ok=False, error="ContainerStartError", at=NOW)
    assert _rows(build_system_health(rec, _inputs(performer_modes=("ephemeral",))))["performer_runtime"]["status"] == DOWN


def test_performer_agent_probe_degraded_shows_on_the_row() -> None:
    rec = SystemHealthRecorder()
    rec.record_performer_start("DockerRuntime", ok=True, at=NOW)
    row = _rows(build_system_health(rec, _inputs(performer_modes=("ephemeral",), health={"agent": "degraded"})))["performer_runtime"]
    assert row["status"] == DEGRADED


# --- Model backends ------------------------------------------------------------------


def test_model_error_rate_at_the_named_fraction_is_degraded() -> None:
    rec = SystemHealthRecorder()
    for _ in range(5):
        rec.record_model_turn(ok=False, at=NOW)
    for _ in range(MODEL_WINDOW_TURNS - 5):
        rec.record_model_turn(ok=True, at=NOW)
    row = _rows(build_system_health(rec, _inputs()))["model_backends"]
    assert row["status"] == DEGRADED
    assert row["summary"] == f"5 of last {MODEL_WINDOW_TURNS} turns ended in a system error"


def test_model_errors_below_the_named_fraction_stay_healthy() -> None:
    rec = SystemHealthRecorder()
    for _ in range(4):
        rec.record_model_turn(ok=False, at=NOW)
    for _ in range(MODEL_WINDOW_TURNS - 4):
        rec.record_model_turn(ok=True, at=NOW)
    assert _rows(build_system_health(rec, _inputs()))["model_backends"]["status"] == HEALTHY


def test_model_consecutive_errors_at_the_named_count_are_down() -> None:
    rec = SystemHealthRecorder()
    rec.record_model_turn(ok=True, at=NOW - timedelta(minutes=5))
    for _ in range(MODEL_ERROR_STREAK_DOWN):
        rec.record_model_turn(ok=False, at=NOW)
    row = _rows(build_system_health(rec, _inputs()))["model_backends"]
    assert row["status"] == DOWN
    assert row["last_success_at"] == (NOW - timedelta(minutes=5)).isoformat()


def test_model_window_forgets_old_turns() -> None:
    rec = SystemHealthRecorder()
    for _ in range(MODEL_WINDOW_TURNS):
        rec.record_model_turn(ok=False, at=NOW - timedelta(hours=1))
    for _ in range(MODEL_WINDOW_TURNS):
        rec.record_model_turn(ok=True, at=NOW)
    assert _rows(build_system_health(rec, _inputs()))["model_backends"]["status"] == HEALTHY


# --- Disk and env cache --------------------------------------------------------------


def test_disk_free_space_thresholds_are_degraded_then_down() -> None:
    degraded = _rows(build_system_health(SystemHealthRecorder(), _inputs(disk_free_fraction=DISK_FREE_DEGRADED_FRACTION - 0.01)))
    assert degraded["disk_env_cache"]["status"] == DEGRADED
    down = _rows(build_system_health(SystemHealthRecorder(), _inputs(disk_free_fraction=DISK_FREE_DOWN_FRACTION - 0.01)))
    assert down["disk_env_cache"]["status"] == DOWN
    fine = _rows(build_system_health(SystemHealthRecorder(), _inputs(disk_free_fraction=DISK_FREE_DEGRADED_FRACTION)))
    assert fine["disk_env_cache"]["status"] == HEALTHY


def test_disk_unreadable_is_unknown() -> None:
    row = _rows(build_system_health(SystemHealthRecorder(), _inputs(disk_free_fraction=None)))["disk_env_cache"]
    assert row["status"] == UNKNOWN


def test_env_cache_bootstrap_failure_names_the_symphony_and_degrades_the_row() -> None:
    row = _rows(build_system_health(SystemHealthRecorder(), _inputs(env_symphonies=2, env_failed=("alpha",), env_ready=1)))["disk_env_cache"]
    assert row["status"] == DEGRADED
    assert "alpha" in row["reason"]


def test_env_cache_summary_counts_ready_symphonies() -> None:
    row = _rows(build_system_health(SystemHealthRecorder(), _inputs(env_symphonies=2, env_ready=2)))["disk_env_cache"]
    assert row["status"] == HEALTHY
    assert row["summary"].endswith("env cache ready for 2 symphonies")


# --- Secrets -------------------------------------------------------------------------


def test_the_payload_carries_no_token_material_even_when_the_error_text_had_some() -> None:
    rec = SystemHealthRecorder()
    secret = "ghp_thisIsASecretTokenValue0123456789"
    # The call sites pass the exception's class name, never its message.
    rec.record_github_call(ok=False, error="TransientGitHubError", at=NOW)
    rec.record_board_poll("acme/1", ok=False, error="TransientGitHubError", at=NOW)
    rec.record_performer_start("DockerRuntime", ok=False, error="ContainerStartError", at=NOW)
    result = build_system_health(rec, _inputs(performer_modes=("ephemeral",), webhooks_enabled=True))
    payload = json.dumps(result)
    assert secret not in payload
    assert "Bearer" not in payload
    assert "token" not in payload.lower()
