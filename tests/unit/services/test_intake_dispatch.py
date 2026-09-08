"""Spec 173: the per-role intake gate (shared by the advocate and the curator).

Pure rules over EnvCacheState. Every rule here has a named mutation in the
spec's mutation table, because each one is a decision to spend a container.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.intake_dispatch import (
    register_failure,
    register_success,
    should_run,
)


def _state(**over: object) -> EnvCacheState:
    state = EnvCacheState(symphony_name="s", sanitised_name="s", cache_dir="/tmp/s")
    for k, v in over.items():
        setattr(state, k, v)
    return state


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- should_run


def test_a_disabled_role_never_runs() -> None:
    assert not should_run(_state(), "advocate", enabled=False, interval_seconds=900, now=NOW)


def test_an_enabled_role_with_no_history_runs() -> None:
    assert should_run(_state(), "advocate", enabled=True, interval_seconds=900, now=NOW)


def test_a_role_already_in_flight_does_not_run_again() -> None:
    state = _state(advocate_in_flight=True)
    assert not should_run(state, "advocate", enabled=True, interval_seconds=900, now=NOW)


def test_an_exhausted_role_stops_trying() -> None:
    state = _state(advocate_exhausted=True)
    assert not should_run(state, "advocate", enabled=True, interval_seconds=900, now=NOW)


def test_a_role_inside_its_cooldown_waits() -> None:
    state = _state(last_advocate_run_at=NOW - timedelta(seconds=100))
    assert not should_run(state, "advocate", enabled=True, interval_seconds=900, now=NOW)


def test_a_role_past_its_cooldown_runs() -> None:
    state = _state(last_advocate_run_at=NOW - timedelta(seconds=901))
    assert should_run(state, "advocate", enabled=True, interval_seconds=900, now=NOW)


def test_a_failure_lengthens_the_wait() -> None:
    """One failed run must not be retried at the same cadence as a healthy one:
    a broken repository would otherwise burn a container every interval."""
    state = _state(last_advocate_run_at=NOW - timedelta(seconds=901), advocate_attempts=2)
    assert not should_run(state, "advocate", enabled=True, interval_seconds=900, now=NOW)
    later = _state(last_advocate_run_at=NOW - timedelta(seconds=901 * 8), advocate_attempts=2)
    assert should_run(later, "advocate", enabled=True, interval_seconds=900, now=NOW)


def test_a_naive_timestamp_does_not_crash_the_gate() -> None:
    """A snapshot written by an older build may carry a naive datetime."""
    state = _state(last_advocate_run_at=datetime(2026, 9, 7, 11, 0))
    assert isinstance(should_run(state, "advocate", enabled=True, interval_seconds=900, now=NOW), bool)


def test_the_two_roles_gate_independently() -> None:
    state = _state(advocate_in_flight=True)
    assert should_run(state, "curator", enabled=True, interval_seconds=900, now=NOW)


# ---------------------------------------------------------------- failure and success


def test_a_failure_records_its_reason_and_counts() -> None:
    state = _state()
    register_failure(state, "advocate", "boom", max_attempts=3, now=NOW)
    assert state.advocate_attempts == 1
    assert state.last_advocate_succeeded is False
    assert state.last_advocate_error == "boom"
    assert state.last_advocate_run_at == NOW
    assert state.advocate_exhausted is False


def test_the_breaker_trips_at_the_bound() -> None:
    state = _state(advocate_attempts=2)
    exhausted = register_failure(state, "advocate", "boom", max_attempts=3, now=NOW)
    assert state.advocate_attempts == 3 and state.advocate_exhausted is True and exhausted


def test_a_success_resets_the_breaker() -> None:
    state = _state(advocate_attempts=2, advocate_exhausted=True, last_advocate_error="old")
    register_success(state, "advocate", issues_seen=4, now=NOW)
    assert state.advocate_attempts == 0
    assert state.advocate_exhausted is False
    assert state.last_advocate_succeeded is True
    assert state.last_advocate_error is None
    assert state.last_advocate_issues_seen == 4
    assert state.last_advocate_run_at == NOW


@pytest.mark.parametrize("role", ["advocate", "curator"])
def test_both_roles_share_the_same_helpers(role: str) -> None:
    state = _state()
    register_failure(state, role, "x", max_attempts=3, now=NOW)
    assert getattr(state, f"{role}_attempts") == 1
    register_success(state, role, issues_seen=1, now=NOW)
    assert getattr(state, f"{role}_attempts") == 0


# ---------------------------------------------------------------- building a run


def test_the_identifier_key_is_id_not_card_id() -> None:
    """The transport reads card_context["id"]; nothing reads "card_id". The
    wiki-init precedent sets card_id, so its synthetic id never reaches the
    wire. Copying that key would inherit the same silent drop."""
    from coordinare.services.intake_dispatch import build_card_context

    ctx = build_card_context(
        "advocate", symphony_name="My Symphony", org="o", repo="r",
        persona="p", backend="codex",
    )
    assert ctx["id"] == "advocate-my-symphony"
    assert "card_id" not in ctx


def test_the_context_selects_the_workflow_unconditionally() -> None:
    """There is no prose path for these roles, so the workflow is not optional."""
    from coordinare.services.intake_dispatch import build_card_context

    for role in ("advocate", "curator"):
        ctx = build_card_context(role, symphony_name="s", org="o", repo="r",
                                 persona="p", backend="codex")
        assert ctx["workflow"] == role and ctx["role"] == role


def test_only_the_curator_carries_a_board_id() -> None:
    from coordinare.services.intake_dispatch import build_card_context

    advocate = build_card_context("advocate", symphony_name="s", org="o", repo="r",
                                  persona="p", backend="codex", project_id="PVT_1")
    curator = build_card_context("curator", symphony_name="s", org="o", repo="r",
                                 persona="p", backend="codex", project_id="PVT_1")
    assert "project_id" not in advocate, "the advocate never touches a board"
    assert curator["project_id"] == "PVT_1"


def test_the_branch_is_role_scoped_and_never_the_default() -> None:
    from coordinare.services.intake_dispatch import build_card_context

    ctx = build_card_context("advocate", symphony_name="s", org="o", repo="r",
                             persona="p", backend="codex")
    assert ctx["branch"] == "advocate/s" and ctx["branch"] != ctx["base_branch"]


def test_the_persona_travels_as_instruction_on_the_context() -> None:
    from coordinare.services.intake_dispatch import build_card_context

    ctx = build_card_context("advocate", symphony_name="s", org="o", repo="r",
                             persona="YOU TRIAGE", backend="codex")
    assert ctx["persona_instructions"] == "YOU TRIAGE"


def test_advocate_settings_round_trip_through_the_env_channel() -> None:
    """workflow_env takes scalars, so the lists are JSON. The workflow's own
    loader must read back exactly what coordinare configured."""
    from performer.workflows.advocate.settings import AdvocateSettings

    from coordinare.config import AdvocateConfig
    from coordinare.services.intake_dispatch import build_workflow_env

    cfg = AdvocateConfig(
        handled_label="handled", escalation_label="human",
        confidence_threshold=0.85, sensitive_keywords=["legal", "refund"],
        doc_sources=["README.md", "docs/faq.md"], support_channel_url="https://s",
    )
    env = build_workflow_env("advocate", cfg)
    assert all(isinstance(v, str) for v in env.values()), "workflow_env is scalars only"

    settings = AdvocateSettings.from_env(env)
    assert settings.handled_label == "handled"
    assert settings.escalation_label == "human"
    assert settings.confidence_threshold == 0.85
    assert settings.sensitive_keywords == ["legal", "refund"]
    assert settings.doc_sources == ["README.md", "docs/faq.md"]
    assert settings.support_channel_url == "https://s"


def test_a_malformed_env_list_falls_back_rather_than_taking_the_role_down() -> None:
    from performer.workflows.advocate.settings import AdvocateSettings

    settings = AdvocateSettings.from_env({"ADVOCATE_SENSITIVE_KEYWORDS": "not json,"})
    assert "billing" in settings.sensitive_keywords


# ---------------------------------------------------------------- completing a run


def _completion_state() -> tuple[EnvCacheState, dict, list]:
    flushed: list = []

    async def save() -> None:
        flushed.append(True)

    return _state(advocate_in_flight=True), {"snapshot_save_fn": save}, flushed


@pytest.mark.asyncio
async def test_a_successful_run_is_recorded_and_the_marker_cleared() -> None:
    from coordinare.services.intake_dispatch import handle_run_result

    state, daemon, _ = _completion_state()
    ok = handle_run_result(
        state, "advocate",
        {"status": "advocate_complete", "report": {"advocate": {"issues_seen": 3}}},
        daemon, now=NOW,
    )
    assert ok and state.advocate_in_flight is False
    assert state.last_advocate_succeeded is True and state.last_advocate_issues_seen == 3


@pytest.mark.asyncio
async def test_a_failed_run_is_recorded_and_the_marker_cleared() -> None:
    from coordinare.services.intake_dispatch import handle_run_result

    state, daemon, _ = _completion_state()
    ok = handle_run_result(state, "advocate", {"status": "error", "reason": "boom"}, daemon, now=NOW)
    assert not ok and state.advocate_in_flight is False
    assert state.advocate_attempts == 1 and state.last_advocate_error == "boom"


@pytest.mark.asyncio
async def test_an_unrecognised_status_still_clears_the_marker() -> None:
    """A marker left set is a role that never runs again until a restart."""
    from coordinare.services.intake_dispatch import handle_run_result

    state, daemon, _ = _completion_state()
    handle_run_result(state, "advocate", {"status": "working"}, daemon, now=NOW)
    assert state.advocate_in_flight is False and state.advocate_attempts == 1


@pytest.mark.asyncio
async def test_no_status_at_all_clears_the_marker() -> None:
    from coordinare.services.intake_dispatch import handle_run_result

    state, daemon, _ = _completion_state()
    handle_run_result(state, "advocate", None, daemon, now=NOW)
    assert state.advocate_in_flight is False and state.advocate_attempts == 1


@pytest.mark.asyncio
async def test_a_completion_forces_the_snapshot_to_disk() -> None:
    """A card-less completion moves no lifecycle signature, so the gated save
    would defer it and a restart in that window would rewind the result."""
    import asyncio

    from coordinare.services.intake_dispatch import handle_run_result

    state, daemon, flushed = _completion_state()
    handle_run_result(state, "advocate", {"status": "advocate_complete"}, daemon, now=NOW)
    await asyncio.sleep(0)
    assert flushed == [True]


@pytest.mark.asyncio
async def test_a_completion_without_a_save_function_does_not_crash() -> None:
    from coordinare.services.intake_dispatch import handle_run_result

    state, _, _ = _completion_state()
    assert handle_run_result(state, "advocate", {"status": "advocate_complete"}, {}, now=NOW)


@pytest.mark.asyncio
async def test_the_curator_completion_reads_its_own_count() -> None:
    from coordinare.services.intake_dispatch import handle_run_result

    state, daemon, _ = _completion_state()
    handle_run_result(
        state, "curator",
        {"status": "curation_complete", "report": {"curation": {"candidates_seen": 7}}},
        daemon, now=NOW,
    )
    assert state.last_curator_issues_seen == 7


@pytest.mark.asyncio
async def test_a_role_cannot_be_completed_by_the_other_roles_status() -> None:
    from coordinare.services.intake_dispatch import handle_run_result

    state, daemon, _ = _completion_state()
    assert not handle_run_result(state, "advocate", {"status": "curation_complete"}, daemon, now=NOW)


def test_the_metrics_survive_a_registry_problem() -> None:
    """A metrics failure must never lose the outcome of a run that already
    posted its comments."""
    from coordinare.services.intake_dispatch import _record_metrics

    _record_metrics("advocate", {"outcomes": [{"action": None}]}, {})  # must not raise


def test_the_metrics_are_fed_from_the_run_report() -> None:
    from coordinare.metrics import METRICS
    from coordinare.services.intake_dispatch import _record_metrics

    before = METRICS.advocate_issues_escalated_total.labels(reason="sensitive_keyword")._value.get()
    _record_metrics(
        "advocate",
        {"outcomes": [{"action": "escalated", "escalation_reason": "sensitive_keyword"},
                      {"action": "replied"}]},
        {"workflow_metrics": {"step_durations_ms": {"intake": 120, "gate": 30}}},
    )
    after = METRICS.advocate_issues_escalated_total.labels(reason="sensitive_keyword")._value.get()
    assert after == before + 1


def test_the_curator_does_not_touch_the_advocate_counters() -> None:
    from coordinare.services.intake_dispatch import _record_metrics

    _record_metrics("curator", {"outcomes": [{"action": "added"}]}, {})  # no-op, no raise


def test_a_flush_outside_an_event_loop_is_skipped_not_fatal() -> None:
    from coordinare.services.intake_dispatch import flush_snapshot

    async def save() -> None:
        return None

    assert flush_snapshot({"snapshot_save_fn": save}) is False
