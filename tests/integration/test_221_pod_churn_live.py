"""Issue #221 — performer Pod churn against a real cluster.

The deterministic counterpart is ``tests/unit/test_221_pod_churn.py``, which
forces the interleavings on demand with a fake API. This file checks that a real
cluster agrees — that kubelet's actual termination timing, the API server's
admission, and the grace period behave the way the fake models them.

A lightweight image is used rather than the 4 GB performer: what is under test is
Pod lifecycle under contention, not anything the performer serves. Using the real
image would make this slow without making it stronger.

**Skipped, not failed, when no cluster is reachable**, with the skip naming the
command that creates one.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from types import SimpleNamespace

import pytest

NAMESPACE = "default"
LIGHT_IMAGE = "registry.k8s.io/pause:3.10"
LABEL = "coordinare.vividynamics.com/performer-id"

SKIP_REASON = (
    "no reachable Kubernetes cluster. Create one:\n  kind create cluster --name coordinare-dev"
)


def _cluster_available() -> bool:
    if shutil.which("kubectl") is None:
        return False
    return (
        subprocess.run(
            ["kubectl", "get", "nodes"], capture_output=True, text=True, timeout=15,
        ).returncode
        == 0
    )


pytestmark = [
    pytest.mark.skipif(not _cluster_available(), reason=SKIP_REASON),
    pytest.mark.timeout(600),
]


def _config(performer_id: str, **overrides):
    base = {
        "id": performer_id,
        "image": LIGHT_IMAGE,
        "env": {},
        "readiness_timeout_s": 60,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def runtime():
    from coordinare.services.kubernetes_runtime import KubernetesRuntime

    return KubernetesRuntime(namespace=NAMESPACE, owner=f"churn-e2e-{uuid.uuid4().hex[:8]}")


def _pods_for(prefix: str) -> list[str]:
    result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "pods", "-o", "name"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return [line for line in result.stdout.split() if prefix in line]


def test_rapid_restart_of_one_performer_never_collides(runtime) -> None:
    """The failure Docker structurally cannot have.

    ``pod_name_for`` is deterministic, so a stop that returns while its Pod is
    still terminating makes the *next* start fail to create at all. On a real
    cluster the grace period is real time, which is the part a fake cannot prove.
    """
    performer_id = "churn-restart"

    async def churn():
        for _ in range(4):
            started = await runtime.start_ephemeral(_config(performer_id))
            assert started.handle
            await runtime.stop(started.handle, performer_id=performer_id)

    try:
        asyncio.run(churn())
    finally:
        asyncio.run(runtime.cleanup_orphaned(performer_id))

    assert not _pods_for(performer_id), f"Pods survived the churn: {_pods_for(performer_id)}"


def test_many_distinct_performers_run_and_stop_concurrently(runtime) -> None:
    """``PerformerRuntime`` promises this is safe; nothing had checked it."""
    ids = [f"churn-conc-{i}" for i in range(5)]

    async def scenario():
        started = await asyncio.gather(*(runtime.start_ephemeral(_config(i)) for i in ids))
        assert len({s.handle for s in started}) == len(ids)
        assert all(s.endpoint.startswith("http://") for s in started)
        await asyncio.gather(
            *(runtime.stop(s.handle, performer_id=i) for s, i in zip(started, ids, strict=True)),
        )

    try:
        asyncio.run(scenario())
    finally:
        for performer_id in ids:
            asyncio.run(runtime.cleanup_orphaned(performer_id))

    leaked = [p for i in ids for p in _pods_for(i)]
    assert not leaked, f"concurrent performers leaked Pods: {leaked}"


def test_a_restart_while_the_predecessor_is_terminating(runtime) -> None:
    """The exact window the collision lives in, forced rather than hoped for.

    Deletion is requested directly, bypassing ``stop``, so the next start begins
    while the predecessor is genuinely still terminating. If ``start_ephemeral``
    did not tolerate that, this is where it would fail with "already exists".
    """
    performer_id = "churn-terminating"

    async def scenario():
        first = await runtime.start_ephemeral(_config(performer_id))

        # Request deletion and do NOT wait, so the next start races the grace period.
        subprocess.run(
            ["kubectl", "-n", NAMESPACE, "delete", "pod", first.handle, "--wait=false"],
            capture_output=True,
            timeout=30,
        )

        second = await runtime.start_ephemeral(_config(performer_id))
        await runtime.stop(second.handle, performer_id=performer_id)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(runtime.cleanup_orphaned(performer_id))

    assert not _pods_for(performer_id)


def test_a_performer_recovers_from_a_failed_start(runtime) -> None:
    """An unschedulable image must be transient, not permanently disabling.

    If the failed start leaks its Pod, the retry collides with the corpse and the
    performer can never start again — the second failure looking unrelated to the
    first.
    """
    from coordinare.services import performer_lifecycle

    performer_id = "churn-recover"

    async def scenario():
        with pytest.raises(performer_lifecycle.LifecycleError):
            await runtime.start_ephemeral(
                _config(performer_id, image="example.invalid/nope:0", readiness_timeout_s=25),
            )
        # The image problem is resolved; the same performer must start.
        started = await runtime.start_ephemeral(_config(performer_id))
        await runtime.stop(started.handle, performer_id=performer_id)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(runtime.cleanup_orphaned(performer_id))

    assert not _pods_for(performer_id)
