from __future__ import annotations

from coordinare.lib.runtime_events import build_runtime_event, event_level_for_category


def test_build_runtime_event_includes_fields() -> None:
    event = build_runtime_event(category="startup", message="ok", run_mode="shell")

    assert event["category"] == "startup"
    assert event["message"] == "ok"
    assert event["run_mode"] == "shell"


def test_event_level_for_category() -> None:
    assert event_level_for_category("failure") == "error"
    assert event_level_for_category("heartbeat") == "debug"
    assert event_level_for_category("activity") == "debug"
    assert event_level_for_category("shutdown") == "info"
    assert event_level_for_category("startup") == "info"
    assert event_level_for_category("state_change") == "info"
