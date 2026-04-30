"""Tests for performer endpoint hot-reload guard (spec 056, T012a)."""

from __future__ import annotations

import pytest

from coordinare.models.performer_endpoint import (
    HotReloadRejectedError,
    PerformerEndpointConfig,
    PerformerEndpointState,
    apply_endpoint_reload,
)


def _state(**overrides: object) -> PerformerEndpointState:
    base: dict[str, object] = {
        "id": "p1",
        "mode": "persistent",
        "endpoint": "http://host:8080",
    }
    base.update(overrides)
    return PerformerEndpointState(**base)  # type: ignore[arg-type]


def _config(**overrides: object) -> PerformerEndpointConfig:
    base: dict[str, object] = {
        "id": "p1",
        "mode": "persistent",
        "roles": ["performer"],
        "image": "performer:latest",
        "endpoint": "http://host:8080",
    }
    base.update(overrides)
    return PerformerEndpointConfig(**base)  # type: ignore[arg-type]


def test_reload_rejected_when_job_in_flight() -> None:
    current = _state(current_job_id="job-123")
    with pytest.raises(HotReloadRejectedError, match="job-123"):
        apply_endpoint_reload(current, _config())


def test_reload_rejected_on_id_mismatch() -> None:
    current = _state()
    with pytest.raises(HotReloadRejectedError, match="id mismatch"):
        apply_endpoint_reload(current, _config(id="p2"))


def test_reload_succeeds_when_idle_and_matching() -> None:
    apply_endpoint_reload(_state(), _config())
