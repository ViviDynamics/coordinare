"""Runtime environment references and declared execution roots stay distinct."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.runner import _expand, manifest_environment

from tests.unit.env_manifest_fixture import reviewed_metadata

ROOTS = {"cache": "/cache/{repo}", "repo": "/source", "scratch": "/scratch"}


@pytest.mark.parametrize(("value", "env", "expected"), [
    ("${repo}/bin", {"repo": "/operator/data"}, "/operator/data/bin"),
    ("{repo}/bin", {"repo": "/operator/data"}, "/source/bin"),
    ("{cache}/bin", {}, "/cache/{repo}/bin"),
    ("${VALUE}", {"VALUE": "${repo}/{cache}"}, "${repo}/{cache}"),
])
def test_expansion_is_single_pass_and_environment_values_are_opaque(value, env, expected):
    assert _expand(value, ROOTS, env) == expected


def test_undefined_lowercase_environment_reference_is_a_clear_hold():
    with pytest.raises(ValueError, match="unresolved environment reference: repo"):
        _expand("${repo}/bin", ROOTS, {})


def test_activation_keeps_operator_variable_distinct_from_source_root(monkeypatch):
    from coordinare_service_inference.environment.models import EnvironmentManifest

    monkeypatch.setenv("repo", "/operator/data")
    manifest = EnvironmentManifest.model_validate(reviewed_metadata(activation={
        "env": {"FROM_ENV": "${repo}/lib", "FROM_SOURCE": "{repo}/lib"}, "path": ["${repo}/bin"],
    })["environment_manifest"])
    env = manifest_environment(manifest, cache="/cache", repo="/pinned/source", scratch="/scratch")
    assert env["FROM_ENV"] == "/operator/data/lib"
    assert env["FROM_SOURCE"] == "/pinned/source/lib"
    assert env["PATH"].split(":")[0] == "/operator/data/bin"
