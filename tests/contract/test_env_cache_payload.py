"""Contract test: env_cache_path round-trip through JobInitPayload (spec 060 T047)."""

from __future__ import annotations

import re

from coordinare.models.env_cache import BootstrapJobPayload
from coordinare.models.performer_endpoint import JobInitPayload


def test_bootstrap_job_payload_cache_mount_path_round_trips() -> None:
    payload = BootstrapJobPayload(
        symphony_name="my-project",
        symphony_org="myorg",
        symphony_repo="my-project",
        env_spec_contents={"README.md": "# My Project\n..."},
        cache_mount_path="/devenv/my-project-a1b2c3",
    )
    dumped = payload.model_dump()
    assert dumped["cache_mount_path"] == "/devenv/my-project-a1b2c3"
    assert dumped["job_type"] == "env_bootstrap"
    assert re.match(r"^/devenv/[a-z0-9_-]+-[0-9a-f]{6}$", dumped["cache_mount_path"]), (
        f"cache_mount_path {dumped['cache_mount_path']!r} does not match "
        "expected format /devenv/<slug>-<6-char-hex>"
    )
    restored = BootstrapJobPayload.model_validate(dumped)
    assert restored.cache_mount_path == "/devenv/my-project-a1b2c3"


def test_job_init_payload_metadata_carries_env_cache_path() -> None:
    """env_cache_path injected into card_context propagates through JobInitPayload.metadata."""
    card_context = {
        "id": "card-1",
        "role": "implementer",
        "backend": "claude_code",
        "persona_instructions": "be excellent",
        "env_cache_path": "/devenv/my-project-a1b2c3",
    }
    payload = JobInitPayload(
        job_id="job-1",
        card_id="card-1",
        role="implementer",
        backend="claude_code",
        persona="be excellent",
        repo_url="https://github.com/myorg/my-project",
        branch="main",
        metadata=card_context,
    )
    assert payload.metadata["env_cache_path"] == "/devenv/my-project-a1b2c3"


def test_job_init_payload_metadata_absent_when_no_cache() -> None:
    """When no env cache is configured, env_cache_path is absent from metadata."""
    card_context = {
        "id": "card-1",
        "role": "implementer",
        "backend": "claude_code",
        "persona_instructions": "be excellent",
    }
    payload = JobInitPayload(
        job_id="job-2",
        card_id="card-1",
        role="implementer",
        backend="claude_code",
        persona="be excellent",
        repo_url="https://github.com/myorg/my-project",
        branch="main",
        metadata=card_context,
    )
    assert "env_cache_path" not in payload.metadata
