"""The accepted contract, not ecosystem guesses, drives bootstrap artifacts."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from coordinare.daemon import _persist_env_cache
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import EnvCacheService
from coordinare.state_store import EnvCacheStateSnapshot
from tests.unit.test_351_environment_discovery import CAPABILITIES


def test_accepted_contract_survives_snapshot(tmp_path: Path) -> None:
    manifest = {"schema_version": 1, "source_commit": "a" * 40}
    assessment = {"verdict": "accept", "manifest_digest": "b" * 64}
    state = EnvCacheState(
        symphony_name="example", sanitised_name="example-abc", cache_dir=tmp_path,
        environment_manifest=manifest, environment_assessment=assessment,
    )
    saved = _persist_env_cache({"example": state})["example"]
    loaded = EnvCacheStateSnapshot.model_validate_json(saved.model_dump_json())
    assert loaded.environment_manifest == manifest
    assert loaded.environment_assessment == assessment


@pytest.mark.asyncio
async def test_manifest_build_requires_review_instead_of_parser_fallback(tmp_path: Path) -> None:
    config = SimpleNamespace(global_config=SimpleNamespace())
    service = EnvCacheService(config)
    cache = EnvCacheState(symphony_name="example", sanitised_name="example-abc", cache_dir=tmp_path)
    github = SimpleNamespace(
        get_repository_paths=AsyncMock(return_value=["strange/build.plan"]),
        get_file_content=AsyncMock(return_value="build the thing"),
    )
    with pytest.raises(RuntimeError, match=r"(?i)(review|model|discovery|commit)"):
        await service._build_manifest_artifacts(
            performer_mounts=(tmp_path,), symphony_name="example", repo="example",
            github_org="org", github_service=github, readme_contents={},
            current_sha="a" * 40, cache_dir=tmp_path, cache_mount_path="/devenv/example",
            llm_chat=None, cache_state=cache,
        )
    assert cache.environment_manifest is None
    assert cache.rendered_verify_sh is None


@pytest.mark.asyncio
async def test_source_head_participates_in_invalidation(tmp_path: Path) -> None:
    service = EnvCacheService(SimpleNamespace(global_config=SimpleNamespace()))
    cache = EnvCacheState(symphony_name="example", sanitised_name="example-abc", cache_dir=tmp_path)
    github = SimpleNamespace(get_branch_head_sha=AsyncMock(side_effect=["a" * 40, "b" * 40]))
    sym = SimpleNamespace(env_spec_files=["README.md"])
    effective = SimpleNamespace(github_org="org", project_name="example", base_branch="main")
    with patch.object(service, "_fetch_combined_sha", AsyncMock(return_value="same")), patch.object(
        service, "_inference_cache_suffix", AsyncMock(return_value=None),
    ):
        first = await service._current_manifest_sha("example", sym, github, effective, cache)
        second = await service._current_manifest_sha("example", sym, github, effective, cache)
    assert first != second, "new files must invalidate discovery even when existing spec files are unchanged"


def test_verifier_image_requires_immutable_identity() -> None:
    from coordinare.services.environment_acceptance import immutable_verifier_image

    with pytest.raises(ValueError, match="immutable"):
        immutable_verifier_image("registry.example/performer:latest")
    pinned = "registry.example/performer@sha256:" + "a" * 64
    assert immutable_verifier_image(pinned) == pinned


def test_missing_contract_cannot_supply_consumer_mount(tmp_path: Path) -> None:
    from coordinare.services.env_cache import get_env_volume_for_symphony

    (tmp_path / "activate.sh").write_text("export PATH=/tools/bin:$PATH\n")
    cache = EnvCacheState(
        symphony_name="example", sanitised_name="example-abc", cache_dir=tmp_path,
        rendered_activate_sh=(tmp_path / "activate.sh").read_text(),
        last_bootstrap_succeeded=True, cache_dir_ready=True,
    )
    assert get_env_volume_for_symphony(
        "example", {"example": cache}, is_bootstrap=False, performer_mounts=(tmp_path,),
    ) is None


def test_trusted_execution_report_cannot_omit_required_checks() -> None:
    import json

    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_acceptance import execution_report
    from tests.unit.env_manifest_fixture import reviewed_cache

    cache = reviewed_cache(EnvCacheState(symphony_name="sym", sanitised_name="sym-abc", cache_dir=Path("/tmp/cache")))
    assert cache.environment_assessment is not None
    digest = cache.environment_assessment["manifest_digest"]
    with pytest.raises(ValueError, match="required"):
        execution_report(json.dumps({"manifest_digest": digest, "platform": "linux/arm64", "results": [], "scope": "", "passed": True}), cache)


@pytest.mark.asyncio
async def test_reviewed_cache_requires_per_job_container() -> None:
    from coordinare.services.http_performer_service import HTTPPerformerService

    service = HTTPPerformerService.__new__(HTTPPerformerService)
    service._config = SimpleNamespace(mode="persistent", id="shared")
    payload = SimpleNamespace(role="implementer", env_cache_path="/devenv/cache", env_provenance={}, metadata={})
    with pytest.raises(ValueError, match="per-job"):
        await service._require_provenance_capability(payload, None)


def test_clean_report_image_must_match_consumer() -> None:
    from coordinare.services.environment_acceptance import consumer_image_matches

    assert consumer_image_matches({"image_ref": "a@sha256:" + "1" * 64}, "a@sha256:" + "1" * 64)
    assert not consumer_image_matches({"image_ref": "a@sha256:" + "1" * 64}, "a@sha256:" + "2" * 64)
    assert not consumer_image_matches(None, "a:latest")


@pytest.mark.asyncio
async def test_discovery_inventory_respects_outer_bootstrap_budget(tmp_path: Path) -> None:
    import asyncio

    async def stalled(*args, **kwargs):
        await asyncio.sleep(1)

    service = EnvCacheService(SimpleNamespace(global_config=SimpleNamespace(
        env_cache=SimpleNamespace(bootstrap_max_seconds=0.01))))
    cache = EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path, bootstrap_commit_sha="a" * 40)
    github = SimpleNamespace(get_repository_paths=stalled, prepare_execution_source=stalled)
    from coordinare.services.environment_discovery import EnvironmentDiscoveryError

    with pytest.raises(EnvironmentDiscoveryError, match="deadline"):
        await service._build_manifest_artifacts(
            performer_mounts=(tmp_path,), symphony_name="sym", repo="sym", github_org="org",
            github_service=github, readme_contents={}, current_sha="a" * 40, cache_dir=tmp_path,
            cache_mount_path="/devenv/sym", llm_chat=None, cache_state=cache,
            capabilities=CAPABILITIES.model_copy(update={"cache_mount_path": "/devenv/sym"}))


def test_clean_report_requires_service_lifecycle_evidence(tmp_path: Path) -> None:
    import json

    from coordinare.services.environment_acceptance import execution_report
    from tests.unit.env_manifest_fixture import reviewed_metadata

    metadata = reviewed_metadata(services=[{"id": "db", "start": {"id": "start", "argv": ["true"]},
        "health": {"id": "health", "argv": ["true"]}, "stop": {"id": "stop", "argv": ["true"]}}])
    cache = EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path,
        bootstrap_commit_sha="a" * 40, environment_manifest=metadata["environment_manifest"],
        environment_assessment=metadata["environment_assessment"],
        environment_capabilities=metadata["environment_capabilities"])
    digest = metadata["environment_manifest_digest"]
    report = {"manifest_digest": digest, "platform": "linux/arm64", "results": [{"id": "baseline", "status": "pass",
        "required": True, "exit_code": 0, "manifest_digest": digest}]}
    with pytest.raises(ValueError, match="required"):
        execution_report(json.dumps(report), cache)


@pytest.mark.parametrize("data", [
    {"env": {"DATABASE_URL": "postgres://alice:supersecret@db/app"}},
    {"argv": ["tool", "--url=https://alice:supersecret@example/path"]},
])
def test_manifest_cannot_persist_credential_uri(data: dict) -> None:
    from coordinare_service_inference.environment.models import Command
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="runtime reference"):
        Command.model_validate({"id": "check", "argv": ["true"], **data})


def test_saved_readiness_requires_complete_execution_report(tmp_path: Path) -> None:
    from coordinare.services.environment_acceptance import saved_execution_accepted
    from tests.unit.env_manifest_fixture import reviewed_cache

    cache = reviewed_cache(EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path))
    cache.environment_verification = None
    assert not saved_execution_accepted(cache)


def test_dashboard_cannot_show_ready_without_execution_evidence(tmp_path: Path) -> None:
    from coordinare.dashboard.symphony_rows import env_status
    from tests.unit.env_manifest_fixture import reviewed_cache

    cache = reviewed_cache(EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path,
        cache_dir_ready=True, last_bootstrap_succeeded=True))
    assert env_status(configured=True, cache=cache)["state"] == "ready"
    cache.environment_verification = None
    assert env_status(configured=True, cache=cache)["state"] != "ready"


def test_parent_report_cannot_label_nonzero_check_as_passing(tmp_path: Path) -> None:
    import json

    from coordinare.services.environment_acceptance import execution_report
    from tests.unit.env_manifest_fixture import reviewed_cache

    cache = reviewed_cache(EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path))
    report = dict(cache.environment_verification)
    report.pop("image_ref")
    report["results"][0]["exit_code"] = 1
    with pytest.raises(ValueError, match="required"):
        execution_report(json.dumps(report), cache)


@pytest.mark.asyncio
async def test_unrun_clean_verification_invalidates_previous_readiness(tmp_path: Path) -> None:
    from coordinare.services.env_cache import verify_env_cache_clean
    from tests.unit.env_manifest_fixture import reviewed_cache

    cache = reviewed_cache(EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path,
        rendered_verify_sh="true", cache_dir_ready=True, last_bootstrap_succeeded=True))
    with patch("coordinare.services.env_cache._verify_cache_refusal", return_value=None), patch(
        "coordinare.services.env_cache._docker_daemon_reachable", AsyncMock(return_value=False),
    ):
        result, _ = await verify_env_cache_clean({"env_cache": {"sym": cache}}, "sym", SimpleNamespace(), performer_mounts=())
    assert result is None
    assert cache.environment_verification is None


@pytest.mark.parametrize("command_id", ["start", "health", "init", "stop"])
@pytest.mark.parametrize("exit_code", [None, 1, 125])
def test_parent_report_rejects_nonzero_required_service_result(tmp_path: Path, command_id: str, exit_code: int | None) -> None:
    import json

    from coordinare.services.environment_acceptance import execution_report
    from tests.unit.env_manifest_fixture import (
        reviewed_cache,
        reviewed_metadata,
        verification_output,
    )

    cache = reviewed_cache(EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path))
    metadata = reviewed_metadata(services=[{
        "id": "db", **{name: {"id": name, "argv": ["true"]} for name in ("start", "health", "init", "stop")},
    }])
    cache.environment_manifest = metadata["environment_manifest"]
    cache.environment_assessment = metadata["environment_assessment"]
    report = json.loads(verification_output(cache.environment_manifest))
    for result in report["results"]:
        if result["id"] == command_id:
            result["exit_code"] = exit_code
    with pytest.raises(ValueError, match="exit code zero"):
        execution_report(json.dumps(report), cache)
