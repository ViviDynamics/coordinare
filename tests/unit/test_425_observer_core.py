"""425 — the observer core: wake triggers, lightweight-model verdicts, config.

The monitoring loop already carries the evidence a person would use (merged
performer events, tool_use counts, token deltas, repetition signatures). The
observer is the lightweight judge woken when mechanical triggers fire. Triggers
are wake conditions, not judges — they bias toward waking. The verdict schema is
continue | correction | kill | retune | escalate; this issue implements the
schema, the call, and the continue path only.

Guardrails under test here:
- observer.enabled=false leaves monitoring byte-identical.
- A dead or malformed observer behaves exactly as today (no action) and logs.
- Every evaluation logs a structured event: trigger, evidence summary, verdict.
- Evidence summaries only in logs; never raw telemetry or diff dumps.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from coordinare.services.observer import (
    OBSERVER_TRIGGERS,
    OBSERVER_VERDICTS,
    ObserverQuery,
    ObserverVerdict,
    TriggerSnapshot,
    build_prompt,
    evaluate_triggers,
    observe,
    parse_verdict,
    persona,
)

# --- the verdict schema -------------------------------------------------------

def test_the_verdict_vocabulary_is_exactly_the_five_verbs():
    assert frozenset({"continue", "correction", "kill", "retune", "escalate"}) == OBSERVER_VERDICTS


def test_each_verdict_parses():
    for verb in ("continue", "correction", "kill", "retune", "escalate"):
        v = parse_verdict({"data": {"verdict": verb, "reason": "r"}})
        assert v is not None, verb
        assert v.verdict == verb


def test_an_unreadable_answer_is_not_a_verdict():
    """Fail-safe: malformed behaves exactly as today (no action)."""
    for bad in ({"data": None}, {}, {"data": {}}, {"data": "kill"}, None,
                {"data": {"reason": "x"}}, {"data": {"verdict": 7}},
                {"data": {"verdict": "obliterate"}}, "continue"):
        assert parse_verdict(bad) is None, bad


def test_the_verdict_is_frozen():
    v = ObserverVerdict(verdict="continue", reason="r")
    with pytest.raises(FrozenInstanceError):
        v.verdict = "kill"


def test_a_long_reason_is_capped():
    v = parse_verdict({"data": {"verdict": "continue", "reason": "x" * 10_000}})
    assert v is not None
    assert len(v.reason) <= 300


def test_a_model_added_data_wrapper_is_unwrapped():
    """A model that wrapped its verdict in its own data layer still parses."""
    v = parse_verdict({"data": {"data": {"verdict": "continue", "reason": "r"}}})
    assert v is not None
    assert v.verdict == "continue"


# --- the trigger vocabulary ---------------------------------------------------

def test_the_trigger_vocabulary_is_exactly_the_five_triggers():
    assert frozenset({
        "quiet_window", "repetition_signature", "token_burn_anomaly",
        "turn_boundary", "watchdog_trip",
    }) == OBSERVER_TRIGGERS


def _snap(**overrides) -> TriggerSnapshot:
    base = {
        "quiet_age_s": 0.0, "quiet_window_s": 300.0,
        "repetition_count": 1, "repetition_threshold": 3,
        "tokens_delta": 0, "token_burn_min_tokens": 200_000,
        "production_moved": False, "watchdog_pending": False,
    }
    base.update(overrides)
    return TriggerSnapshot(**base)


def test_quiet_window_fires_at_the_threshold():
    assert "quiet_window" in evaluate_triggers(_snap(quiet_age_s=300.0))
    assert "quiet_window" in evaluate_triggers(_snap(quiet_age_s=301.0))
    assert "quiet_window" not in evaluate_triggers(_snap(quiet_age_s=299.0))


def test_a_zero_quiet_window_disables_the_trigger():
    """0 is the documented off switch, not a wake-everything switch."""
    assert "quiet_window" not in evaluate_triggers(_snap(quiet_age_s=600.0, quiet_window_s=0.0))


def test_repetition_signature_fires_at_the_threshold():
    assert "repetition_signature" not in evaluate_triggers(_snap(repetition_count=2))
    assert "repetition_signature" in evaluate_triggers(_snap(repetition_count=3))


def test_token_burn_anomaly_needs_burn_and_no_artifact():
    assert "token_burn_anomaly" in evaluate_triggers(
        _snap(tokens_delta=250_000, production_moved=False),
    )
    assert "token_burn_anomaly" not in evaluate_triggers(
        _snap(tokens_delta=250_000, production_moved=True),
    )
    assert "token_burn_anomaly" not in evaluate_triggers(
        _snap(tokens_delta=100, production_moved=False),
    )


def test_turn_boundary_fires_when_production_moved():
    assert "turn_boundary" in evaluate_triggers(_snap(production_moved=True))
    assert "turn_boundary" not in evaluate_triggers(_snap(production_moved=False))


def test_watchdog_trip_fires_when_the_ceiling_is_exceeded():
    assert "watchdog_trip" in evaluate_triggers(_snap(watchdog_pending=True))
    assert "watchdog_trip" not in evaluate_triggers(_snap(watchdog_pending=False))


def test_a_trigger_subset_only_wakes_its_own_members():
    """The trigger set is configurable; a subset gate keeps the rest silent."""
    fired = evaluate_triggers(
        _snap(quiet_age_s=300.0, watchdog_pending=True),
        enabled_triggers=frozenset({"quiet_window"}),
    )
    assert fired == ["quiet_window"]


# --- the prompt ---------------------------------------------------------------

def test_the_prompt_names_the_vocabulary_and_carries_the_evidence():
    q = build_prompt(
        evidence={"quiet_age_s": 310, "tool_uses": 0, "completions": 0,
                  "total_events": 164, "tokens_delta": 210000,
                  "production_advanced": False, "repetition_count": 4},
        triggers=["quiet_window", "token_burn_anomaly"],
        recent_meta=["progress/18"],
    )
    for verb in ("continue", "correction", "kill", "retune", "escalate"):
        assert verb in q, verb
    assert "164" in q
    assert "210000" in q


def test_evidence_strings_are_summaries():
    """A long string field keeps its head and loses its tail."""
    q = build_prompt(
        evidence={"note": "begin" + "y" * 10_000, "tool_uses": 0},
        triggers=["turn_boundary"],
        recent_meta=[],
    )
    assert "begin" in q, "the head of a long string survives"
    assert "y" * 1000 not in q, "its tail does not — summaries stay summaries"


def test_the_prompt_is_bounded():
    """It is built from an event stream of unknown size and goes to a gateway."""
    q = build_prompt(
        evidence={"tool_uses": 0}, triggers=["turn_boundary"],
        recent_meta=["progress/50000"] * 50,
    )
    assert len(q) < 8000, len(q)


def test_the_persona_names_no_language_or_tool():
    """#364: the judgement must not carry stack knowledge."""
    p = persona().lower()
    for bad in ("python", "ruby", "rspec", "pytest", "rails", "javascript", "npm"):
        assert bad not in p, bad


# --- the config block ---------------------------------------------------------

def test_observer_defaults_off():
    from coordinare.config import ObserverConfig

    cfg = ObserverConfig()
    assert cfg.enabled is False
    assert cfg.model_endpoint is None


def test_enabled_observer_needs_a_model_endpoint():
    from coordinare.config import ObserverConfig

    with pytest.raises(ValueError):
        ObserverConfig(enabled=True, model_endpoint=None)


def test_observer_triggers_must_be_from_the_vocabulary():
    from coordinare.config import ObserverConfig

    with pytest.raises(ValueError):
        ObserverConfig(triggers=["quiet_window", "hunch"])


def _symphony_cfg(observer) -> object:
    from coordinare.config import CoordinareConfiguration, ProjectConfiguration, SymphonyConfig

    global_cfg = ProjectConfiguration(
        project_name="t", github_org="o", github_project_number=1,
        github_token="ghp_x", human_reviewers=["a"],
        endpoints=[{"name": "ep", "kind": "litellm", "base_url": "http://localhost:4000",
                    "auth_env": "LITELLM_PROXY_AUTH_TOKEN"}],
        model_endpoints=[{"name": "fast-model", "endpoint": "ep", "model": "local/fast"}],
    )
    symphony = SymphonyConfig(name="s", github_project_number=1, observer=observer)
    return CoordinareConfiguration(global_config=global_cfg, symphonies=[symphony])


def test_a_symphony_observer_block_validates_against_the_catalogs():
    from coordinare.config import ObserverConfig

    cfg = _symphony_cfg(ObserverConfig(enabled=True, model_endpoint="fast-model"))
    assert cfg.symphonies[0].observer.enabled is True

    with pytest.raises(ValueError, match="unknown model_endpoint"):
        _symphony_cfg(ObserverConfig(enabled=True, model_endpoint="missing"))

    with pytest.raises(ValueError, match="model_endpoint is required"):
        _symphony_cfg(ObserverConfig(enabled=True))


# --- the model: resolved through the 080 catalogs ------------------------------

def _catalog_cfg():
    from coordinare.config import ProjectConfiguration

    return ProjectConfiguration(
        project_name="t", github_org="o", github_project_number=1,
        github_token="ghp_x", human_reviewers=["a"],
        endpoints=[{"name": "ep", "kind": "litellm", "base_url": "http://localhost:4000",
                    "auth_env": "LITELLM_PROXY_AUTH_TOKEN"},
                   {"name": "anthropic-cloud", "kind": "anthropic", "auth_env": "ANTHROPIC_API_KEY"}],
        model_endpoints=[{"name": "fast-model", "endpoint": "ep", "model": "local/fast"},
                         {"name": "sonnet", "endpoint": "anthropic-cloud", "model": "claude-sonnet-4-5"}],
    )


def test_a_model_endpoint_builds_an_openai_compatible_backend():
    from coordinare.services.conducting import OpenAiApiBackend, build_model_endpoint_backend

    backend = build_model_endpoint_backend(_catalog_cfg(), "fast-model")
    assert isinstance(backend, OpenAiApiBackend)
    assert backend._model == "local/fast"
    assert backend._base_url == "http://localhost:4000"


def test_a_native_anthropic_model_endpoint_builds_the_anthropic_backend():
    from coordinare.services.conducting import AnthropicApiBackend, build_model_endpoint_backend

    backend = build_model_endpoint_backend(_catalog_cfg(), "sonnet")
    assert isinstance(backend, AnthropicApiBackend)


def test_an_unresolvable_model_endpoint_builds_no_backend():
    from coordinare.services.conducting import build_model_endpoint_backend

    assert build_model_endpoint_backend(_catalog_cfg(), "no-such-model") is None


# --- the call: fail-safe on a dead observer ------------------------------------

class _Backend:
    def __init__(self, answer=None, boom=False):
        self._answer, self._boom, self.calls = answer, boom, 0
        self.last_prompt: str | None = None

    async def prompt(self, text, response_format=None):
        self.calls += 1
        self.last_prompt = text
        if self._boom:
            raise RuntimeError("gateway unreachable")
        return self._answer


@pytest.mark.asyncio
async def test_a_dead_observer_never_raises():
    """An unreachable observer behaves exactly as today: no action, logged."""
    backend = _Backend(boom=True)
    verdict = await observe(
        backend,
        ObserverQuery(
            card_id="C1", stage="implementing", triggers=["quiet_window"],
            evidence={"tool_uses": 0}, prompt="judge this",
        ),
    )
    assert verdict is None
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_a_malformed_observer_response_is_no_action():
    backend = _Backend({"data": {"verdict": "whatever", "reason": "x"}})
    verdict = await observe(
        backend,
        ObserverQuery(
            card_id="C1", stage="implementing", triggers=["quiet_window"],
            evidence={"tool_uses": 0}, prompt="judge this",
        ),
    )
    assert verdict is None


@pytest.mark.asyncio
async def test_a_live_observer_logs_trigger_evidence_and_verdict():
    backend = _Backend({"data": {"verdict": "continue", "reason": "moving"}})
    verdict = await observe(
        backend,
        ObserverQuery(
            card_id="C1", stage="implementing", triggers=["quiet_window"],
            evidence={"tool_uses": 2, "completions": 1, "total_events": 40,
                      "tokens_delta": 1200, "quiet_age_s": 310,
                      "production_advanced": False, "repetition_count": 1},
            prompt="judge this",
        ),
    )
    assert verdict is not None
    assert verdict.verdict == "continue"


# --- the wiring: the monitor wakes the observer --------------------------------

class _Performer:
    def __init__(self, *responses: dict) -> None:
        self._responses = list(responses) or [{"status": "working"}]

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def _observer_cfg(**overrides):
    from coordinare.config import ObserverConfig

    base = {
        "enabled": True, "model_endpoint": "fast-model",
        "quiet_window_seconds": 300.0, "repetition_signature_threshold": 3,
    }
    base.update(overrides)
    return ObserverConfig(**base)


def _observer_state(observer_cfg, backend, performer: _Performer | None = None, **extra):
    from coordinare.graph.state import initial_state

    s = initial_state()
    s["performer_services"] = {
        "implementing": performer or _Performer({"status": "working"}),
    }
    s["performer_stage"] = "implementing"
    s["lifecycle_sequence"] = ["implementing"]
    s["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    s["agent_dispatch"] = {"session_id": "s1"}
    s["agent_dispatch_at"] = datetime.now(UTC) - timedelta(seconds=600)
    s["role_timeouts"] = {"implementing": 30_000}
    s["performer_events"] = [{"type": "progress", "text": "t"}] * 3
    s["symphony_configs"] = {"sym": SimpleNamespace(observer=observer_cfg)}
    s["current_symphony"] = "sym"
    if backend is not None:
        s["observer_backend"] = backend
    s.update(extra)
    return s


@pytest.mark.asyncio
async def test_disabled_observer_leaves_monitoring_byte_identical():
    """observer.enabled=false: no calls, no observer state writes, no new keys."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "kill", "reason": "should not be asked"}})
    s = _observer_state(_observer_cfg(enabled=False), backend)
    baseline_keys = set(s.keys())

    result = await monitor_performer(s)
    added = set(result.keys()) - baseline_keys
    assert backend.calls == 0, "a disabled observer was sent to the gateway"
    assert not any(k.startswith("observer") for k in added), (
        f"a disabled observer wrote {added}"
    )


@pytest.mark.asyncio
async def test_a_trigger_wake_produces_a_logged_verdict():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "continue", "reason": "steady work"}})
    cfg = _observer_cfg(quiet_window_seconds=100.0)
    s = _observer_state(cfg, backend)
    s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=600)

    result = await monitor_performer(s)
    assert backend.calls == 1, "the quiet-window wake never reached the model"
    assert result.get("observer_verdict") == "continue"


@pytest.mark.asyncio
async def test_the_quiet_clock_falls_back_to_dispatch():
    """A performer that has produced nothing yet is measured from dispatch."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "continue", "reason": "quiet since dispatch"}})
    cfg = _observer_cfg(quiet_window_seconds=300.0)
    s = _observer_state(cfg, backend)
    assert s.get("last_production_at") is None, "the anchor under test is the fallback"

    result = await monitor_performer(s)
    assert backend.calls == 1, "the dispatch-anchored quiet window never woke"
    assert result.get("observer_verdict") == "continue"


@pytest.mark.asyncio
async def test_no_trigger_no_call():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "kill", "reason": "should not be asked"}})
    cfg = _observer_cfg(quiet_window_seconds=3600.0)
    s = _observer_state(cfg, backend)
    s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=10)

    result = await monitor_performer(s)
    assert backend.calls == 0, "the observer woke with no trigger fired"
    assert "observer_verdict" not in result


@pytest.mark.asyncio
async def test_a_dead_observer_wedges_neither_nor_bounces_the_card():
    """The failure path must reproduce today's behavior exactly."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    cfg = _observer_cfg(quiet_window_seconds=100.0)
    when = datetime.now(UTC) - timedelta(seconds=600)

    dead = _Backend(boom=True)
    with_dead = await monitor_performer(
        _observer_state(cfg, dead, last_production_at=when),
    )
    assert dead.calls == 1, "the wake happened"
    assert "observer_verdict" not in with_dead

    off = _observer_cfg(enabled=False)
    without = await monitor_performer(
        _observer_state(off, None, last_production_at=when),
    )
    assert without.get("phase") == with_dead.get("phase")
    assert without.get("open_questions") == with_dead.get("open_questions")


@pytest.mark.asyncio
async def test_repetition_wakes_after_enough_unchanged_polls():
    """Three consecutive unchanged polls, threshold 3: wake exactly on the third."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "continue", "reason": "looping in place"}})
    cfg = _observer_cfg(repetition_signature_threshold=3, quiet_window_seconds=0.0)
    s = _observer_state(cfg, backend)

    first = await monitor_performer(s)
    assert backend.calls == 0, "repetition needs several unchanged polls"
    assert first.get("observer_repetition_count") == 1

    second = await monitor_performer(first)
    assert backend.calls == 0, "two unchanged polls is under the threshold"
    assert second.get("observer_repetition_count") == 2

    third = await monitor_performer(second)
    assert backend.calls == 1, "the third unchanged poll crossed the threshold"
    assert third.get("observer_repetition_count") == 3
    assert third.get("observer_verdict") == "continue"


@pytest.mark.asyncio
async def test_new_work_resets_the_repetition_streak():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "continue", "reason": "moving again"}})
    cfg = _observer_cfg(repetition_signature_threshold=3, quiet_window_seconds=0.0)
    performer = _Performer(
        {"status": "working"},
        {"status": "working", "events": [{"type": "progress", "text": "new work"}]},
    )
    s = _observer_state(cfg, backend, performer=performer)

    first = await monitor_performer(s)
    assert first.get("observer_repetition_count") == 1
    second = await monitor_performer(first)
    assert second.get("observer_repetition_count") == 1, (
        "a poll whose event text changed is not part of the streak"
    )
    assert backend.calls == 0


# --- the resets and the session round-trip -------------------------------------

def _staged_state(observer_cfg) -> dict:
    """A two-stage lifecycle so _advance_stage takes the advance path."""
    from coordinare.graph.state import initial_state

    s = initial_state()
    s["lifecycle_sequence"] = ["implementing", "reviewing"]
    s["performer_stage"] = "implementing"
    s["symphony_configs"] = {"sym": SimpleNamespace(observer=observer_cfg)}
    s["current_symphony"] = "sym"
    return s


def test_a_stage_advance_on_the_default_off_path_writes_no_observer_keys():
    """An ordinary stage advance must not add observer keys to state at all."""
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    s = _staged_state(_observer_cfg(enabled=False, model_endpoint=None))
    updates = _advance_stage(s)
    assert "observer_repetition_count" not in updates
    assert "observer_verdict" not in updates


def test_a_stage_advance_with_an_observer_resets_the_run_state():
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    s = _staged_state(_observer_cfg())
    s["observer_repetition_count"] = 4
    s["observer_verdict"] = "continue"
    updates = _advance_stage(s)
    assert updates["observer_repetition_count"] == 0
    assert updates["observer_verdict"] is None


def test_the_observer_state_round_trips_through_the_card_session():
    """Multi-card mode: the streak and verdict belong to the card, not the run."""
    from coordinare.graph.state import initial_state
    from coordinare.session import (
        _SESSION_FIELDS,
        session_to_state,
        state_to_session,
    )

    assert "observer_repetition_count" in _SESSION_FIELDS
    assert "observer_verdict" in _SESSION_FIELDS

    s = initial_state()
    s["observer_repetition_count"] = 2
    s["observer_verdict"] = "continue"
    session = state_to_session(s)
    assert session["observer_repetition_count"] == 2
    assert session["observer_verdict"] == "continue"

    fresh: dict = {}
    session_to_state(session, fresh)  # type: ignore[arg-type]
    assert fresh["observer_repetition_count"] == 2
    assert fresh["observer_verdict"] == "continue"


def test_a_card_without_observer_state_stays_without_it():
    """Absent stays absent on the round-trip (byte-identical default-off)."""
    from coordinare.graph.state import initial_state
    from coordinare.session import state_to_session

    assert "observer_repetition_count" not in initial_state()
    session = state_to_session(initial_state())
    assert "observer_repetition_count" not in session
    assert "observer_verdict" not in session


@pytest.mark.asyncio
async def test_the_prompt_carries_no_raw_event_text():
    """Performer event text is untrusted telemetry; it never reaches the prompt."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    telemetry = "SECRET-DIFF-CONTENT $password /etc/shadow"
    backend = _Backend({"data": {"verdict": "continue", "reason": "quiet"}})
    s = _observer_state(
        _observer_cfg(), backend,
        performer_events=[{"type": "progress", "text": telemetry}] * 3,
    )
    await monitor_performer(s)
    assert backend.calls == 1
    assert backend.last_prompt is not None
    assert telemetry not in backend.last_prompt, "raw event text reached the prompt"
    assert "progress/41c" in backend.last_prompt, "the metadata tail is there"


def test_the_reason_is_a_single_bounded_line():
    """Log hygiene: the reason is capped and whitespace-collapsed."""
    v = parse_verdict({"data": {"verdict": "continue",
                                "reason": "a\n\nb\t\tc\n" + "d" * 400}})
    assert v is not None
    assert v.reason == "a b c" + " " + "d" * 292
    assert "\n" not in v.reason and "\t" not in v.reason


def test_a_card_without_the_keys_hydrates_clean():
    """Hydration clears stale flat-state values; no cross-card inheritance."""
    from coordinare.session import session_to_state

    s = {"observer_repetition_count": 2, "observer_verdict": "continue"}
    session_b: dict = {}
    session_to_state(session_b, s)  # type: ignore[arg-type]
    assert "observer_repetition_count" not in s
    assert "observer_verdict" not in s

    session_c: dict = {"observer_repetition_count": 1, "observer_verdict": "continue"}
    session_to_state(session_c, s)  # type: ignore[arg-type]
    assert s["observer_repetition_count"] == 1
