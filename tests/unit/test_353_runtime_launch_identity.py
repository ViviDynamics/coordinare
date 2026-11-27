"""Reviewed jobs use the immutable image supplied to the actual runtime launch."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.docker_runtime import DockerRuntime
from tests.unit.test_351_performer_capability import IMAGE, payload, service, status


@pytest.mark.asyncio
async def test_runtime_reports_pinned_launch_identity_without_mutating_config(monkeypatch):
    resolver = SimpleNamespace(pin=AsyncMock(return_value=IMAGE))
    config = PerformerEndpointConfig(id='p', roles=['implementer'], mode='ephemeral', image='performer:latest')
    launched = []
    async def start(actual, **kwargs):
        launched.append(actual.image)
        return SimpleNamespace(container_id='owned', endpoint='http://127.0.0.1:8080')
    monkeypatch.setattr('coordinare.services.performer_lifecycle.start_ephemeral', start)
    started = await DockerRuntime(image_resolver=resolver).start_ephemeral(config)
    assert started.image_ref == IMAGE
    assert launched == [IMAGE]
    assert config.image == 'performer:latest'


@pytest.mark.asyncio
@pytest.mark.parametrize('launched', [IMAGE, 'performer:latest', 'performer@sha256:'+'b'*64])
async def test_gate_uses_actual_immutable_launch_identity(launched):
    checked = service(image='performer:latest')
    if launched == IMAGE:
        await checked._require_provenance_capability(payload('implementer', IMAGE), status(1), launched_image=launched)
    else:
        with pytest.raises(ValueError, match=r'immutable|differs'):
            await checked._require_provenance_capability(payload('implementer', IMAGE), status(1), launched_image=launched)


@pytest.mark.asyncio
async def test_http_dispatch_accepts_the_pinned_launch_of_a_configured_tag(monkeypatch):
    from coordinare.models.performer_endpoint import JobInitPayload
    from coordinare.services import performer_lifecycle
    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.services.performer_runtime import StartedPerformer
    from coordinare.transport.http_transport import PerformerHTTPClient

    runtime = SimpleNamespace(start_ephemeral=AsyncMock(return_value=StartedPerformer('owned', 'http://127.0.0.1:8080', IMAGE)),
        stop=AsyncMock())
    config = PerformerEndpointConfig(id='p', roles=['env_bootstrap'], mode='ephemeral', image='performer:latest')
    instance = HTTPPerformerService(config, runtime=runtime)
    wire = JobInitPayload(job_id='j', card_id='bootstrap', role='env_bootstrap', backend='codex', persona='install',
        repo_url='https://github.com/a/b', branch='main', env_cache_path='/devenv/sym', metadata=payload(image=IMAGE).metadata)
    monkeypatch.setattr(instance, '_build_job_payload', lambda *args: wire)
    monkeypatch.setattr(performer_lifecycle, 'wait_ready', AsyncMock(return_value=status(1)))
    post = AsyncMock(return_value=SimpleNamespace(accepted=False, reason='fixture stops at HTTP boundary'))
    monkeypatch.setattr(PerformerHTTPClient, 'post_job', post)
    try:
        response = await instance.dispatch_card({'id': 'bootstrap', 'role': 'env_bootstrap'})
        assert 'fixture stops at HTTP boundary' in response['reason']
        post.assert_awaited_once()
        runtime.stop.assert_awaited_once_with('owned')
    finally:
        await instance.aclose()
