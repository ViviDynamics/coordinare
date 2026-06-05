"""080 contract — shipped example configs use the catalog form, not inline model.

Guards the hard-cut migration (FR-006) and the catalog reference contract
(contracts/config-catalogs.md): every shipped config.example*.yaml must load
under the 080 schema, carry no inline performer `model:`/`base_url:` etc., and
resolve each moded role to a concrete model via mode → model_endpoint → endpoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinare.config import ProjectConfiguration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_CONFIGS = sorted(_REPO_ROOT.glob("config.example*.yaml"))
_REMOVED_INLINE_FIELDS = ("model", "base_url", "api_key_env", "auth_token_env")


def _load(path: Path) -> ProjectConfiguration:
    raw = yaml.safe_load(path.read_text())
    # Examples ship placeholder tokens / github_auth; override so we exercise the
    # 080 catalogs, not the unrelated token-placeholder rule.
    raw["github_token"] = "ghp_realtokenvalue1234567890"
    raw.pop("github_auth", None)
    return ProjectConfiguration(**raw)


def test_example_configs_exist():
    assert _EXAMPLE_CONFIGS, "no config.example*.yaml found"


@pytest.mark.parametrize("path", _EXAMPLE_CONFIGS, ids=lambda p: p.name)
def test_example_config_loads_under_080(path: Path):
    cfg = _load(path)  # raises if catalogs/refs are invalid
    assert isinstance(cfg, ProjectConfiguration)


@pytest.mark.parametrize("path", _EXAMPLE_CONFIGS, ids=lambda p: p.name)
def test_example_performers_have_no_inline_model_fields(path: Path):
    raw = yaml.safe_load(path.read_text())
    performers = raw.get("performers") or {}
    for role, rc in performers.items():
        if not isinstance(rc, dict):
            continue
        offending = [f for f in _REMOVED_INLINE_FIELDS if f in rc]
        assert not offending, f"{path.name} performers.{role} still sets inline {offending}"


@pytest.mark.parametrize("path", _EXAMPLE_CONFIGS, ids=lambda p: p.name)
def test_example_moded_roles_resolve_to_a_model(path: Path):
    cfg = _load(path)
    for role in (
        "default", "assessor", "architect", "implementer", "reviewer",
        "security", "qa", "tech_writer", "closer", "env_bootstrap",
    ):
        rc = getattr(cfg.performers, role, None)
        if rc is not None and rc.mode is not None:
            resolved = cfg.resolve_performer_dispatch_model(role)
            assert resolved.get("model"), f"{path.name} {role} mode {rc.mode!r} resolved no model"
