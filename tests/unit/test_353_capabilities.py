"""Runtime changes cannot ride an older independent environment approval."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import ExecutionCapabilities

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.environment_acceptance import accepted_environment
from coordinare.services.environment_capabilities import (
    capabilities_match,
    resolve_execution_capabilities,
)
from tests.unit.env_manifest_fixture import reviewed_metadata

IMAGE = "trusted/global@sha256:" + "a" * 64


def capabilities(**changes):
    return ExecutionCapabilities(schema_version=1, image_ref=IMAGE, platform="linux/x86_64",
        cache_mount_path="/devenv/sym", install_network="enabled", verify_network="disabled", **changes)


def cache_for(metadata):
    return EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir="/tmp/cache", bootstrap_commit_sha="a" * 40,
        environment_manifest=metadata["environment_manifest"], environment_assessment=metadata["environment_assessment"])


@pytest.mark.parametrize(("field", "value"), [
    ("image_ref", "other/image@sha256:" + "b" * 64), ("platform", "linux/aarch64"),
    ("cache_mount_path", "/another/cache"), ("install_network", "disabled"),
])
def test_capability_change_holds_saved_cache(field, value):
    current = capabilities()
    cache = cache_for(reviewed_metadata())
    cache.environment_manifest["capabilities_digest"] = current.digest()
    from coordinare_service_inference.environment.models import EnvironmentManifest

    cache.environment_assessment["manifest_digest"] = EnvironmentManifest.model_validate(cache.environment_manifest).digest()
    cache.environment_assessment["capabilities_digest"] = current.digest()
    assert capabilities_match(cache, current)
    assert not capabilities_match(cache, current.model_copy(update={field: value}))


def test_legacy_missing_capability_binding_requires_rediscovery():
    data = copy.deepcopy(reviewed_metadata())
    data["environment_manifest"].pop("capabilities_digest", None)
    data["environment_assessment"].pop("capabilities_digest", None)
    assert accepted_environment(cache_for(data)) is None


def test_performer_rejects_capability_digest_mismatch():
    from performer.environment_contract import accepted_manifest

    data = reviewed_metadata()
    data["environment_assessment"]["capabilities_digest"] = "b" * 64
    with pytest.raises(ValueError, match="capabilit"):
        accepted_manifest(data)


@pytest.mark.asyncio
async def test_prompt_capabilities_match_actual_runtime():
    global_config = SimpleNamespace(agent_transport="kubernetes", kubernetes_verifier_image=IMAGE,
        kubernetes_verifier_platform="linux/x86_64", env_cache=SimpleNamespace(kubernetes_verifier_network_isolated=True))
    cache = cache_for(reviewed_metadata())
    state = {"config": global_config, "env_cache": {"sym": cache}}
    result = await resolve_execution_capabilities(state, "sym")
    assert result.image_ref == IMAGE
    assert result.platform == "linux/x86_64"
    assert result.verify_network == "disabled"
    global_config.kubernetes_verifier_platform = None
    with pytest.raises(ValueError, match="platform"):
        await resolve_execution_capabilities(state, "sym")


def test_schema_explains_check_id_references():
    from coordinare_service_inference.environment.models import EnvironmentManifest

    schema = EnvironmentManifest.model_json_schema()
    assert "check" in schema["$defs"]["Requirement"]["properties"]["checks"]["description"].lower()
    assert "ID" in schema["$defs"]["Requirement"]["properties"]["checks"]["description"]


@pytest.mark.asyncio
async def test_discovery_and_fresh_review_receive_actual_capabilities():
    import json

    from tests.unit.test_351_environment_discovery import CAPABILITIES, ScriptedChat, discover

    chat = ScriptedChat()
    manifest, review = await discover(chat)
    for messages in (chat.calls[0], chat.calls[2]):
        context = json.loads(messages[1]["content"])
        assert context["execution_capabilities"]["image_ref"] == CAPABILITIES.image_ref
        assert context["execution_capabilities"]["platform"] == "linux/x86_64"
        assert context["execution_capabilities"]["verify_network"] == "disabled"
        assert "{scratch}" in context["execution_rules"]
    assert manifest.capabilities_digest == review.capabilities_digest == CAPABILITIES.digest()


@pytest.mark.asyncio
async def test_docker_capabilities_use_inspected_image_platform(monkeypatch):
    import json

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps([{"RepoDigests": [IMAGE], "Os": "linux", "Architecture": "arm64"}]).encode(), b""

    async def inspect_image(*args, **kwargs):
        assert args[:3] == ("docker", "image", "inspect")
        return Process()

    monkeypatch.setattr("coordinare.services.environment_capabilities.asyncio.create_subprocess_exec", inspect_image)
    state = {"config": SimpleNamespace(agent_transport="docker", performer_image="trusted/global:latest"),
        "env_cache": {"sym": cache_for(reviewed_metadata())}}
    result = await resolve_execution_capabilities(state, "sym")
    assert result.image_ref == IMAGE
    assert result.platform == "linux/aarch64"
    assert result.cache_mount_path == "/devenv/sym"


@pytest.mark.asyncio
async def test_distinct_bootstrap_image_cannot_claim_target_capabilities():
    global_config = SimpleNamespace(agent_transport="kubernetes", kubernetes_verifier_image=IMAGE,
        kubernetes_verifier_platform="linux/x86_64", env_cache=SimpleNamespace(kubernetes_verifier_network_isolated=True),
        performer_endpoints=[SimpleNamespace(id="bootstrap", image="other/image@sha256:" + "b" * 64)])
    state = {"config": global_config, "env_cache": {"sym": cache_for(reviewed_metadata())},
        "symphony_configs": {"sym": SimpleNamespace(env_bootstrap_performer_id="bootstrap")}}
    with pytest.raises(ValueError, match=r"bootstrap.*image"):
        await resolve_execution_capabilities(state, "sym")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "image", "platform"])
async def test_installer_dispatch_checks_target_identity_before_execution(kind):
    from tests.unit.test_351_performer_capability import PLATFORM, payload, service, status

    target = payload(image="performer@sha256:" + "a" * 64)
    if kind == "missing":
        target.metadata = {}
    elif kind == "image":
        target.metadata["environment_verification_image"] = "other@sha256:" + "b" * 64
    else:
        target.metadata["environment_verification_platform"] = PLATFORM + "-changed"
    with pytest.raises(ValueError, match=r"image|platform"):
        await service()._require_provenance_capability(target, status(1))


def test_target_description_survives_snapshot_before_installer_dispatch():
    from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache

    target = capabilities()
    cache = cache_for(reviewed_metadata(capabilities=target))
    cache.environment_capabilities = target.model_dump()
    snapshot = _persist_env_cache({"sym": cache})["sym"]
    restored = EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir="/tmp/cache")
    _rehydrate_env_cache({"env_cache": {"sym": restored}}, SimpleNamespace(env_cache={"sym": snapshot}))
    assert restored.environment_capabilities == target.model_dump()


@pytest.mark.asyncio
async def test_kubernetes_capabilities_use_actual_consumer_cache_root():
    global_config = SimpleNamespace(agent_transport="kubernetes", kubernetes_verifier_image=IMAGE,
        kubernetes_verifier_platform="linux/x86_64", env_cache=SimpleNamespace(kubernetes_verifier_network_isolated=True),
        performer_endpoints=[SimpleNamespace(roles=["implementer"], image=IMAGE, container_devenv_root="/custom")])
    result = await resolve_execution_capabilities({"config": global_config, "env_cache": {"sym": cache_for(reviewed_metadata())}}, "sym")
    assert result.cache_mount_path == "/custom/sym"
