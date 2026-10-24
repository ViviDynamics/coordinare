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
