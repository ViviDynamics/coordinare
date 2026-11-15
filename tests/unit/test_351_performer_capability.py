"""Reviewed manifests require an explicit image capability and isolated job container."""
from __future__ import annotations

import platform
from types import SimpleNamespace

import pytest

from coordinare.models.performer_endpoint import PerformerStatus
from coordinare.services.http_performer_service import HTTPPerformerService

IMAGE = "performer@sha256:" + "a" * 64
PLATFORM = platform.system().lower() + "/" + platform.machine().lower()


def service(mode="ephemeral", image=IMAGE):
    instance = object.__new__(HTTPPerformerService)
    instance._config = SimpleNamespace(id="test", mode=mode, image=image)
    return instance


def payload(role="env_bootstrap", image=None):
    return SimpleNamespace(role=role, env_cache_path="/devenv/sym", env_provenance={} if role != "env_bootstrap" else None,
                           metadata={"environment_verification_image": image, "environment_verification_platform": PLATFORM} if image else {})


def status(version):
    return PerformerStatus.model_validate({"availability": "idle", "capabilities": {"backends": []},
        "auth_enabled": False, "environment_manifest_version": version, "env_provenance_version": 2,
        "environment_platform": PLATFORM})


@pytest.mark.asyncio
async def test_bootstrap_rejects_image_that_silently_ignores_reviewed_manifest():
    with pytest.raises(ValueError, match="reviewed environment manifest"):
        await service()._require_provenance_capability(payload(), status(0))


@pytest.mark.asyncio
async def test_bootstrap_requires_ephemeral_container_even_with_capability():
    with pytest.raises(ValueError, match="per-job container"):
        await service("persistent")._require_provenance_capability(payload(), status(1))


@pytest.mark.asyncio
async def test_capable_ephemeral_bootstrap_is_admitted():
    await service()._require_provenance_capability(payload(image=IMAGE), status(1))


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", ["performer:latest", "performer@sha256:" + "b" * 64])
async def test_consumer_cannot_change_clean_verified_image(configured):
    with pytest.raises(ValueError, match=r"verified image|immutable"):
        await service(image=configured)._require_provenance_capability(payload("implementer", IMAGE), status(1))


@pytest.mark.asyncio
async def test_consumer_can_use_exact_immutable_verified_image():
    await service()._require_provenance_capability(payload("implementer", IMAGE), status(1))


def test_legacy_status_has_no_reviewed_manifest_capability():
    old = PerformerStatus.model_validate({"availability": "idle", "capabilities": {"backends": []}, "auth_enabled": False})
    assert old.environment_manifest_version == 0


@pytest.mark.asyncio
async def test_dispatch_refuses_old_bootstrap_image_before_post_and_reaps_container(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    from coordinare.models.performer_endpoint import JobInitPayload, PerformerEndpointConfig
    from coordinare.services import performer_lifecycle
    from coordinare.services.performer_runtime import StartedPerformer
    from coordinare.transport.http_transport import PerformerHTTPClient

    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(500)
    client = PerformerHTTPClient("http://127.0.0.1:8080", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    runtime = SimpleNamespace(start_ephemeral=AsyncMock(return_value=StartedPerformer("container", "http://127.0.0.1:8080")),
                              stop=AsyncMock())
    config = PerformerEndpointConfig(id="p", mode="ephemeral", roles=["env_bootstrap"], image=IMAGE)
    instance = HTTPPerformerService(config, runtime=runtime, client=client)
    wire = JobInitPayload(job_id="j", card_id="bootstrap", role="env_bootstrap", backend="codex", persona="install",
                          repo_url="https://github.com/a/b", branch="main", env_cache_path="/devenv/sym")
    monkeypatch.setattr(instance, "_build_job_payload", lambda *args: wire)
    monkeypatch.setattr(performer_lifecycle, "wait_ready", AsyncMock(return_value=status(0)))
    try:
        response = await instance.dispatch_card({"id": "bootstrap", "role": "env_bootstrap"})
        assert response["status"] == "error"
        assert "reviewed environment manifest" in response["reason"]
        assert requests == []
        runtime.stop.assert_awaited_once_with("container")
    finally:
        await instance.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", [None, "other-os/other-cpu"])
async def test_consumer_requires_same_explicit_runtime_platform(actual):
    ready = status(1).model_dump()
    if actual is None:
        ready.pop("environment_platform", None)
    else:
        ready["environment_platform"] = actual
    with pytest.raises(ValueError, match="platform"):
        await service()._require_provenance_capability(payload("implementer", IMAGE), PerformerStatus.model_validate(ready))


@pytest.mark.asyncio
async def test_consumer_matching_runtime_platform_is_admitted():
    await service()._require_provenance_capability(payload("implementer", IMAGE), status(1))


@pytest.mark.asyncio
async def test_consumer_requires_verified_platform_metadata():
    wire = payload("implementer", IMAGE)
    wire.metadata.pop("environment_verification_platform")
    with pytest.raises(ValueError, match="platform"):
        await service()._require_provenance_capability(wire, status(1))


@pytest.mark.asyncio
async def test_consumer_requires_clean_verified_image_identity():
    with pytest.raises(ValueError, match="verified image"):
        await service()._require_provenance_capability(payload("implementer"), status(1))


@pytest.mark.asyncio
async def test_platform_mismatch_has_structured_rebuild_reason():
    actual = status(1)
    actual.environment_platform = 'different/platform'
    with pytest.raises(ValueError) as error:
        await service()._require_provenance_capability(payload('implementer', IMAGE), actual)
    assert getattr(error.value, 'reason_code', None) == 'environment_identity_mismatch'


@pytest.mark.asyncio
async def test_dispatch_platform_drift_refuses_before_post_and_requests_rebuild(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    from coordinare.models.performer_endpoint import JobInitPayload, PerformerEndpointConfig
    from coordinare.services import performer_lifecycle
    from coordinare.services.performer_runtime import StartedPerformer
    from coordinare.transport.http_transport import PerformerHTTPClient

    requests = []
    client = PerformerHTTPClient('http://127.0.0.1:8080', client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(500))))
    runtime = SimpleNamespace(start_ephemeral=AsyncMock(return_value=StartedPerformer('pod', 'http://127.0.0.1:8080')),
                              stop=AsyncMock())
    instance = HTTPPerformerService(PerformerEndpointConfig(id='p', mode='ephemeral', roles=['implementer'], image=IMAGE),
                                    runtime=runtime, client=client)
    wire = JobInitPayload(job_id='j', card_id='card', role='implementer', backend='codex', persona='work',
        repo_url='https://github.com/a/b', branch='main', env_cache_path='/devenv/sym', env_provenance={},
        metadata={'environment_verification_image': IMAGE, 'environment_verification_platform': PLATFORM})
    actual = status(1)
    actual.environment_platform = 'different/platform'
    monkeypatch.setattr(instance, '_build_job_payload', lambda *args: wire)
    monkeypatch.setattr(performer_lifecycle, 'wait_ready', AsyncMock(return_value=actual))
    try:
        response = await instance.dispatch_card({'id': 'card', 'role': 'implementer'})
        assert response['status'] == 'error'
        assert response['reason_code'] == 'environment_identity_mismatch'
        assert requests == []
        runtime.stop.assert_awaited_once_with('pod')
    finally:
        await instance.aclose()
