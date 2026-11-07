"""Two checks, or two install commands, with the same argv, cwd, env and scope are one obligation written twice."""

from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import (
    Command,
    EnvironmentManifest,
    ShardChecks,
    validate_records,
)
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from tests.unit.env_manifest_fixture import reviewed_metadata


def cmd(identifier, argv=("quality-runner",), **extra):
    return {"id": identifier, "argv": list(argv), **extra}


def manifest_data(*, install=(), checks=()):
    return reviewed_metadata(install=list(install), checks=list(checks))["environment_manifest"]


def duplicate_error(exc):
    errors = [error for error in exc.value.errors(include_url=False, include_input=False)
              if error["type"] == "duplicate_command_content"]
    assert errors, exc.value
    return errors[0]["ctx"]["pairs"]


def test_identical_checks_are_rejected_and_each_copy_names_the_first_it_repeats():
    checks = [cmd("c00-suite"), cmd("env-suite"), cmd("c01-suite"), cmd("c00-other", ["other"])]
    with pytest.raises(ValidationError) as exc:
        EnvironmentManifest.model_validate(manifest_data(checks=checks))

    assert duplicate_error(exc) == [["c00-suite", "env-suite"], ["c00-suite", "c01-suite"]]


def test_identical_install_commands_are_rejected():
    install = [cmd("install-a", ["make"]), cmd("install-b", ["make"])]
    with pytest.raises(ValidationError) as exc:
        EnvironmentManifest.model_validate(manifest_data(install=install, checks=[cmd("check-a", ["true"])]))

    assert sorted(map(sorted, duplicate_error(exc))) == [["install-a", "install-b"]]


def test_the_same_argv_with_a_different_cwd_env_or_scope_is_not_a_duplicate():
    checks = [cmd("check-a"), cmd("check-cwd", cwd="{cache}"), cmd("check-env", env={"MODE": "x"}),
              cmd("check-scope", scope=["qa"])]
    EnvironmentManifest.model_validate(manifest_data(checks=checks))


def test_an_install_and_a_check_with_the_same_argv_are_different_phases():
    EnvironmentManifest.model_validate(manifest_data(install=[cmd("install-tool", ["make"])],
                                                     checks=[cmd("check-tool", ["make"])]))


def test_scope_order_does_not_make_content_different():
    checks = [Command.model_validate(cmd("check-a", scope=["qa", "ci"])), Command.model_validate(cmd("check-b", scope=["ci", "qa"]))]
    with pytest.raises(PydanticCustomError) as exc:
        validate_records([], checks, [], [], {}, runtime=False)
    assert exc.value.type == "duplicate_command_content" and exc.value.context == {"pairs": [["check-a", "check-b"]]}


def test_a_group_author_sees_its_own_duplicates_in_its_phase():
    part = {"shard_checks": {"checks": [cmd("c00-a"), cmd("c00-b")], "criterion_checks": {}}}
    with pytest.raises(ValidationError) as exc:
        ShardChecks.model_validate(part["shard_checks"])

    assert sorted(map(sorted, duplicate_error(exc))) == [["c00-a", "c00-b"]]


def test_two_groups_copies_with_different_prerequisites_both_stand():
    """Dettmore e2e-53 (ruling 2026-11-04): c00 and c01 each wrote `bundle exec rspec`, only c01's after seeding; no
    merge could fold them. Different prerequisites mean different prior state, so both stand; anything else is still
    one obligation."""
    rspec = ("bundle", "exec", "rspec")
    seed, prep = cmd("c01-seed", ["seed"]), cmd("check-prepare", ["prepare"])
    EnvironmentManifest.model_validate(manifest_data(checks=[
        prep, seed, cmd("c00-rspec", rspec, depends_on=["check-prepare"]),
        cmd("c01-rspec", rspec, depends_on=["check-prepare", "c01-seed"])]))
    for checks, pair in [
        # Same prerequisites in two groups.
        ([prep, cmd("c00-rspec", rspec, depends_on=["check-prepare"]), cmd("c01-rspec", rspec, depends_on=["check-prepare"])],
         ["c00-rspec", "c01-rspec"]),
        # One group's two copies, prerequisites aside.
        ([prep, cmd("c00-rspec", rspec), cmd("c00-rspec-again", rspec, depends_on=["check-prepare"])],
         ["c00-rspec", "c00-rspec-again"]),
        # A group copy of a global check.
        ([prep, cmd("rspec-all", rspec), cmd("c00-rspec", rspec, depends_on=["check-prepare"])], ["rspec-all", "c00-rspec"]),
    ]:
        with pytest.raises(ValidationError) as exc:
            EnvironmentManifest.model_validate(manifest_data(checks=checks))
        assert duplicate_error(exc) == [pair]
