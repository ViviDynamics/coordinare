"""Spec 146 / issue #200 — the Kubernetes runtime against a real cluster.

**Skipped, not failed, when no cluster is reachable.** A developer without one
should get a green suite and a message telling them what to run.

The skip must stay *visible*, which is why the reason names the exact command.
A silently-skipped integration test is how "it works on Kubernetes" remains green
while quietly becoming untrue — the same failure mode as a grep that matches
nothing and is read as a clean result.

    kind create cluster --name coordinare-dev
    kind load docker-image coordinare-performer:base --name coordinare-dev
    .venv/bin/pytest tests/integration/test_146_kind_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest

NAMESPACE = "default"
TEST_IMAGE = "coordinare-performer:base"
SKIP_REASON = (
    "no reachable Kubernetes cluster with the performer image loaded. Create one:\n"
    "  kind create cluster --name coordinare-dev\n"
    f"  kind load docker-image {TEST_IMAGE} --name coordinare-dev"
)


def _cluster_available() -> bool:
    """Whether a cluster is reachable. Deliberately not whether the image is loaded.

    An earlier version checked ``node.status.images`` too, so that a missing image
    skipped with the ``kind load`` command rather than failing on a pull. That was
    a mistake: kubelet reports its image list on a cycle, so the check reads
    "absent" for a while after a successful load — in CI it skipped an entire
    suite seconds after loading the image, and the job went green having verified
    nothing. A silent skip is a worse failure mode than a confusing error, and
    ``classify_pod_failure`` already names an image-pull reason clearly.
    """
    try:
        from kubernetes import client, config
    except ImportError:
        return False
    try:
        config.load_kube_config()
        client.CoreV1Api().list_namespaced_pod(namespace=NAMESPACE, limit=1, _request_timeout=5)
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _cluster_available(), reason=SKIP_REASON)


@contextlib.contextmanager
def _port_forward(pod_name: str, remote_port: int):
    """Tunnel to a Pod so a host-side test can reach it.

    Needed because **Pod IPs are not routable from outside the cluster**. In
    production coordinare runs in-cluster, where ``http://<pod-ip>:8088`` is
    directly reachable — the spike proved that from a second Pod. A test running
    on a laptop is in the one position where it is not, so it tunnels rather than
    pretending the address works from here.

    This is the concrete form of the caveat in ``kubernetes_runtime``: running the
    daemon outside the cluster is a development convenience, not a supported mode.
    """
    import json
    import re
    import subprocess
    import time
    import urllib.error
    import urllib.request

    def _spawn() -> subprocess.Popen:
        # Let kubectl choose and bind the local port, then tell us which one. The
        # obvious alternative — bind a socket to find a free port, close it, and
        # hand the number to kubectl — races: between the close and kubectl's
        # bind, anything else on the machine can take that port. The test would
        # then connect to a *foreign* listener and poll it until the readiness
        # deadline, reporting a broken performer when the performer was fine.
        return subprocess.Popen(
            ["kubectl", "port-forward", f"pod/{pod_name}", f":{remote_port}", "-n", NAMESPACE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _establish(proc: subprocess.Popen) -> str | None:
        """The tunnel's local endpoint once it actually serves ``/status``."""
        local_port: int | None = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and proc.poll() is None:
            match = re.search(r"127\.0\.0\.1:(\d+)", proc.stdout.readline() or "")
            if match:
                local_port = int(match.group(1))
                break
        if local_port is None:
            return None

        # A TCP connect cannot distinguish the performer from anything else
        # listening, so verify the tunnel by what comes back through it.
        endpoint = f"http://127.0.0.1:{local_port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                with urllib.request.urlopen(f"{endpoint}/status", timeout=2) as response:
                    json.loads(response.read())
                    return endpoint
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.3)
        return None

    # ``kubectl port-forward`` exits, rather than retrying, if it attaches before
    # the performer binds its port: "failed to connect to localhost:8088 inside
    # namespace ... connection refused" and then "lost connection to pod". A
    # performer that is still starting is normal, so the tunnel is retried until
    # it serves.
    #
    # This resilience is the *test harness* catching up with the product, not
    # covering for it: in-cluster, coordinare connects straight to the Pod IP and
    # ``wait_ready`` already retries a refused connection. Only a host-side
    # caller needs a tunnel, and only a tunnel can die this way.
    proc: subprocess.Popen | None = None
    endpoint: str | None = None
    last_stderr = ""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        proc = _spawn()
        endpoint = _establish(proc)
        if endpoint is not None:
            break
        proc.kill()
        last_stderr = (proc.communicate()[1] or "").strip()
        time.sleep(0.5)

    if endpoint is None:
        if proc is not None:
            proc.kill()
        raise RuntimeError(
            f"port-forward to {pod_name} never served /status. kubectl stderr: {last_stderr!r}",
        )

    try:
        yield endpoint
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


@pytest.fixture
def runtime():
    from coordinare.services.kubernetes_runtime import KubernetesRuntime

    return KubernetesRuntime(namespace=NAMESPACE, owner=f"e2e-owner-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def config():
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

    return PerformerEndpointConfig(
        id=f"e2e-{uuid.uuid4().hex[:8]}",
        mode="ephemeral",
        roles=["implementer"],
        image=TEST_IMAGE,
        readiness_timeout_s=120,
    )


def test_performer_pod_starts_serves_and_stops(runtime, config, tmp_path) -> None:
    """SC-001 — the whole point, end to end, with no Docker socket involved.

    Start a performer as a Pod, confirm it serves its HTTP contract through the
    *shared* readiness wait (proving the Kubernetes path reuses it unchanged),
    capture logs, and confirm the Pod is gone afterwards.
    """
    from kubernetes import client

    from coordinare.services import performer_lifecycle

    async def scenario():
        started = await runtime.start_ephemeral(config)
        try:
            assert started.handle, "a Pod name must come back as the handle"
            assert started.endpoint.startswith("http://"), started.endpoint

            # The shared, runtime-agnostic readiness wait — unchanged for k8s.
            # Tunnelled because this test runs on the host, where Pod IPs are not
            # routable; in-cluster, started.endpoint is used directly.
            with _port_forward(started.handle, 8088) as local_endpoint:
                status = await performer_lifecycle.wait_ready(
                    local_endpoint, auth_token=None, timeout=120.0, performer_id=config.id,
                )
            assert status.availability != "starting"
            return started
        except Exception:
            await runtime.stop(started.handle)
            raise

    started = asyncio.run(scenario())

    # Logs are captured BEFORE deletion; afterwards they are unreachable.
    asyncio.run(runtime.stop(started.handle, host_log_dir=tmp_path, performer_id=config.id))

    log_file = tmp_path / f"{config.id}.log"
    assert log_file.exists(), (
        "stop() must capture Pod logs before deleting the Pod — afterwards there is "
        "nothing left to read (FR-007)"
    )

    core = client.CoreV1Api()
    remaining = core.list_namespaced_pod(
        namespace=NAMESPACE, field_selector=f"metadata.name={started.handle}",
    )
    assert not remaining.items, "the Pod must not outlive the performer (FR-008)"


@pytest.mark.parametrize("bystander_owner", ["unmanaged", "legacy", "other-coordinare"])
def test_orphan_sweep_only_touches_coordinares_own_pods(runtime, config, bystander_owner) -> None:
    """A sweep in a shared namespace must not reap somebody else's workload."""
    from kubernetes import client

    from coordinare.services.kubernetes_runtime import (
        MANAGED_BY_LABEL,
        MANAGED_BY_VALUE,
        OWNER_LABEL,
        PERFORMER_ID_LABEL,
    )

    labels = {} if bystander_owner == "unmanaged" else {
        MANAGED_BY_LABEL: MANAGED_BY_VALUE,
        PERFORMER_ID_LABEL: config.id,
    }
    if bystander_owner not in {"unmanaged", "legacy"}:
        labels[OWNER_LABEL] = bystander_owner
    core = client.CoreV1Api()
    bystander = f"bystander-{uuid.uuid4().hex[:8]}"
    core.create_namespaced_pod(
        namespace=NAMESPACE,
        body={
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": bystander, "labels": labels},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "c",
                        "image": TEST_IMAGE,
                        "imagePullPolicy": "Never",
                        "command": ["sleep", "300"],
                    },
                ],
            },
        },
    )
    try:
        started = asyncio.run(runtime.start_ephemeral(config))
        swept = asyncio.run(runtime.cleanup_orphaned())
        assert swept >= 1

        survivors = core.list_namespaced_pod(
            namespace=NAMESPACE, field_selector=f"metadata.name={bystander}",
        )
        assert survivors.items, (
            "the sweep deleted a Pod coordinare does not own; it must select on its "
            "own labels, or two coordinares sharing a namespace will reap each other"
        )
        remaining = core.list_namespaced_pod(
            namespace=NAMESPACE, field_selector=f"metadata.name={started.handle}",
        )
        assert not remaining.items, "the owned Pod must be swept"
    finally:
        with contextlib.suppress(Exception):
            core.delete_namespaced_pod(name=bystander, namespace=NAMESPACE, grace_period_seconds=0)


def test_a_pod_that_cannot_pull_fails_fast_with_a_useful_reason(runtime) -> None:
    """FR-009 — an unschedulable Pod must not be reported as a timeout.

    Waiting the full readiness timeout and then saying "timed out" sends an
    operator looking at the performer when the answer is in the Pod's status.
    """
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services import performer_lifecycle

    bad = PerformerEndpointConfig(
        id=f"e2e-nopull-{uuid.uuid4().hex[:8]}",
        mode="ephemeral",
        roles=["implementer"],
        image="example.invalid/does-not-exist:nope",
        readiness_timeout_s=90,
    )

    with pytest.raises(performer_lifecycle.LifecycleError) as excinfo:
        asyncio.run(runtime.start_ephemeral(bad))

    assert "Pull" in str(excinfo.value) or "Image" in str(excinfo.value), (
        f"the failure should name the image problem, got: {excinfo.value}"
    )
    asyncio.run(
        runtime.stop(
            __import__(
                "coordinare.services.kubernetes_runtime", fromlist=["pod_name_for"],
            ).pod_name_for(bad.id),
        ),
    )


def _can_i(*, verb: str, resource: str, namespace: str | None, subresource: str = "") -> bool:
    """Ask the cluster whether the coordinare ServiceAccount may do this.

    Uses ``SubjectAccessReview``, which is the authorizer's own answer rather
    than our reading of the manifest. That distinction is the point of this
    check: the unit tests assert the YAML *declares* a set of permissions, which
    cannot tell you whether the grant is sufficient to run, nor whether it is
    wider than claimed.
    """
    from kubernetes import client

    review = client.AuthorizationV1Api().create_subject_access_review(
        client.V1SubjectAccessReview(
            spec=client.V1SubjectAccessReviewSpec(
                user=f"system:serviceaccount:{NAMESPACE}:coordinare",
                resource_attributes=client.V1ResourceAttributes(
                    namespace=namespace,
                    verb=verb,
                    resource=resource,
                    subresource=subresource,
                ),
            ),
        ),
    )
    return bool(review.status.allowed)


@pytest.fixture(scope="module")
def _rbac_applied() -> None:
    """Apply the shipped manifest, so this tests what operators actually get."""
    import subprocess

    result = subprocess.run(
        ["kubectl", "apply", "-n", NAMESPACE, "-f", "deploy/kubernetes/rbac.yaml"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.skip(f"could not apply RBAC: {result.stderr.strip()}")


@pytest.mark.parametrize(
    ("verb", "resource", "subresource"),
    [
        ("create", "pods", ""),
        ("get", "pods", ""),
        ("list", "pods", ""),
        ("delete", "pods", ""),
        ("get", "pods", "log"),
    ],
)
def test_shipped_rbac_grants_everything_the_runtime_actually_does(
    _rbac_applied, verb: str, resource: str, subresource: str,
) -> None:
    """SC-002, sufficiency half — the daemon can do its job under this Role.

    Each entry is an operation ``KubernetesRuntime`` genuinely performs:
    ``create`` to start, ``get``/``list`` to await readiness and sweep
    orphans, ``delete`` to stop, and ``pods/log`` to capture logs before removal.
    A grant that is missing one of these fails at dispatch time on a real
    cluster, which is far too late to discover it.
    """
    assert _can_i(verb=verb, resource=resource, namespace=NAMESPACE, subresource=subresource), (
        f"the shipped RBAC denies {verb} {resource}/{subresource or '-'}, which the runtime performs"
    )


@pytest.mark.parametrize(
    ("verb", "resource", "namespace"),
    [
        # Not a hypothetical: an earlier iteration of this work left a Role
        # under a previous name bound to the same ServiceAccount, and it kept
        # granting pods/watch after the shipped manifest had dropped it. Reading
        # rbac.yaml said "no watch"; the cluster said yes. That is the entire
        # reason these assertions ask the authorizer instead of the file.
        ("watch", "pods", NAMESPACE),
        ("get", "secrets", NAMESPACE),
        ("list", "secrets", NAMESPACE),
        ("create", "pods", "kube-system"),
        ("delete", "pods", "kube-system"),
        ("list", "nodes", None),
        ("create", "clusterrolebindings", None),
        ("delete", "namespaces", None),
    ],
)
def test_shipped_rbac_grants_nothing_beyond_that(
    _rbac_applied, verb: str, resource: str, namespace: str | None,
) -> None:
    """SC-002, the "and nothing more" half — the half a manifest cannot prove.

    Reading the YAML tells you what was *written*; it cannot tell you what the
    cluster will *permit*, because a stray ClusterRoleBinding elsewhere grants
    real authority while leaving this file untouched. Asking the authorizer is
    the only way to answer the question the security claim actually makes.

    ``pods`` in another namespace is deliberately included: namespace scoping is
    the entire reason this is a ``Role`` rather than a ``ClusterRole``, and it is
    the property that would silently disappear if someone "fixed" a permissions
    error by reaching for a ClusterRole.
    """
    assert not _can_i(verb=verb, resource=resource, namespace=namespace), (
        f"the coordinare ServiceAccount can {verb} {resource} "
        f"in {namespace or 'cluster scope'} — that is outside the documented grant"
    )
