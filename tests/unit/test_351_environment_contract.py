from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.models import CoverageAssessment, EnvironmentManifest
from pydantic import ValidationError


def manifest_data():
    return {
        "schema_version": 1,
        "symphony_name": "fixture",
        "source_commit": "a" * 40,
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


@pytest.mark.parametrize('path', ['.', './guide', 'a//guide', 'a/./guide', 'a/../guide', 'guide\n'])
def test_source_paths_reject_normalization_and_controls(path):
    data = manifest_data()
    data['inputs'] = {path: 'a' * 64}
    data['requirements'][0]['source'] = path
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


@pytest.mark.parametrize('path', ['{workspace}/bin', '{repo}/../outside', 'relative/bin', '${BAD:-fallback}'])
def test_execution_paths_reject_unknown_roots(path):
    data = manifest_data()
    data['activation']['path'] = [path]
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


def test_contract_processing_performance_budget() -> None:
    """Exercise the documented 1,000-check budget on every CI test runner."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[2] / "scripts/benchmark_environment_manifest.py")],
        capture_output=True, text=True, check=True, timeout=60,
    )
    measurement = json.loads(result.stdout)
    assert measurement


def assessment_data():
    return {
        "schema_version": 1,
        "manifest_digest": "b" * 64,
        "source_commit": "a" * 40,
        "verdict": "accept",
        "rationale": "Independently inspected repository inputs",
        "sources": ["guide.any"],
        "inputs": {"guide.any": "a" * 64},
        "policy_version": "351-v1",
    }


@pytest.mark.parametrize("model,fixture", [
    (EnvironmentManifest, manifest_data), (CoverageAssessment, assessment_data),
])
@pytest.mark.parametrize("commit", ["main", "abc", "a" * 39, "a" * 65, "A" * 40, "g" * 40, "a" * 40 + "\n"])
def test_contract_rejects_unpinned_source_commit(model, fixture, commit):
    data = fixture()
    data["source_commit"] = commit
    with pytest.raises(ValidationError, match="source_commit"):
        model.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("model,fixture", [
    (EnvironmentManifest, manifest_data), (CoverageAssessment, assessment_data),
])
@pytest.mark.parametrize("commit", ["0123456789abcdef" * 2 + "01234567", "abcdef0123456789" * 4])
def test_contract_accepts_pinned_source_commit(model, fixture, commit):
    data = fixture()
    data["source_commit"] = commit
    assert model.model_validate_json(json.dumps(data)).source_commit == commit
