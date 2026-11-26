"""#382: nare is the harness every role falls back to when none is named.

One test per place that picks a backend without the operator naming one. An
explicit ``backend:`` or ``harness:`` still wins everywhere.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from coordinare import init_probes
from coordinare.config import PerformerRoleConfig, ProjectConfiguration
from coordinare.daemon import _BootstrapBackend
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.presets import HARNESS_DEFAULTS, expand_preset
from coordinare.services.http_performer_service import HTTPPerformerService

ROLES = (
    "advocate", "assessor", "architect", "implementer", "reviewer", "security",
    "qa", "tech_writer", "closer", "env_bootstrap",
)


def test_a_role_with_no_backend_is_nare() -> None:
    assert PerformerRoleConfig().backend == "nare"


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, performers: dict[str, Any]) -> ProjectConfiguration:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    return ProjectConfiguration.model_validate({
        "project_name": "test", "github_org": "org", "github_project_number": 1,
        "github_token": "test", "human_reviewers": ["reviewer"],
        "performers": performers,
    })


def test_every_configured_role_without_a_backend_resolves_to_nare(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path, monkeypatch, {role: {} for role in ROLES})
    for role in ROLES:
        resolved = config.performers.resolved_role(role)
        if resolved is None:  # curator and advocate configure through their own blocks
            continue
        assert resolved.backend == "nare", role


def test_an_explicit_backend_still_wins(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path, monkeypatch, {
        "default": {"backend": "codex"}, "reviewer": {"backend": "claude_code"}, "qa": {},
    })
    assert config.performers.resolved_role("reviewer").backend == "claude_code"
    assert config.performers.resolved_role("qa").backend == "codex"


def test_a_preset_without_a_harness_is_nare() -> None:
    expanded = expand_preset({"preset": "solo", "github_org": "o", "github_repo": "r"})
    assert expanded["performers"]["default"]["backend"] == "nare"
    assert expanded["performers"]["implementer"]["backend"] == "nare"
    assert expanded["endpoints"][0]["kind"] == "anthropic"


def test_a_preset_harness_still_wins() -> None:
    expanded = expand_preset({"preset": "solo", "harness": "codex", "github_org": "o", "github_repo": "r"})
    assert expanded["performers"]["default"]["backend"] == "codex"


def test_containers_preset_endpoint_runs_nare() -> None:
    expanded = expand_preset({"preset": "containers", "github_org": "o", "github_repo": "r"})
    assert expanded["performer_endpoints"][0]["env"] == {"BACKEND": "nare"}


def test_init_offers_nare_first() -> None:
    assert next(iter(HARNESS_DEFAULTS)) == "nare"


def test_init_falls_back_to_nare_when_no_harness_is_installed(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(init_probes, "read_command", _empty_command)
    for name in ("COORDINARE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert init_probes.detected_defaults("token")["harness"] == "nare"


async def _empty_command(*_args: str) -> str:
    return ""


def test_env_bootstrap_falls_back_to_nare() -> None:
    assert _BootstrapBackend().backend == "nare"


def _bootstrap_payload(**extra: Any):
    ctx: dict[str, Any] = {
        "job_type": "env_bootstrap",
        "symphony_name": "demo",
        "symphony_org": "org",
        "symphony_repo": "demo",
        "env_spec_files": ["README.md"],
        "env_spec_contents": {"README.md": "## Setup"},
        "cache_mount_path": "/devenv/demo-1",
    }
    ctx.update(extra)
    svc = HTTPPerformerService(PerformerEndpointConfig.model_validate({
        "id": "p", "mode": "ephemeral", "roles": ["env_bootstrap"], "image": "performer:base",
    }))
    return svc._build_env_bootstrap_payload(ctx)


def test_a_bootstrap_dispatch_without_a_backend_runs_nare() -> None:
    assert _bootstrap_payload().backend == "nare"


def test_a_nare_bootstrap_gets_its_rail_from_the_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("PROXY_KEY", "bearer")
    payload = _bootstrap_payload(
        backend="nare", endpoint_kind="litellm", base_url="http://gateway/v1",
        auth_token_env="PROXY_KEY",
    )
    secrets = {k: v.get_secret_value() for k, v in payload.secrets.items()}
    assert secrets["OPENAI_API_KEY"] == "bearer"
    assert "NARE_PROVIDER" not in secrets
    # The rail travels in metadata, which becomes the performer's Score.
    assert payload.metadata["endpoint_kind"] == "litellm"
    assert payload.metadata["base_url"] == "http://gateway/v1"
