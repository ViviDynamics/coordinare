"""The opt-in field harness cannot turn accepted proposals into unverified readiness."""

from __future__ import annotations

import json
import subprocess

import pytest
from scripts.validate_environment_repositories import (
    Profile,
    Snapshot,
    execution_authorized,
    final_readiness,
    new_profile_directory,
    validate_repository,
)


def test_scope_refuses_deployment_command_without_operator_review(tmp_path):
    assert not execution_authorized(
        tmp_path / "execution-approved.json",
        manifest_digest="a" * 64,
        source_commit="b" * 40,
        image="trusted@sha256:" + "c" * 64,
    )
    approval = {
        "scope": "deployment",
        "approved": True,
        "manifest_digest": "a" * 64,
        "source_commit": "b" * 40,
        "image_ref": "trusted@sha256:" + "c" * 64,
    }
    path = tmp_path / "execution-approved.json"
    path.write_text(json.dumps(approval))
    assert not execution_authorized(
        path, manifest_digest="a" * 64, source_commit="b" * 40, image=approval["image_ref"],
    )
    approval["scope"] = "environment_validation"
    path.write_text(json.dumps(approval))
    assert execution_authorized(
        path, manifest_digest="a" * 64, source_commit="b" * 40, image=approval["image_ref"],
    )
    assert not execution_authorized(
        path, manifest_digest="d" * 64, source_commit="b" * 40, image=approval["image_ref"],
    )


@pytest.mark.parametrize(
    "values", [(True, None, True), (True, False, True), (False, True, True), (True, True, False)],
)
def test_missing_clean_evidence_never_ready(values):
    assert not final_readiness(installer_passed=values[0], clean_passed=values[1], frozen=values[2])


def test_profiles_and_snapshots_do_not_overwrite_baseline(tmp_path):
    root = tmp_path / "profile"
    new_profile_directory(root)
    (root / "report.json").write_text('{"status":"held"}')
    with pytest.raises(FileExistsError):
        new_profile_directory(root)
    assert (root / "report.json").read_text() == '{"status":"held"}'
    assert root.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_hold_never_launches_installer(tmp_path, monkeypatch):
    source = tmp_path / "snapshot"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "guide").write_text("Use the documented tool")
    subprocess.run(["git", "-C", str(source), "add", "guide"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "snapshot",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    snapshot = Snapshot(source=source, commit=commit, org="fixture", name="sample")

    async def image_identity(image):
        return image, "linux/x86_64"

    async def invalid_model(messages):
        return "{}"

    async def forbidden(*args, **kwargs):
        raise AssertionError("a held proposal launched installation")

    monkeypatch.setattr(
        "coordinare.services.environment_capabilities._docker_identity", image_identity,
    )
    monkeypatch.setattr("scripts.validate_environment_repositories.execute_accepted", forbidden)
    result = await validate_repository(
        snapshot,
        image="trusted@sha256:" + "a" * 64,
        output=tmp_path / "attempt",
        profile=Profile(model="fixture", base_url="http://localhost:1", execute=True),
        chat=invalid_model,
    )
    assert result["status"] == "held"
    assert result["independent_review_reached"] is False
    assert result["installer_ran"] is False
    assert result["clean_consumer_ran"] is False
    assert result["live_daemon_exercised"] is False
    assert (source / "guide").read_text() == "Use the documented tool"


def test_image_must_contain_current_candidate_code(monkeypatch):
    from scripts.environment_validation_image import verify_image_code

    monkeypatch.setattr("scripts.environment_validation_image._docker", lambda *args: json.dumps({
        "platform": "linux/x86_64", "files": {"runner.py": "outdated"}}))
    with pytest.raises(ValueError, match="differs from the checkout"):
        verify_image_code("trusted@sha256:" + "c" * 64)


@pytest.mark.asyncio
async def test_scope_approval_cannot_waive_missing_clean_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts.validate_environment_repositories import execute_accepted

    from coordinare.services.environment_acceptance import accepted_environment
    from tests.unit.env_manifest_fixture import reviewed_cache, verification_output
    from tests.unit.test_353_bootstrap_pipeline import setup

    service, cache, state, capabilities = setup(tmp_path)
    reviewed_cache(cache, capabilities=capabilities)
    target = tmp_path / "case"
    target.mkdir()
    snapshot = SimpleNamespace(name="suite", commit=cache.bootstrap_commit_sha)

    async def verify():
        return None

    snapshot.verify = verify
    (target / "execution-approved.json").write_text(json.dumps({"scope": "environment_validation", "approved": True,
        "manifest_digest": accepted_environment(cache).digest(), "source_commit": snapshot.commit,
        "image_ref": capabilities.image_ref}))

    async def installer(*args):
        report = json.loads(verification_output(cache.environment_manifest))
        report["platform"] = capabilities.platform
        return 0, (json.dumps(report) + "\n").encode()

    async def missing_clean(*args, **kwargs):
        return None, "external verifier unavailable"

    async def forbidden_freeze(*args):
        raise AssertionError("missing clean evidence reached freeze")

    monkeypatch.setattr("scripts.validate_environment_repositories._install", installer)
    monkeypatch.setattr("scripts.validate_environment_repositories.verify_env_cache_clean", missing_clean)
    monkeypatch.setattr(service, "freeze_provenance", forbidden_freeze)
    result = {"ready_at_validation": False}
    await execute_accepted(snapshot, service, state, image=capabilities.image_ref, target=target,
        profile=Profile(model="fixture", base_url="http://localhost:1"), result=result)
    assert result["installer_ran"] is True
    assert result["clean_consumer_ran"] is True
    assert result["ready_at_validation"] is False
    assert result["reason_code"] == "clean_consumer_held"
