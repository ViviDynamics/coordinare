"""156: the server data behind the Symphonies table: env state, session counts and spend per symphony.

The pure helpers are checked directly. The snapshot builder is checked with a symphony pair, so a
figure that belongs to one symphony is shown on that symphony's entry and on no other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from coordinare.dashboard.store import DashboardStore
from coordinare.dashboard.symphony_rows import (
    ENV_BOOTSTRAPPING,
    ENV_FAILED,
    ENV_NOT_BOOTSTRAPPED,
    ENV_NOT_CONFIGURED,
    ENV_READY,
    env_status,
    session_counts,
    week_spend,
)
from coordinare.lib.redaction import MAX_SESSION_ERROR_CHARS

_NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
_WEEK = {
    "tokens": {"total": 2050},
    "cost": {"usd": 0.13, "partial_usd": 0.13, "kind_usd": {}, "unknown_reason": None},
    "models": [],
    "pricing": {"as_of": "2026-10-01", "source": "table"},
}
_OTHER_WEEK = {
    "tokens": {"total": 9},
    "cost": {"usd": 0.5, "partial_usd": 0.5, "kind_usd": {}, "unknown_reason": None},
    "models": [],
    "pricing": {"as_of": "2026-10-01", "source": "table"},
}


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



def _ready() -> dict[str, str]:
    return env_from_flags(
        configured=True, in_flight=False, succeeded=True, cache_ready=True, error=None,
    )


# --- env state ------------------------------------------------------------------------ #


def test_an_unconfigured_symphony_says_no_bootstrap_performer_is_set() -> None:
    status = env_from_flags(
        configured=False, in_flight=True, succeeded=False, cache_ready=True, error="boom",
    )
    assert status == {
        "state": ENV_NOT_CONFIGURED,
        "reason": "No env bootstrap performer is configured for this symphony.",
    }


def test_a_running_bootstrap_is_bootstrapping() -> None:
    status = env_from_flags(
        configured=True, in_flight=True, succeeded=None, cache_ready=False, error=None,
    )
    assert status["state"] == ENV_BOOTSTRAPPING


def test_a_failed_bootstrap_carries_its_reason() -> None:
    status = env_from_flags(
        configured=True,
        in_flight=False,
        succeeded=False,
        cache_ready=False,
        error="image pull denied",
    )
    assert status == {"state": ENV_FAILED, "reason": "image pull denied"}


def test_a_failed_bootstrap_without_a_recorded_error_still_says_so() -> None:
    status = env_from_flags(
        configured=True, in_flight=False, succeeded=False, cache_ready=False, error=None,
    )
    assert status == {"state": ENV_FAILED, "reason": "the error was not recorded"}


def test_a_long_bootstrap_error_is_clipped() -> None:
    status = env_from_flags(
        configured=True, in_flight=False, succeeded=False, cache_ready=False, error="x" * 2000,
    )
    assert len(status["reason"]) == MAX_SESSION_ERROR_CHARS
    assert status["reason"].endswith("...")


def test_a_cached_and_succeeded_bootstrap_is_ready() -> None:
    assert _ready()["state"] == ENV_READY


def test_a_configured_symphony_never_bootstrapped_says_so() -> None:
    status = env_from_flags(
        configured=True, in_flight=False, succeeded=None, cache_ready=False, error=None,
    )
    assert status == {"state": ENV_NOT_BOOTSTRAPPED, "reason": "No env bootstrap has finished yet."}


# --- sessions ------------------------------------------------------------------------- #


def test_queued_sessions_are_not_active_and_paused_sessions_are_counted_on_their_own() -> None:
    sessions: dict[str, Any] = {
        "A": {"board_paused": False},
        "B": {"slot_queued_since": "2026-10-14T11:00:00+00:00"},
        "C": {"slot_queued_since": "2026-10-14T11:00:00+00:00", "board_paused": True},
        "D": {"board_paused": True},
    }
    assert session_counts(sessions) == {"active": 2, "queued": 2, "paused": 2}


def test_no_sessions_counts_zero() -> None:
    assert session_counts({}) == {"active": 0, "queued": 0, "paused": 0}


def test_a_session_without_a_mapping_still_counts_as_active() -> None:
    assert session_counts({"A": None}) == {"active": 1, "queued": 0, "paused": 0}


# --- spend ---------------------------------------------------------------------------- #


def test_spend_is_the_named_symphonys_week_and_nothing_else() -> None:
    block = {
        "tracked": True,
        "reason": None,
        "day": "2026-10-14",
        "week_start": "2026-10-08",
        "today": _OTHER_WEEK,
        "week": {"tokens": {"total": 2059}},
        "by_symphony": {
            "alpha": {"today": _WEEK, "week": _WEEK},
            "beta": {"today": _OTHER_WEEK, "week": _OTHER_WEEK},
        },
    }
    alpha = week_spend(block, "alpha")
    assert alpha["tracked"] is True
    assert alpha["cost"] == _WEEK["cost"]
    assert alpha["tokens"] == _WEEK["tokens"]
    assert "today" not in alpha and "by_symphony" not in alpha
    assert week_spend(block, "beta")["cost"] == _OTHER_WEEK["cost"]


def test_a_symphony_with_no_figures_is_not_tracked_rather_than_zero() -> None:
    block = {"tracked": True, "reason": None, "by_symphony": {}}
    assert week_spend(block, "gamma") == {
        "tracked": False,
        "reason": "No spend is recorded for this symphony.",
        "tokens": None,
        "cost": None,
    }


def test_untracked_spend_gives_the_reason() -> None:
    block = {"tracked": False, "reason": "transcripts disabled"}
    assert week_spend(block, "alpha") == {
        "tracked": False,
        "reason": "transcripts disabled",
        "tokens": None,
        "cost": None,
    }
    assert week_spend(None, "alpha")["tracked"] is False


# --- the snapshot builder ------------------------------------------------------------- #


def _sym_cfg(
    name: str, number: int, *, enabled: bool = True, env: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        github_project_number=number,
        enabled=enabled,
        env_bootstrap_performer_id=env,
        overrides=None,
    )


def _sym_state(
    sessions: dict[str, Any], *, error_count: int = 0, last_error: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        cycle_count=1,
        error_count=error_count,
        last_error=last_error,
        last_poll_at=_NOW,
        active_sessions=sessions,
        board_snapshot=None,
        board_titles={},
        board_issue_numbers={},
        board_issue_urls={},
        board_pr_urls={},
        session_skip_reasons={},
        todo_waits={},
        todo_capacity=None,
        active_card=None,
    )


def _daemon() -> SimpleNamespace:
    configs = {
        "alpha": _sym_cfg("alpha", 16, env="perf-env"),
        "beta": _sym_cfg("beta", 2, enabled=False),
    }
    states = {
        "alpha": _sym_state({"A1": {}, "A2": {"slot_queued_since": _NOW, "board_paused": True}}),
        "beta": _sym_state({"B1": {}}),
    }
    cache = SimpleNamespace(
        bootstrap_in_flight=False,
        cache_dir_ready=False,
        last_bootstrap_succeeded=False,
        last_bootstrap_error="registry denied",
    )
    return SimpleNamespace(
        state={
            "symphony_configs": configs,
            "symphony_states": states,
            "config": None,
            "env_cache": {"alpha": cache},
        },
    )


def test_each_symphony_entry_carries_its_own_status_inputs() -> None:
    token = {
        "tracked": True,
        "reason": None,
        "by_symphony": {"alpha": {"week": _WEEK}, "beta": {"week": _OTHER_WEEK}},
    }
    rows = {row["name"]: row for row in DashboardStore._build_symphonies_data(_daemon(), 3, token)}

    alpha, beta = rows["alpha"], rows["beta"]
    assert alpha["enabled"] is True and beta["enabled"] is False
    assert alpha["env_status"]["state"] == ENV_FAILED
    assert alpha["env_status"]["reason"] == "registry denied"
    assert beta["env_status"]["state"] == ENV_NOT_CONFIGURED
    assert (alpha["cards_active"], alpha["cards_queued"], alpha["cards_paused"]) == (1, 1, 1)
    assert (beta["cards_active"], beta["cards_queued"], beta["cards_paused"]) == (1, 0, 0)
    assert alpha["spend_week"]["cost"] == _WEEK["cost"]
    assert beta["spend_week"]["cost"] == _OTHER_WEEK["cost"]
    assert alpha["daemon_cycles_completed"] == 3


def test_a_symphony_with_no_runtime_state_has_empty_counts_and_no_spend_of_another() -> None:
    daemon = _daemon()
    daemon.state["symphony_states"] = {}
    token = {"tracked": True, "reason": None, "by_symphony": {"alpha": {"week": _WEEK}}}
    rows = {row["name"]: row for row in DashboardStore._build_symphonies_data(daemon, 0, token)}
    assert (
        rows["alpha"]["cards_active"],
        rows["alpha"]["cards_queued"],
        rows["alpha"]["cards_paused"],
    ) == (0, 0, 0)
    assert rows["beta"]["spend_week"]["tracked"] is False
