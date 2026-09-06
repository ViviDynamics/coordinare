"""T014 — FR-003: the role-level `workflow` field and its load-time validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import KNOWN_WORKFLOWS, PerformerRoleConfig


def test_workflow_defaults_to_none_so_existing_configs_are_unchanged():
    """FR-005: a role with no workflow takes the pre-164 single-backend path."""
    cfg = PerformerRoleConfig(backend="claude_code")
    assert cfg.workflow is None


def test_a_known_workflow_name_is_accepted():
    cfg = PerformerRoleConfig(backend="claude_code", workflow="qa")
    assert cfg.workflow == "qa"


def test_an_unknown_workflow_name_is_rejected_at_load_not_at_dispatch():
    """A typo'd name must fail while the operator is looking at the config,
    not silently produce a role that never runs its workflow."""
    with pytest.raises(ValidationError) as exc:
        PerformerRoleConfig(backend="claude_code", workflow="qaa")
    msg = str(exc.value)
    assert "qaa" in msg
    assert "qa" in msg, "the error must list what IS valid"


def test_empty_string_is_treated_as_absent_not_as_a_bad_name():
    """YAML `workflow:` with no value parses as None; `workflow: ""` should not
    be a validation error, it should mean the same thing."""
    assert PerformerRoleConfig(backend="claude_code", workflow="").workflow is None


def test_known_workflows_matches_the_performer_registry():
    """The coordinare-side list mirrors the performer's registry because the two
    are separately deployed and coordinare cannot import the performer package
    in production.  This test only runs where both are installed, and exists so
    the mirror cannot silently drift (same reasoning as the duplicated
    cdn_upload modules)."""
    performer_workflows = pytest.importorskip("performer.workflows")
    assert set(KNOWN_WORKFLOWS) == set(performer_workflows.SUPPORTED_WORKFLOWS), (
        "coordinare's KNOWN_WORKFLOWS has drifted from the performer registry"
    )


def test_workflow_env_accepts_the_scalars_yaml_naturally_produces():
    """Second review round, by hand.

    `PORT: 3000` unquoted -- how everyone writes YAML -- arrives as an int, and
    a strict dict[str, str] rejected it on BOTH the coordinare and performer
    sides. The natural spelling was a validation error.
    """
    cfg = PerformerRoleConfig(
        backend="x", workflow="qa",
        workflow_env={"PORT": 3000, "QA_APP_BOOT_TIMEOUT": 180.5, "DEBUG": True, "EMPTY": None},
    )
    assert cfg.workflow_env == {"PORT": "3000", "QA_APP_BOOT_TIMEOUT": "180.5", "DEBUG": "1", "EMPTY": ""}


def test_workflow_env_still_refuses_nested_values():
    """A dict or list is not an environment variable."""
    with pytest.raises(ValidationError):
        PerformerRoleConfig(backend="x", workflow_env={"PORT": {"a": 1}})
    with pytest.raises(ValidationError):
        PerformerRoleConfig(backend="x", workflow_env={"PORT": [3000]})


def test_workflow_env_coercion_matches_on_the_performer_side():
    """Both ends must agree, or a value coordinare accepts is dropped in transit."""
    performer_models = pytest.importorskip("performer.models")
    s = performer_models.Score(
        title="t", repo_url="https://github.com/o/r.git", branch="b",
        workflow_env={"PORT": 3000, "DEBUG": False},
    )
    assert s.workflow_env == {"PORT": "3000", "DEBUG": "0"}


def test_architect_is_accepted_on_a_role():
    cfg = PerformerRoleConfig(backend="codex", workflow="architect")
    assert cfg.workflow == "architect"
