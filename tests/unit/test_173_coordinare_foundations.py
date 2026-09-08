"""Spec 173 coordinare foundations: registries, state fields, schema bump, config.

The registry-agreement test exists because the two registries live in different
packages and only agree by convention: a workflow the performer can run but
coordinare rejects at load is a role that never starts, and the reverse is a
config that validates and then fails mid-dispatch.
"""
from __future__ import annotations

import pytest


def test_the_two_workflow_registries_agree() -> None:
    from performer.workflows import SUPPORTED_WORKFLOWS

    from coordinare.config import KNOWN_WORKFLOWS

    assert set(SUPPORTED_WORKFLOWS) == set(KNOWN_WORKFLOWS), (
        "a name in one registry and not the other is either a role that never "
        "starts or a config that fails after it validates"
    )


@pytest.mark.parametrize("name", ["advocate", "curator"])
def test_both_intake_roles_are_registered(name: str) -> None:
    from performer.workflows import SUPPORTED_WORKFLOWS, is_supported_workflow

    from coordinare.config import KNOWN_WORKFLOWS

    assert name in SUPPORTED_WORKFLOWS and name in KNOWN_WORKFLOWS
    assert is_supported_workflow(name)


@pytest.mark.parametrize("step", ["advocate_classify", "curator_judge"])
def test_both_intake_steps_have_a_budget(step: str) -> None:
    from performer.workflows.budget import _STEP_BUDGETS

    assert _STEP_BUDGETS[step] > 0


# ---------------------------------------------------------------- state fields


@pytest.mark.parametrize("role", ["advocate", "curator"])
def test_the_per_role_gate_fields_exist_with_safe_defaults(role: str) -> None:
    from coordinare.models.env_cache import EnvCacheState

    state = EnvCacheState(symphony_name="s", sanitised_name="s", cache_dir="/tmp/s")
    assert getattr(state, f"{role}_in_flight") is False
    assert getattr(state, f"{role}_attempts") == 0
    assert getattr(state, f"{role}_exhausted") is False
    assert getattr(state, f"last_{role}_run_at") is None
    assert getattr(state, f"last_{role}_succeeded") is None
    assert getattr(state, f"last_{role}_error") is None
    assert getattr(state, f"last_{role}_issues_seen") == 0


@pytest.mark.parametrize("role", ["advocate", "curator"])
def test_the_in_flight_marker_is_never_persisted(role: str) -> None:
    """Deliberately transient: a crash mid-run must not leave a marker on disk
    that blocks the role forever. Same reasoning as bootstrap_in_flight."""
    from coordinare.state_store import EnvCacheStateSnapshot

    assert f"{role}_in_flight" not in EnvCacheStateSnapshot.model_fields


@pytest.mark.parametrize("role", ["advocate", "curator"])
def test_the_durable_gate_fields_are_persisted(role: str) -> None:
    from coordinare.state_store import EnvCacheStateSnapshot

    fields = EnvCacheStateSnapshot.model_fields
    for name in (
        f"{role}_attempts", f"{role}_exhausted", f"last_{role}_run_at",
        f"last_{role}_succeeded", f"last_{role}_error", f"last_{role}_issues_seen",
    ):
        assert name in fields, f"{name} must survive a restart"


def test_the_schema_version_was_bumped_for_the_new_persisted_fields() -> None:
    from coordinare.state_store import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION >= 20, (
        "new persisted fields without a bump leave a snapshot claiming a schema "
        "it does not have"
    )


def test_a_v19_snapshot_loads_with_defaults() -> None:
    from coordinare.state_store import EnvCacheStateSnapshot

    old = {"symphony_name": "s", "sanitised_name": "s", "cache_dir": "/tmp/s"}
    snap = EnvCacheStateSnapshot.model_validate(old)
    assert snap.advocate_attempts == 0 and snap.curator_exhausted is False
    assert snap.last_advocate_run_at is None


# ---------------------------------------------------------------- config


def test_the_retired_multi_provider_field_is_gone() -> None:
    from coordinare.config import AdvocateConfig

    assert "scoring_models" not in AdvocateConfig.model_fields, (
        "nothing read it, and the multi-provider path it described retires here"
    )


def test_the_advocate_keeps_a_scan_interval() -> None:
    from coordinare.config import AdvocateConfig

    assert AdvocateConfig().scan_interval_seconds > 30, (
        "the poll is ~30s and a webhook can shorten it; the interval must be a "
        "real floor, not once per cycle"
    )


def test_the_curator_is_off_by_default() -> None:
    from coordinare.config import CuratorConfig

    assert CuratorConfig().enabled is False


def test_the_curator_needs_a_repo_when_enabled() -> None:
    from coordinare.config import CuratorConfig

    with pytest.raises(ValueError, match="github_repo"):
        CuratorConfig(enabled=True, github_repo="   ")


def test_the_curator_may_not_target_the_dispatch_column() -> None:
    """The curator proposes; a human promotes. A backlog column equal to the
    column coordinare dispatches from would put work straight into the pipeline."""
    from coordinare.config import CuratorConfig

    with pytest.raises(ValueError, match="backlog_column"):
        CuratorConfig(enabled=True, github_repo="r", backlog_column="TODO")


def test_the_curator_accepts_a_real_backlog_column() -> None:
    from coordinare.config import CuratorConfig

    assert CuratorConfig(enabled=True, github_repo="r", backlog_column="Backlog").backlog_column == "Backlog"
