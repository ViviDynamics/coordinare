"""083 US3 — load-time judge-gate: the security role rejects weak-model configs (T021).

The security performer is the authoritative review lever. Spec 083 forbids
binding it to a model from the known weak-judge denylist (these models fail
contract-bound roles per MEMORY.md `project_qwen_coder_limitations`). The gate is
load-time and fail-closed: a denylisted `security` model raises at config load.

Scope is the `security` role ONLY — reviewer/assessor/etc. may use any model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinare.config import ProjectConfiguration

# The weak-judge denylist (spec 083 US3). Bare model names; catalog entries may
# carry a provider prefix (e.g. "local/qwen3.6:35b").
DENYLISTED = [
    "qwen3.6:35b",
    "qwq:32b",
    "qwen2.5:14b-instruct",
    "qwen2.5:32b",
    "qwen3-coder:30b",
]


def _cfg(tmp_path: Path, *, model_endpoints: list[dict], performers: dict) -> ProjectConfiguration:
    base = {
        "project_name": "t", "github_org": "o", "github_project_number": 1,
        "github_token": "ghp_x", "human_reviewers": ["a"],
        "endpoints": [
            {"name": "local-litellm", "kind": "litellm", "base_url": "http://localhost:4000",
             "auth_env": "LITELLM_PROXY_AUTH_TOKEN"},
            {"name": "anthropic-cloud", "kind": "anthropic", "auth_env": "ANTHROPIC_API_KEY"},
        ],
        "model_endpoints": model_endpoints,
        "modes": [
            {"name": "single", "strategy": "single", "tool": "the-model"},
        ],
        "performers": performers,
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(base))
    return ProjectConfiguration.from_yaml(p)


@pytest.mark.parametrize("weak_model", DENYLISTED)
def test_security_role_rejects_denylisted_model(tmp_path, weak_model):
    """A `security` role resolving to any denylisted model must raise at load."""
    with pytest.raises(ValueError, match=r"(?i)security.*denylist|denylist.*security|weak"):
        _cfg(
            tmp_path,
            model_endpoints=[
                {"name": "the-model", "endpoint": "local-litellm",
                 "model": f"local/{weak_model}"},
            ],
            performers={"security": {"backend": "claude_code", "mode": "single"}},
        )


def test_security_role_accepts_capable_model(tmp_path):
    """A `security` role on a capable (non-denylisted) model loads cleanly."""
    cfg = _cfg(
        tmp_path,
        model_endpoints=[
            {"name": "the-model", "endpoint": "anthropic-cloud",
             "model": "claude-sonnet-4-5"},
        ],
        performers={"security": {"backend": "claude_code", "mode": "single"}},
    )
    assert cfg.resolve_performer_dispatch_model("security")["model"] == "claude-sonnet-4-5"


@pytest.mark.parametrize("other_role", ["reviewer", "assessor"])
def test_non_security_roles_may_use_denylisted_model(tmp_path, other_role):
    """The gate is scoped to `security` — other roles accept any model (out of scope)."""
    cfg = _cfg(
        tmp_path,
        model_endpoints=[
            {"name": "the-model", "endpoint": "local-litellm",
             "model": "local/qwen3.6:35b"},
        ],
        performers={other_role: {"backend": "claude_code", "mode": "single"}},
    )
    assert cfg.resolve_performer_dispatch_model(other_role)["model"] == "local/qwen3.6:35b"
