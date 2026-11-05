"""Source Secrets are attempt-owned and cannot outlive confirmed verifier cleanup."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from coordinare.services.kubernetes_runtime import KubernetesRuntime
from tests.unit.test_353_source_transport import source_spec


class SourceCore:
    def __init__(self, *, fail_at=None, removal_fails=False):
        self.secrets = {}
        self.fail_at = fail_at
        self.removal_fails = removal_fails
        self.deleted = []

    def create_namespaced_pod(self, **kwargs):
        return SimpleNamespace(metadata=SimpleNamespace(uid="pod-uid"))

    def create_namespaced_secret(self, *, namespace, body):
        name = body["metadata"]["name"]
        if self.fail_at is not None and len(self.secrets) == self.fail_at:
            raise ApiException(status=403)
        self.secrets[name] = body
        return SimpleNamespace(metadata=SimpleNamespace(uid="secret-uid"))

    def delete_namespaced_secret(self, *, name, namespace):
        if self.removal_fails:
            raise ApiException(status=403)
        self.deleted.append(name)
        self.secrets.pop(name, None)

    def read_namespaced_secret(self, *, name, namespace):
        if name not in self.secrets:
            raise ApiException(status=404)
        return self.secrets[name]


@pytest.mark.asyncio
async def test_partial_create_cancel_restart_cleanup():
    core = SourceCore()
    runtime = KubernetesRuntime(namespace="ns", cache_claim="claim", core_v1=core, owner="owner")

    async def finish(*args, **kwargs):
        assert core.secrets
        return True, "verified"

    async def remove(name):
        return True

    runtime._await_verifier = finish
    runtime._remove_verifier = remove
    assert await runtime.run_verifier(slug="sym", spec=source_spec()) == (True, "verified")
    assert not core.secrets
    assert core.deleted


@pytest.mark.asyncio
async def test_failed_cleanup_never_reports_success():
    core = SourceCore(removal_fails=True)
    runtime = KubernetesRuntime(namespace="ns", cache_claim="claim", core_v1=core, owner="owner")

    async def finish(*args, **kwargs):
        return True, "verified"

    async def remove(name):
        return True

    runtime._await_verifier = finish
    runtime._remove_verifier = remove
    passed, reason = await runtime.run_verifier(slug="sym", spec=source_spec())
    assert passed is None
    assert "source" in reason.lower()
    assert core.secrets


@pytest.mark.asyncio
async def test_partial_source_creation_removes_prior_chunks():
    import hashlib
    from dataclasses import replace

    files = {f"file-{i}": str(i) * 200_000 for i in range(3)}
    spec = replace(source_spec(), project_files=files,
        source_inputs={path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()})
    core = SourceCore(fail_at=1)
    runtime = KubernetesRuntime(namespace="ns", cache_claim="claim", core_v1=core, owner="owner")

    async def remove(name):
        return True

    runtime._remove_verifier = remove
    passed, reason = await runtime.run_verifier(slug="sym", spec=spec)
    assert passed is None
    assert "source Secret could not be created" in reason
    assert not core.secrets
    assert len(core.deleted) == 2


@pytest.mark.asyncio
async def test_cancel_waits_for_late_source_creation_then_cleans():
    import asyncio
    import threading

    core = SourceCore()
    entered, release = threading.Event(), threading.Event()
    original = core.create_namespaced_secret

    def delayed(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    core.create_namespaced_secret = delayed
    runtime = KubernetesRuntime(namespace="ns", cache_claim="claim", core_v1=core, owner="owner")

    async def remove(name):
        return True

    runtime._remove_verifier = remove
    task = asyncio.create_task(runtime.run_verifier(slug="sym", spec=source_spec()))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not core.secrets
    assert core.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_restart_reconciles_only_owned_source_secrets(foreign):
    from coordinare.services.kubernetes_verifier import build_verifier_source_secrets

    core = SourceCore()
    spec = source_spec()
    secret = build_verifier_source_secrets(spec, name="restored", namespace="ns", owner="owner", pod_uid="old-uid")[0]
    if foreign:
        secret["metadata"]["ownerReferences"][0]["uid"] = "different-pod"
    core.secrets[secret["metadata"]["name"]] = secret
    metadata = SimpleNamespace(name="restored", uid="old-uid", annotations={"coordinare.vividynamics.com/source-secrets": "1"},
        labels={"app.kubernetes.io/managed-by": "coordinare", "coordinare.role": "env-verifier",
                "coordinare.vividynamics.com/owner": "owner"})
    core.list_namespaced_pod = lambda **kwargs: SimpleNamespace(items=[SimpleNamespace(metadata=metadata)])
    runtime = KubernetesRuntime(namespace="ns", cache_claim="claim", core_v1=core, owner="owner")

    async def remove(name):
        assert not foreign
        return True

    runtime._remove_verifier = remove
    swept = await runtime.sweep_orphaned_verifiers()
    assert swept == ([] if foreign else ["restored"])
    assert bool(core.secrets) is foreign


@pytest.mark.asyncio
async def test_restart_cleans_large_admitted_execution_source(tmp_path):
    import hashlib
    import os
    from dataclasses import replace

    from coordinare.services.kubernetes_verifier import (
        build_verifier_pod_manifest,
        build_verifier_source_secrets,
    )
    from tests.unit.test_353_execution_source_pipeline import bundle_fixture

    source = b'build asset.bin\n'
    bundle = bundle_fixture(tmp_path, {'Buildfile': (source, '100644'),
                                      'asset.bin': (os.urandom(32 * 1024 * 1024), '100644')})
    spec = replace(source_spec(), project_files={}, execution_source=bundle,
                   source_inputs={'Buildfile': hashlib.sha256(source).hexdigest()})
    pod = build_verifier_pod_manifest(spec, name='restored', namespace='ns', owner='owner')
    secrets = build_verifier_source_secrets(spec, name='restored', namespace='ns', owner='owner', pod_uid='old-uid')
    assert len(secrets) > 64
    assert int(pod['metadata']['annotations']['coordinare.vividynamics.com/source-secrets']) == len(secrets)
    core = SourceCore()
    core.secrets = {secret['metadata']['name']: secret for secret in secrets}
    metadata = SimpleNamespace(**pod['metadata'], uid='old-uid')
    core.list_namespaced_pod = lambda **kwargs: SimpleNamespace(items=[SimpleNamespace(metadata=metadata)])
    runtime = KubernetesRuntime(namespace='ns', cache_claim='claim', core_v1=core, owner='owner')
    removed_pods = []

    async def remove(name):
        removed_pods.append(name)
        return True

    runtime._remove_verifier = remove
    assert await runtime.sweep_orphaned_verifiers() == ['restored']
    assert removed_pods == ['restored']
    assert not core.secrets and len(core.deleted) == len(secrets)


@pytest.mark.asyncio
@pytest.mark.parametrize(('count', 'foreign'), [(257, False), (257, True), (258, False)])
async def test_restart_large_source_bound_keeps_complete_ownership_checks(count, foreign):
    import copy

    from coordinare.services.kubernetes_verifier import (
        build_verifier_source_secrets,
        source_secret_name,
    )

    core = SourceCore()
    template = build_verifier_source_secrets(source_spec(), name='restored', namespace='ns',
                                             owner='owner', pod_uid='old-uid')[0]['metadata']
    for index in range(count):
        name = source_secret_name('restored', index)
        metadata = copy.deepcopy(template)
        metadata['name'] = name
        if foreign and index == count - 1:
            metadata['ownerReferences'][0]['uid'] = 'other-pod'
        core.secrets[name] = {'metadata': metadata}
    metadata = SimpleNamespace(name='restored', uid='old-uid',
        annotations={'coordinare.vividynamics.com/source-secrets': str(count)},
        labels={'app.kubernetes.io/managed-by': 'coordinare', 'coordinare.role': 'env-verifier',
                'coordinare.vividynamics.com/owner': 'owner'})
    core.list_namespaced_pod = lambda **kwargs: SimpleNamespace(items=[SimpleNamespace(metadata=metadata)])
    reads, removed_pods = [], []
    original_read = core.read_namespaced_secret

    def read(**kwargs):
        reads.append(kwargs['name'])
        return original_read(**kwargs)

    core.read_namespaced_secret = read
    runtime = KubernetesRuntime(namespace='ns', cache_claim='claim', core_v1=core, owner='owner')
    source_names = set(core.secrets)

    async def remove(name):
        assert set(reads) == source_names
        assert set(core.secrets) == source_names
        removed_pods.append(name)
        return True

    runtime._remove_verifier = remove
    permitted = count == 257 and not foreign
    assert await runtime.sweep_orphaned_verifiers() == (['restored'] if permitted else [])
    assert set(reads) == (source_names if count == 257 else set())
    assert removed_pods == (['restored'] if permitted else [])
    assert len(core.deleted) == (257 if permitted else 0)
    assert bool(core.secrets) is not permitted


@pytest.mark.asyncio
async def test_conflicting_source_secret_is_never_deleted():
    core = SourceCore()

    def conflict(**kwargs):
        raise ApiException(status=409)

    core.create_namespaced_secret = conflict
    runtime = KubernetesRuntime(namespace="ns", cache_claim="claim", core_v1=core, owner="owner")

    async def remove(name):
        return True

    runtime._remove_verifier = remove
    passed, reason = await runtime.run_verifier(slug="sym", spec=source_spec())
    assert passed is None
    assert "source Secret could not be created" in reason
    assert core.deleted == []


@pytest.mark.asyncio
async def test_init_image_pull_failure_is_infrastructure_hold():
    import asyncio

    core = SourceCore()
    waiting = SimpleNamespace(state=SimpleNamespace(waiting=SimpleNamespace(reason="ImagePullBackOff")))
    core.read_namespaced_pod = lambda **kwargs: SimpleNamespace(status=SimpleNamespace(
        phase="Pending", container_statuses=[], init_container_statuses=[waiting]))
    runtime = KubernetesRuntime(namespace="ns", cache_claim="claim", core_v1=core, owner="owner")
    passed, reason = await asyncio.wait_for(runtime._await_verifier("attempt", walk_only=False, poll_interval_s=0.001), 0.2)
    assert passed is None
    assert "ImagePullBackOff" in reason
