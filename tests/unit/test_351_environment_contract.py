from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest
from pydantic import ValidationError


def manifest_data():
    return {
        "schema_version": 1,
        "symphony_name": "fixture",
        "source_commit": "abc",
        "inputs": {"guide.any": "a" * 64},
        "requirements": [
            {
                "id": "tool",
                "description": "arbitrary tool",
                "source": "guide.any",
                "checks": ["usable"],
                "required": True,
            },
        ],
        "install": [],
        "checks": [{"id": "usable", "argv": ["true"]}],
        "activation": {"path": [], "env": {}},
        "services": [],
        "empty_reason": None,
        "policy_version": "351-v1",
    }


def test_canonical_digest_covers_requiredness():
    data = manifest_data()
    first = EnvironmentManifest.model_validate(data)
    assert (
        first.digest()
        == EnvironmentManifest.model_validate(dict(reversed(list(data.items())))).digest()
    )
    data["checks"][0]["argv"] = ["false"]
    assert first.digest() != EnvironmentManifest.model_validate(data).digest()


@pytest.mark.parametrize(
    "mutation", ["version", "duplicate", "missing", "cycle", "optional", "source"],
)
def test_invalid_contract_rejected(mutation):
    data = manifest_data()
    if mutation == "version":
        del data["schema_version"]
    elif mutation == "duplicate":
        data["checks"].append(data["checks"][0])
    elif mutation == "missing":
        data["checks"][0]["depends_on"] = ["absent"]
    elif mutation == "cycle":
        data["checks"][0]["depends_on"] = ["usable"]
    elif mutation == "optional":
        data["checks"][0]["required"] = False
    else:
        data["requirements"][0]["source"] = "../escape"
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


def test_roundtrip_preserves_digest():
    manifest = EnvironmentManifest.model_validate(manifest_data())
    assert EnvironmentManifest.model_validate(manifest.model_dump()).digest() == manifest.digest()


def test_checks_cannot_depend_on_install_only_evidence():
    data = manifest_data()
    data["install"] = [{"id": "install", "argv": ["true"]}]
    data["checks"][0]["depends_on"] = ["install"]
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)
