from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kubernetes.client.rest import ApiException

from coordinare.services.kubernetes_runtime import KubernetesRuntime
from coordinare.services.kubernetes_verifier import VerifierSpec, build_verifier_pod_manifest


def spec():
    return VerifierSpec(image='image:version', cache_claim='claim', cache_subpath='project',
                        mount_path='/devenv/project', verify_text='true', project_files={},
                        transient_credentials=base64.b64encode(json.dumps({'DSN': 'private-value'}).encode()).decode())


def test_pod_uses_secret_reference_and_spec_repr_hides_credentials():
    value = spec()
    pod = build_verifier_pod_manifest(value, name='verify-fixture', namespace='ns', owner='owner')
    env = next(item for item in pod['spec']['containers'][0]['env']
               if item['name'] == 'COORDINARE_ENV_SECRET_VALUES')
    assert env == {'name': 'COORDINARE_ENV_SECRET_VALUES', 'valueFrom': {
        'secretKeyRef': {'name': 'verify-fixture-credentials', 'key': 'values'}}}
    assert value.transient_credentials not in json.dumps(pod)
    assert 'private-value' not in json.dumps(pod)
    assert value.transient_credentials not in repr(value)


class Core:
    def __init__(self):
        self.calls = []
        self.secret = None
        self.secret_delete_failure = False

    def create_namespaced_secret(self, *, namespace, body):
        self.calls.append(('create-secret', body))
        self.secret = body
        return SimpleNamespace(metadata=SimpleNamespace(uid='uid'))

    def create_namespaced_pod(self, **kwargs):
        self.calls.append(('create-pod', kwargs['body']))
        return SimpleNamespace(metadata=SimpleNamespace(uid='pod-uid'))

    def delete_namespaced_secret(self, **kwargs):
        self.calls.append(('delete-secret', kwargs))
        if self.secret_delete_failure:
            raise ApiException(status=403)
        self.secret = None

    def read_namespaced_secret(self, **kwargs):
        self.calls.append(('read-secret', kwargs))
        if self.secret is None:
            raise ApiException(status=404)
        return SimpleNamespace(metadata=SimpleNamespace(uid='uid'))


@pytest.mark.asyncio
async def test_secret_owned_by_pod_and_deleted_after_confirmed_cleanup():
    core = Core()
    runtime = KubernetesRuntime(namespace='ns', cache_claim='claim', core_v1=core)
    runtime._await_verifier = AsyncMock(return_value=(True, 'report'))

    async def remove(name):
        core.calls.append(('pod-confirmed-gone', name))
        return True

    runtime._remove_verifier = remove
    assert await runtime.run_verifier(slug='fixture', spec=spec()) == (True, 'report')
    names = [call[0] for call in core.calls]
    assert names.index('create-pod') < names.index('create-secret')
    assert names.index('pod-confirmed-gone') < names.index('delete-secret')
    secret = next(body for name, body in core.calls if name == 'create-secret')
    assert secret['metadata']['ownerReferences'] == [{'apiVersion': 'v1', 'kind': 'Pod',
        'name': core.calls[0][1]['metadata']['name'], 'uid': 'pod-uid',
        'controller': False, 'blockOwnerDeletion': False}]
    assert secret['metadata']['labels']['coordinare.role'] == 'env-verifier-credentials'
    assert base64.b64decode(secret['data']['values']).decode() == spec().transient_credentials


@pytest.mark.asyncio
@pytest.mark.parametrize('pod_removed', [False, True])
async def test_cleanup_uncertainty_holds_and_retains_credentials_for_live_pod(pod_removed):
    core = Core()
    core.secret_delete_failure = True
    runtime = KubernetesRuntime(namespace='ns', cache_claim='claim', core_v1=core)
    runtime._await_verifier = AsyncMock(return_value=(True, 'report'))
    runtime._remove_verifier = AsyncMock(return_value=pod_removed)
    verdict, _ = await runtime.run_verifier(slug='fixture', spec=spec())
    assert verdict is None
    assert any(call[0] == 'delete-secret' for call in core.calls) is pod_removed


@pytest.mark.asyncio
async def test_cancellation_still_removes_pod_then_secret():
    core = Core()
    runtime = KubernetesRuntime(namespace='ns', cache_claim='claim', core_v1=core)
    entered = asyncio.Event()

    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    runtime._await_verifier = wait
    runtime._remove_verifier = AsyncMock(return_value=True)
    task = asyncio.create_task(runtime.run_verifier(slug='fixture', spec=spec()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime._remove_verifier.assert_awaited_once()
    assert core.secret is None


@pytest.mark.asyncio
async def test_cancellation_waits_for_secret_creation_before_cleanup():
    import threading

    core = Core()
    entered = threading.Event()
    release = threading.Event()
    original = core.create_namespaced_secret

    def delayed(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    core.create_namespaced_secret = delayed
    runtime = KubernetesRuntime(namespace='ns', cache_claim='claim', core_v1=core)
    runtime._remove_verifier = AsyncMock(return_value=True)
    task = asyncio.create_task(runtime.run_verifier(slug='fixture', spec=spec()))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    assert [call[0] for call in core.calls] == ['create-pod']
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert core.secret is None
    assert [call[0] for call in core.calls] == ['create-pod', 'create-secret', 'delete-secret', 'read-secret']


@pytest.mark.asyncio
async def test_existing_secret_conflict_never_deletes_someone_elses_secret():
    core = Core()

    def conflict(**kwargs):
        raise ApiException(status=409)

    core.create_namespaced_secret = conflict
    runtime = KubernetesRuntime(namespace='ns', cache_claim='claim', core_v1=core)
    runtime._remove_verifier = AsyncMock(return_value=True)
    verdict, _ = await runtime.run_verifier(slug='fixture', spec=spec())
    assert verdict is None
    assert [call[0] for call in core.calls] == ['create-pod']


@pytest.mark.asyncio
async def test_missing_pod_uid_refuses_secret_and_removes_pod():
    core = Core()
    original = core.create_namespaced_pod

    def missing_uid(**kwargs):
        original(**kwargs)
        return SimpleNamespace(metadata=SimpleNamespace(uid=None))

    core.create_namespaced_pod = missing_uid
    runtime = KubernetesRuntime(namespace='ns', cache_claim='claim', core_v1=core)
    runtime._remove_verifier = AsyncMock(return_value=True)
    verdict, detail = await runtime.run_verifier(slug='fixture', spec=spec())
    assert verdict is None
    assert 'UID' in detail
    assert [call[0] for call in core.calls] == ['create-pod']
    runtime._remove_verifier.assert_awaited_once()
