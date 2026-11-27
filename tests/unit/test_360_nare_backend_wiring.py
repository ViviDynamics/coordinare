"""#360: the coordinare side of the nare backend.

The adapter itself is covered by tests/contract/test_nare_contract.py and
test_nare_real_binary.py. These are the places that decide whether a role can
name ``backend: nare`` at all, and what reaches the adapter when it does.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from performer.backends import SUPPORTED_BACKENDS
from performer.proxy.launch import PROVIDER_BASE_URL_ENV, _with_verbatim_wire_path

from coordinare.config import PerformerRoleConfig, ProjectConfiguration
from coordinare.config_validation import SUPPORTED_PERFORMER_BACKENDS, _validate_performer_backends
from coordinare.presets import HARNESS_COMMANDS, HARNESS_DEFAULTS, expand_preset
from coordinare.services.performer_tuning import translate_tuning


def test_config_validation_knows_every_backend_the_performer_can_run() -> None:
    """The two lists drifted once: driver was registered but rejected by validation."""
    assert set(SUPPORTED_BACKENDS) == SUPPORTED_PERFORMER_BACKENDS


def test_backend_nare_passes_config_validation() -> None:
    assert _validate_performer_backends({"performers": {"reviewer": {"backend": "nare"}}}) == []


def test_backend_driver_is_rejected() -> None:
    [error] = _validate_performer_backends({"performers": {"reviewer": {"backend": "driver"}}})
    assert error.field_path == "performers.reviewer.backend"


def test_preflight_runs_the_nare_binary() -> None:
    assert HARNESS_COMMANDS["nare"] == "nare"


def test_preset_harness_nare_uses_nares_default_rail() -> None:
    expanded = expand_preset({
        "preset": "solo", "harness": "nare", "github_org": "o", "github_repo": "r",
    })
    assert expanded["performers"]["default"]["backend"] == "nare"
    assert expanded["endpoints"] == [
        {"name": "starter", "kind": "anthropic", "auth_env": "ANTHROPIC_API_KEY"},
    ]
    assert HARNESS_DEFAULTS["nare"][0] == "anthropic"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_effort_passes_through_to_nare(effort: str) -> None:
    role = PerformerRoleConfig(backend="nare", effort=effort)
    assert translate_tuning(role) == {"effort": effort}


def test_the_proxy_points_nare_at_a_v1_base() -> None:
    """nare's openai rail appends /chat/completions; the loopback serves /v1/..."""
    assert PROVIDER_BASE_URL_ENV["nare"] == "NARE_BASE_URL"
    assert _with_verbatim_wire_path("nare", "http://127.0.0.1:9") == "http://127.0.0.1:9/v1"
    assert "driver" not in PROVIDER_BASE_URL_ENV


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: dict[str, str]) -> ProjectConfiguration:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    return ProjectConfiguration.model_validate({
        "project_name": "test", "github_org": "org", "github_project_number": 1,
        "github_token": "test", "human_reviewers": ["reviewer"],
        "endpoints": [{"name": "ep", **endpoint}],
        "model_endpoints": [{"name": "m", "endpoint": "ep", "model": "some-model"}],
        "modes": [{"name": "mode", "strategy": "single", "tool": "m"}],
        "performers": {"reviewer": {"backend": "nare", "mode": "mode"}},
    })


@pytest.mark.parametrize("kind", ["anthropic", "openai"])
def test_a_native_endpoint_names_its_kind(tmp_path, monkeypatch, kind: str) -> None:
    """nare has two native rails; the auth env name alone cannot tell them apart."""
    config = _config(tmp_path, monkeypatch, {"kind": kind, "auth_env": "SOME_KEY"})
    resolved = config.resolve_performer_dispatch_model("reviewer")
    assert resolved == {"model": "some-model", "endpoint_kind": kind, "api_key_env": "SOME_KEY"}


def test_a_self_hosted_endpoint_names_its_kind(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path, monkeypatch, {
        "kind": "litellm", "base_url": "http://gateway/v1", "auth_env": "PROXY_KEY",
    })
    resolved = config.resolve_performer_dispatch_model("reviewer")
    assert resolved["endpoint_kind"] == "litellm"
    assert resolved["base_url"] == "http://gateway/v1"
    assert resolved["auth_token_env"] == "PROXY_KEY"
