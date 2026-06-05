"""Spec 077 T005/T006 — diverse per-role backend routing contract.

Asserts (against a representative diverse-backend config fixture):
- T005: each lifecycle role resolves to its expected backend, and every role's
  model is the single shared `spark/qwen3.6:35b` (FR-001, FR-002).
- T006: each backend endpoint carries the LiteLLM provider-routing env its
  backend needs, so no backend can default to a vendor-hosted model
  (contracts/backend-provider-routing.md C-5).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.config import ProjectConfiguration

SHARED_MODEL = "spark/qwen3.6:35b"

# Expected role → backend mapping for the round (the 4 already-working backends;
# pi/opencode land in US2/US4; openclaw in US6).
EXPECTED_ROLE_BACKEND = {
    "assessor": "junie",
    "architect": "codex",
    "implementer": "codex",
    "security": "codex",
    "reviewer": "claude_code",
    "qa": "claude_code",
    "tech_writer": "hermes",
}

# C-5: the routing env each backend's endpoint MUST carry to reach LiteLLM.
REQUIRED_ENV_BY_BACKEND = {
    "codex": ["CODEX_PROVIDER_BASE_URL"],
    "claude_code": ["LITELLM_PROXY_BASE_URL"],
    "hermes": ["HERMES_BASE_URL"],
    "junie": ["JUNIE_PROVIDER_BASE_URL"],
}

_FIXTURE = """
github_org: acme
github_project_number: 1
github_token: ghp_fake
human_reviewers: [alice]
performer_endpoints:
  - id: codex-ephemeral
    mode: ephemeral
    roles: [architect, implementer, security]
    image: coordinare-performer:full
    env:
      BACKEND: codex
      CODEX_PROVIDER_BASE_URL: https://litellm.example/v1
  - id: claude-ephemeral
    mode: ephemeral
    roles: [qa, reviewer, env_bootstrap]
    image: coordinare-performer:full
    env:
      BACKEND: claude_code
      LITELLM_PROXY_BASE_URL: https://litellm.example
  - id: hermes-ephemeral
    mode: ephemeral
    roles: [tech_writer, closer]
    image: coordinare-performer:full
    env:
      BACKEND: hermes
      HERMES_BASE_URL: https://litellm.example/v1
  - id: junie-ephemeral
    mode: ephemeral
    roles: [assessor]
    image: coordinare-performer:full
    env:
      BACKEND: junie
      JUNIE_PROVIDER_BASE_URL: https://litellm.example/v1
performers:
  assessor: {backend: junie, model: spark/qwen3.6:35b}
  architect: {backend: codex, model: spark/qwen3.6:35b}
  implementer: {backend: codex, model: spark/qwen3.6:35b}
  security: {backend: codex, model: spark/qwen3.6:35b}
  reviewer: {backend: claude_code, model: spark/qwen3.6:35b}
  qa: {backend: claude_code, model: spark/qwen3.6:35b}
  tech_writer: {backend: hermes, model: spark/qwen3.6:35b}
  closer: {backend: hermes, model: spark/qwen3.6:35b}
  env_bootstrap: {backend: claude_code, model: spark/qwen3.6:35b}
"""


@pytest.fixture
def diverse_config(tmp_path: Path) -> ProjectConfiguration:
    p = tmp_path / "config.yaml"
    p.write_text(_FIXTURE)
    return ProjectConfiguration.from_yaml(p)


def test_each_role_routes_to_expected_backend(diverse_config: ProjectConfiguration) -> None:
    """T005: every lifecycle role resolves to its mapped backend."""
    for role, expected_backend in EXPECTED_ROLE_BACKEND.items():
        cfg = getattr(diverse_config.performers, role)
        assert cfg is not None, f"{role} not configured"
        assert cfg.backend == expected_backend, f"{role}: {cfg.backend} != {expected_backend}"


def test_every_role_uses_the_shared_model(diverse_config: ProjectConfiguration) -> None:
    """T005/FR-002: the variable under test is the backend — every role drives
    the single shared model."""
    for role in (*EXPECTED_ROLE_BACKEND, "closer", "env_bootstrap"):
        cfg = getattr(diverse_config.performers, role)
        assert cfg.model == SHARED_MODEL, f"{role} model {cfg.model!r} != {SHARED_MODEL!r}"


def test_endpoints_carry_litellm_provider_routing_env(diverse_config: ProjectConfiguration) -> None:
    """T006/C-5: each backend's endpoint carries the routing env it needs to
    reach LiteLLM (so it cannot default to a vendor-hosted model)."""
    for ep in diverse_config.performer_endpoints:
        backend = ep.env.get("BACKEND")
        required = REQUIRED_ENV_BY_BACKEND.get(backend)
        if required is None:
            continue
        for key in required:
            assert key in ep.env, f"endpoint {ep.id} ({backend}) missing routing env {key}"
