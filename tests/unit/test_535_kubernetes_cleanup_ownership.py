from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.services.kubernetes_runtime import KubernetesRuntime

OWNER_LABEL = "coordinare.vividynamics.com/owner"


class Cluster:
    def __init__(self):
        self.selectors = []
        self.pods = [
            SimpleNamespace(metadata=SimpleNamespace(name=name, labels=labels))
            for name, labels in [
                ("own", {OWNER_LABEL: "first", "coordinare.session_id": "orphan"}),
                ("live", {OWNER_LABEL: "first", "coordinare.session_id": "active"}),
                ("foreign", {OWNER_LABEL: "second", "coordinare.session_id": "other"}),
                ("legacy", {"coordinare.session_id": "old"}),
            ]
        ]

    def list_namespaced_pod(self, **kwargs):
        self.selectors.append(kwargs["label_selector"])
        return SimpleNamespace(items=self.pods)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_sweep", [False, True])
async def test_sweeps_preserve_foreign_and_legacy_pods(session_sweep):
    cluster = Cluster()
    runtime = KubernetesRuntime(core_v1=cluster, owner="first")
    runtime.stop = AsyncMock()
    if session_sweep:
        assert await runtime.sweep_orphaned_sessions({"active"}) == ["own"]
        expected = ["own"]
    else:
        assert await runtime.cleanup_orphaned() == 2
        expected = ["own", "live"]
    assert [call.args[0] for call in runtime.stop.call_args_list] == expected
    assert f"{OWNER_LABEL}=first" in cluster.selectors[0]


@pytest.mark.asyncio
async def test_ownerless_runtime_never_sweeps_namespace():
    cluster = Cluster()
    runtime = KubernetesRuntime(core_v1=cluster)
    runtime.stop = AsyncMock()
    assert await runtime.cleanup_orphaned() == 0
    assert await runtime.sweep_orphaned_sessions({"active"}) == []
    assert cluster.selectors == []
    runtime.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_pods_carry_authoritative_owner_label():
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

    class CreatingCluster(Cluster):
        def create_namespaced_pod(self, **kwargs):
            self.manifest = kwargs["body"]

    cluster = CreatingCluster()
    runtime = KubernetesRuntime(core_v1=cluster, owner="first")
    runtime._await_pod_ip = AsyncMock(return_value="10.0.0.2")
    await runtime.start_ephemeral(
        PerformerEndpointConfig(
            id="performer", image="performer:test", mode="ephemeral", roles=["implementing"],
        ),
        extra_labels={OWNER_LABEL: "spoofed"},
    )
    assert cluster.manifest["metadata"]["labels"][OWNER_LABEL] == "first"


def test_runtime_factory_wires_deployment_owner(monkeypatch):
    from coordinare.__main__ import _build_performer_runtime
    from coordinare.config import ProjectConfiguration
    from coordinare.services import kubernetes_runtime

    captured = {}
    monkeypatch.setattr(
        kubernetes_runtime, "KubernetesRuntime", lambda **kwargs: captured.update(kwargs),
    )
    config = ProjectConfiguration(
        github_token="test-token",
        github_org="example",
        human_reviewers=["reviewer"],
        agent_transport="kubernetes",
        kubernetes_owner="deployment-one",
    )
    _build_performer_runtime(config)
    assert captured["owner"] == "deployment-one"


def test_unknown_runtime_options_are_rejected():
    with pytest.raises(TypeError):
        KubernetesRuntime(core_v1=Cluster(), misspelled_owner="first")


@pytest.mark.asyncio
async def test_shared_performer_ids_get_distinct_owned_pod_names():
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

    class CreatingCluster(Cluster):
        def create_namespaced_pod(self, **kwargs):
            self.manifest = kwargs["body"]

    cluster = CreatingCluster()
    config = PerformerEndpointConfig(
        id="performer", image="performer:test", mode="ephemeral", roles=["implementing"],
    )
    handles = []
    for owner in ("first", "second", "first"):
        runtime = KubernetesRuntime(core_v1=cluster, owner=owner)
        runtime._await_pod_ip = AsyncMock(return_value="10.0.0.2")
        handles.append((await runtime.start_ephemeral(config)).handle)
    assert handles[0] != handles[1]
    assert handles[0] == handles[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("labels", [{OWNER_LABEL: "second"}, {}, None])
@pytest.mark.parametrize("owner", ["first", None])
async def test_conflicting_pods_require_proven_ownership_before_deletion(labels, owner):
    from kubernetes.client.rest import ApiException

    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.performer_lifecycle import ContainerStartError

    class ConflictingCluster(Cluster):
        def create_namespaced_pod(self, **kwargs):
            raise ApiException(status=409)

        def read_namespaced_pod(self, **kwargs):
            if labels is None:
                raise RuntimeError("API unavailable")
            return SimpleNamespace(
                metadata=SimpleNamespace(labels=labels), status=SimpleNamespace(phase="Running"),
            )

    runtime = KubernetesRuntime(core_v1=ConflictingCluster(), owner=owner)
    runtime.stop = AsyncMock()
    with pytest.raises(ContainerStartError):
        await runtime.start_ephemeral(
            PerformerEndpointConfig(
                id="performer",
                image="performer:test",
                mode="ephemeral",
                roles=["implementing"],
            ),
        )
    runtime.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_predecessor_can_still_be_recreated():
    from kubernetes.client.rest import ApiException

    from coordinare.models.performer_endpoint import PerformerEndpointConfig

    class OwnedCluster:
        pod = None
        deletes = 0

        def create_namespaced_pod(self, **kwargs):
            if self.pod is not None:
                raise ApiException(status=409)
            self.pod = kwargs["body"]

        def read_namespaced_pod(self, **kwargs):
            if self.pod is None:
                raise ApiException(status=404)
            return SimpleNamespace(
                metadata=SimpleNamespace(labels=self.pod["metadata"]["labels"]),
                status=SimpleNamespace(phase="Running"),
            )

        def delete_namespaced_pod(self, **kwargs):
            self.pod = None
            self.deletes += 1

    cluster = OwnedCluster()
    runtime = KubernetesRuntime(core_v1=cluster, owner="first")
    runtime._await_pod_ip = AsyncMock(return_value="10.0.0.2")
    config = PerformerEndpointConfig(
        id="performer", image="performer:test", mode="ephemeral", roles=["implementing"],
    )
    first = await runtime.start_ephemeral(config)
    second = await runtime.start_ephemeral(config)
    assert first.handle == second.handle
    assert cluster.deletes == 1
