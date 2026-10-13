"""#108: the subprocess transport is development-only, and the daemon says so.

With the subprocess transport the performer runs as a child of the daemon, on
the daemon's host, in the daemon's own clone. Nothing separates the
orchestrator from the code work. It stays the default (the Quick Start needs
no container runtime), so the daemon warns once per process when it builds one.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import structlog.testing

import coordinare.__main__ as main_module
from coordinare.config import PerformerRoleConfig

_EVENT = "transport.subprocess_no_isolation"


@pytest.fixture(autouse=True)
def _reset_warned(monkeypatch):
    monkeypatch.setattr(main_module, "_subprocess_isolation_warned", set())


def _config(transport: str = "subprocess"):
    return SimpleNamespace(
        agent_transport=transport, agent_executable="bin/run-performer",
        transport_timeout_seconds=30, github_token=None,
    )


def _events(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry["event"] == _EVENT]


def test_building_the_default_transport_warns() -> None:
    with structlog.testing.capture_logs() as logs:
        main_module._build_transport(_config())
    (event,) = _events(logs)
    assert event["log_level"] == "warning"
    assert "development" in event["detail"]
    assert "performer_endpoints" in event["remedy"] and "kubernetes" in event["remedy"]


def test_building_a_role_transport_warns() -> None:
    role = PerformerRoleConfig(transport="subprocess", executable="bin/run-performer")
    with structlog.testing.capture_logs() as logs:
        main_module._build_transport_for_role(role, _config())
    assert len(_events(logs)) == 1


def test_the_warning_is_emitted_once_per_process() -> None:
    role = PerformerRoleConfig(transport="subprocess", executable="bin/run-performer")
    with structlog.testing.capture_logs() as logs:
        main_module._build_transport(_config())
        main_module._build_transport(_config())
        main_module._build_transport_for_role(role, _config())
    assert len(_events(logs)) == 1


def test_no_warning_when_no_subprocess_transport_is_built() -> None:
    with structlog.testing.capture_logs() as logs, pytest.raises(ValueError, match="kubernetes"):
        main_module._build_transport(_config("kubernetes"))
    assert _events(logs) == []
