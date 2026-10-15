"""156 review: a recovered symphony reads healthy, and the env column cannot read Ready while a
re-bootstrap is queued or a health failure is pending.

The failure and success bookkeeping is shared by the daemon (failed cycle, successful cycle) and
the snapshot builder, so these tests drive the same helpers the daemon calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from coordinare.dashboard.store import DashboardStore
from coordinare.dashboard.symphony_rows import (
    ENV_BOOTSTRAPPING,
    ENV_FAILED,
    ENV_READY,
    env_status,
)
from coordinare.lib.redaction import MAX_SESSION_ERROR_CHARS
from coordinare.services.symphony_failures import record_cycle_failure, record_cycle_success


def env_from_flags(**flags: Any) -> dict[str, str]:
    """Builds one symphony's env-cache state from named flags, then reads its env status."""
    cache = SimpleNamespace(
        bootstrap_in_flight=flags.get("in_flight", False),
        pending_sha="queued" if flags.get("rebootstrap_queued", False) else None,
        last_bootstrap_succeeded=flags.get("succeeded"),
        cache_dir_ready=flags.get("cache_ready", False),
        runtime_health_failed=flags.get("health_failed", False),
        last_bootstrap_error=flags.get("error"),
    )
    return env_status(configured=flags.get("configured", True), cache=cache)



def _sym_state() -> SimpleNamespace:
    return SimpleNamespace(cycle_count=0, error_count=0, last_error=None, active_sessions={})


# --- a failure, then a success ------------------------------------------------------- #


def test_a_failure_then_a_success_clears_the_run_of_failures() -> None:
    state = _sym_state()
    record_cycle_failure(state, RuntimeError("GitHub 502"))
    record_cycle_failure(state, RuntimeError("GitHub 502 again"))
    assert state.error_count == 2
    assert state.last_error == "GitHub 502 again"

    record_cycle_success(state)

    assert state.error_count == 0
    assert state.last_error is None


def test_a_failed_cycle_error_is_redacted_and_clipped_before_it_is_kept() -> None:
    state = _sym_state()
    record_cycle_failure(state, RuntimeError("x" * (MAX_SESSION_ERROR_CHARS * 2)))
    assert state.last_error is not None
    assert len(state.last_error) == MAX_SESSION_ERROR_CHARS


def test_a_recovered_symphony_reads_active_in_the_snapshot() -> None:
    state = _sym_state()
    record_cycle_failure(state, RuntimeError("timeout"))
    record_cycle_success(state)
    state.cycle_count = 5
    state.last_poll_at = None
    daemon = SimpleNamespace(state={
        "symphony_configs": {"flaky": SimpleNamespace(name="flaky", github_project_number=1, enabled=True,
                                                      env_bootstrap_performer_id=None, overrides=None)},
        "symphony_states": {"flaky": state},
        "config": None,
        "env_cache": {},
    })
    row = DashboardStore._build_symphonies_data(daemon, 5, None)[0]
    assert row["error_count"] == 0
    assert row["state"]["last_error"] is None


# --- env column: the 093 readiness gate ---------------------------------------------- #


def test_a_queued_re_bootstrap_blocks_ready() -> None:
    status = env_from_flags(configured=True, in_flight=False, rebootstrap_queued=True, succeeded=True, cache_ready=True,
                        health_failed=False, error=None)
    assert status["state"] == ENV_BOOTSTRAPPING
    assert "queued" in status["reason"]


def test_a_pending_health_failure_blocks_ready() -> None:
    status = env_from_flags(configured=True, in_flight=False, rebootstrap_queued=False, succeeded=True, cache_ready=True,
                        health_failed=True, error=None)
    assert status["state"] == ENV_FAILED
    assert "health check" in status["reason"]


def test_ready_needs_a_cache_a_successful_bootstrap_and_no_pending_failure() -> None:
    status = env_from_flags(configured=True, in_flight=False, rebootstrap_queued=False, succeeded=True, cache_ready=True,
                        health_failed=False, error=None)
    assert status["state"] == ENV_READY


def test_the_snapshot_clips_a_long_bootstrap_error_where_it_is_built() -> None:
    cache: Any = SimpleNamespace(bootstrap_in_flight=False, cache_dir_ready=False, last_bootstrap_succeeded=False,
                                 last_bootstrap_error="y" * 2000, pending_sha=None, runtime_health_failed=False)
    daemon = SimpleNamespace(state={
        "symphony_configs": {"alpha": SimpleNamespace(name="alpha", github_project_number=1, enabled=True,
                                                      env_bootstrap_performer_id="perf", overrides=None)},
        "symphony_states": {},
        "config": None,
        "env_cache": {"alpha": cache},
    })
    row = DashboardStore._build_symphonies_data(daemon, 0, None)[0]
    assert row["last_bootstrap_error"] is not None
    assert len(row["last_bootstrap_error"]) == MAX_SESSION_ERROR_CHARS
    assert row["env_status"]["state"] == ENV_FAILED
    assert len(row["env_status"]["reason"]) == MAX_SESSION_ERROR_CHARS
