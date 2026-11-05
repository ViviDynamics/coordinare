"""Clean admission cannot be waived by live-job connectivity or an unknown purpose."""
from __future__ import annotations

import copy
import json
import time

import pytest
from coordinare_service_inference.environment import runtime_supervisor as supervisor

from coordinare.services.environment_acceptance import execution_report
from tests.unit.runtime_manifest_fixture import runtime_cache, runtime_report
from tests.unit.test_353_runtime_supervisor import runtime_spec


def test_live_report_cannot_be_published_as_clean_readiness(tmp_path):
    cache = runtime_cache(tmp_path)
    raw = runtime_report(cache)
    cache.environment_runtime_artifacts = copy.deepcopy(raw['runtime_artifacts'])
    raw['runtime_phase'] = 'live_consumer'
    with pytest.raises(ValueError, match='runtime'):
        execution_report(json.dumps(raw), cache)
    assert cache.environment_verification is None


@pytest.mark.asyncio
@pytest.mark.parametrize('purpose', ['live_consumer', 'unknown'])
async def test_missing_receipt_or_unknown_purpose_refuses_before_admission(tmp_path, monkeypatch, purpose):
    def forbidden(*args, **kwargs):
        pytest.fail('guest admission started without a bound consumer receipt')
    monkeypatch.setattr(supervisor, '_admit', forbidden)
    with pytest.raises(ValueError, match=r'purpose|receipt'):
        await supervisor.open_runtime(runtime_spec(), artifacts=None, repo=tmp_path/'repo', cache=tmp_path/'cache',
            scratch=tmp_path/'scratch', install=False, deadline=time.monotonic()+5,
            manifest_digest='1'*64, source_digest='2'*64, purpose=purpose)


@pytest.mark.asyncio
@pytest.mark.parametrize(('purpose', 'isolated'), [(None, True), ('consumer', True), ('live_consumer', False)])
async def test_only_explicit_live_job_uses_carrier_control_connectivity(tmp_path, monkeypatch, purpose, isolated):
    from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt

    spec = runtime_spec()
    receipt = RuntimeArtifactReceipt(schema_version=1, manifest_digest='1'*64, source_digest='2'*64,
        runtime_digest=spec.digest(), sealed=True,
        artifacts=[{'path': 'installed.qcow2', 'sha256': '3'*64, 'bytes': 1}])
    observed = {}
    def admission(*args, **kwargs):
        observed.update(kwargs)
        raise RuntimeError('admission boundary')
    monkeypatch.setattr(supervisor, '_admit', admission)
    with pytest.raises(RuntimeError, match='admission boundary'):
        await supervisor.open_runtime(spec, artifacts=receipt, repo=tmp_path/'repo', cache=tmp_path/'cache',
            scratch=tmp_path/'scratch', install=False, deadline=time.monotonic()+5,
            manifest_digest='1'*64, source_digest='2'*64, purpose=purpose)
    assert observed['readonly'] is True
    assert observed['isolated'] is isolated
