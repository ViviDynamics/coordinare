"""Kubernetes implementation of :class:`PerformerRuntime` (spec 146 / issue #200).

Runs each performer as a **bare Pod**, so coordinare never needs ``docker.sock``.

**Why a Pod and not a Job.** The performer is a long-running server that coordinare
terminates when the work is done — ``stop()`` is a deliberate kill, not a wait for
completion. Under a Job the container never exits 0 on its own, coordinare's
termination is recorded as a failure, and backoff restarts it, producing a second
performer for work coordinare believes it has already ended. A Pod with
``restartPolicy: Never`` matches the actual lifecycle.

**Portability is the governing constraint.** This must run on EKS, vanilla
Kubernetes, microk8s, minikube and k3s, so: core APIs only, no custom resources,
no assumption that a StorageClass exists, and no assumption that the CNI enforces
NetworkPolicy.

**No egress control.** The Docker path adds ``NET_ADMIN`` to install iptables
rules inside the container. The Kubernetes-native equivalent is a NetworkPolicy,
which needs a CNI that enforces it — and kind, minikube and microk8s defaults
often do not. Shipping a policy that silently does not apply would be worse than
shipping none, so egress allowlisting is documented as Docker-only.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services import performer_lifecycle
from coordinare.services.performer_runtime import StartedPerformer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from coordinare.models.performer_endpoint import PerformerEndpointConfig

_log = structlog.get_logger(__name__)

#: The port the performer image serves on. Fixed by the image's entrypoint
#: (``python -m performer --serve --port 8088``), verified in the spike.
PERFORMER_PORT = 8088

#: Label tying a Pod back to the performer that owns it. Used by the orphan sweep
#: and by an operator reading `kubectl get pods`.
PERFORMER_ID_LABEL = "coordinare.vividynamics.com/performer-id"

#: Marks a Pod as coordinare's, so a sweep never touches anything else running in
#: the namespace.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "coordinare"

#: RFC 1123 label: lowercase alphanumerics and hyphens, start and end
#: alphanumeric, at most 63 characters.
_MAX_NAME = 63
_INVALID = re.compile(r"[^a-z0-9-]+")


def pod_name_for(performer_id: str) -> str:
    """Map a performer identifier onto a valid, unique, recognisable Pod name.

    Docker accepts names Kubernetes rejects (uppercase, underscores, slashes,
    arbitrary length), so this is not optional plumbing.

    Three properties matter, and the third is the one that bites:

    * **valid** — RFC 1123, ≤63 characters;
    * **deterministic** — the same performer maps to the same name every time;
    * **collision-free** — two identifiers differing only past the truncation
      point must still differ. A plain truncation would hand two performers the
      same Pod, and the failure would look like a scheduling bug rather than a
      naming one.

    The digest suffix buys the third property. It is short, but it is derived
    from the *whole* identifier, so truncation cannot erase the difference.
    """
    slug = _INVALID.sub("-", performer_id.strip().lower()).strip("-")
    digest = hashlib.sha256(performer_id.encode("utf-8")).hexdigest()[:8]

    keep = _MAX_NAME - len(digest) - 1
    slug = slug[:keep].strip("-")
    name = f"{slug}-{digest}" if slug else digest

    # A name must start with an alphanumeric; a leading digit is fine.
    if not name[0].isalnum():
        name = f"p{name}"[:_MAX_NAME].strip("-")
    return name


def endpoint_for(pod_ip: str) -> str:
    """The URL for a running Pod's performer.

    IPv6 must be bracketed: ``http://fd00::1:8088`` is unparseable, and a
    dual-stack cluster is not exotic.
    """
    host = f"[{pod_ip}]" if ":" in pod_ip else pod_ip
    return f"http://{host}:{PERFORMER_PORT}"


def classify_pod_failure(reason: str, *, message: str = "") -> performer_lifecycle.LifecycleError:
    """Map a Pod failure onto coordinare's **existing** error hierarchy.

    FR-009: a Pod being evicted, OOM-killed, or losing its node is the Kubernetes
    spelling of "the performer went away", which coordinare already handles. A new
    exception type would surface as a new terminal state and would need handling
    everywhere the old ones are handled.

    Unknown reasons classify too, deliberately. Kubernetes adds reasons over time,
    and one escaping as a bare ``Exception`` would bypass every ``except
    LifecycleError`` in the daemon — a failure mode that appears only when a new
    cluster version ships.
    """
    detail = f"performer Pod failed ({reason})"
    if message:
        detail = f"{detail}: {message}"
    return performer_lifecycle.ContainerStartError(detail)


def build_pod_manifest(
    config: PerformerEndpointConfig,
    *,
    pod_name: str,
    performer_id: str,
    extra_labels: dict[str, str] | None = None,
    cache_claim: str | None = None,
    image_pull_secrets: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the Pod manifest. Pure, so it is testable without a cluster.

    ``cache_claim`` is optional on purpose (FR-012): a cluster with no default
    StorageClass must run performers with a cold cache rather than fail, and that
    describes most minikube and microk8s installs.
    """
    labels = {
        PERFORMER_ID_LABEL: performer_id,
        MANAGED_BY_LABEL: MANAGED_BY_VALUE,
        **(extra_labels or {}),
    }

    container: dict[str, Any] = {
        "name": "performer",
        "image": config.image,
        # A node caches the 4 GB image after its first pull; every subsequent
        # performer scheduled there starts from that cache (FR-013).
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": PERFORMER_PORT}],
    }
    if env:
        container["env"] = [{"name": k, "value": v} for k, v in sorted(env.items())]

    spec: dict[str, Any] = {
        # Never: a restart would resurrect a performer coordinare has finished
        # with, which is the same reason this is a Pod and not a Job.
        "restartPolicy": "Never",
        # A performer runs AI-generated code. Kubernetes mounts a ServiceAccount
        # token into every Pod *by default*, so leaving this unset would hand that
        # code a live credential for the cluster API at a well-known path. A
        # performer never calls the Kubernetes API, so it has nothing to lose here
        # and an obvious escalation path to close: whatever the namespace's default
        # ServiceAccount can do, the model's output could do too.
        "automountServiceAccountToken": False,
        "containers": [container],
    }

    if cache_claim:
        container["volumeMounts"] = [{"name": "devenv-cache", "mountPath": "/devenv"}]
        spec["volumes"] = [
            {"name": "devenv-cache", "persistentVolumeClaim": {"claimName": cache_claim}}
        ]

    if image_pull_secrets:
        spec["imagePullSecrets"] = [{"name": s} for s in image_pull_secrets]

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "labels": labels},
        "spec": spec,
    }


def _load_kube_config() -> None:
    """Load in-cluster credentials, falling back to a kubeconfig.

    In-cluster is the supported deployment; kubeconfig is the development
    convenience of running the daemon on a laptop against a remote cluster. That
    second path is not guaranteed to reach Pod IPs, which is why it is a
    convenience rather than a supported mode.
    """
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


class KubernetesRuntime:
    """Runs performers as Pods in a single namespace."""

    def __init__(
        self,
        *,
        namespace: str = "default",
        cache_claim: str | None = None,
        image_pull_secrets: list[str] | None = None,
        core_v1: Any = None,
    ) -> None:
        self._namespace = namespace
        self._cache_claim = cache_claim
        self._image_pull_secrets = image_pull_secrets or []
        if core_v1 is None:
            from kubernetes import client as k8s_client

            _load_kube_config()
            core_v1 = k8s_client.CoreV1Api()
        self._api = core_v1

    # -- lifecycle ---------------------------------------------------------

    async def start_ephemeral(
        self,
        config: PerformerEndpointConfig,
        *,
        extra_labels: dict[str, str] | None = None,
    ) -> StartedPerformer:
        """Create the Pod and return once it is *addressable*.

        Addressable, not ready: the caller then polls ``/status`` via the shared
        ``performer_lifecycle.wait_ready``. This mirrors Docker, where ``run -d``
        returns before the service inside has bound.
        """
        import asyncio

        from kubernetes.client.rest import ApiException

        pod_name = pod_name_for(config.id)
        manifest = build_pod_manifest(
            config,
            pod_name=pod_name,
            performer_id=config.id,
            extra_labels=extra_labels,
            cache_claim=self._cache_claim,
            image_pull_secrets=self._image_pull_secrets,
            env=dict(config.env or {}),
        )

        try:
            await asyncio.to_thread(
                self._api.create_namespaced_pod, namespace=self._namespace, body=manifest
            )
        except ApiException as exc:
            if exc.status != 409:
                raise performer_lifecycle.ContainerStartError(
                    f"could not create performer Pod {pod_name!r} in namespace "
                    f"{self._namespace!r}: {exc.status} {exc.reason}"
                ) from exc

            # The name is taken. Because ``pod_name_for`` is deterministic, that
            # means this performer's *own* predecessor, and the interesting
            # question is whether it is on its way out.
            #
            # ``stop`` waits for the Pod it deleted, so the ordinary path never
            # arrives here. Other routes do: coordinare killed between the delete
            # request and the Pod actually going, an operator deleting a Pod by
            # hand, an eviction. In every one of those the predecessor is
            # terminating and the right answer is to wait for it, not to refuse
            # work the cluster is seconds away from allowing.
            await self._await_pod_gone(pod_name, timeout_s=config.readiness_timeout_s)
            try:
                await asyncio.to_thread(
                    self._api.create_namespaced_pod, namespace=self._namespace, body=manifest
                )
            except ApiException as retry_exc:
                # Still there. Either it is not terminating at all — a live
                # performer this coordinare does not know it owns — or termination
                # is stuck. Both are situations to report rather than to keep
                # retrying into.
                raise performer_lifecycle.ContainerStartError(
                    f"performer Pod {pod_name!r} already exists in namespace "
                    f"{self._namespace!r} and did not go away: "
                    f"{retry_exc.status} {retry_exc.reason}. Another coordinare may be "
                    "running against this namespace, or the Pod is stuck terminating."
                ) from retry_exc
            _log.info(
                "kubernetes_runtime.pod_recreated_after_predecessor",
                pod=pod_name,
                performer_id=config.id,
                detail="the previous Pod for this performer was still terminating",
            )

        _log.info(
            "kubernetes_runtime.pod_created",
            pod=pod_name,
            namespace=self._namespace,
            performer_id=config.id,
        )

        try:
            pod_ip = await self._await_pod_ip(pod_name, timeout_s=config.readiness_timeout_s)
        except BaseException:
            # The Pod exists from ``create_namespaced_pod`` onwards, so every exit
            # from here that is not a successful start has to remove it. Leaving it
            # costs more than the stranded resources: ``pod_name_for`` is
            # deterministic, so the *next* start of this performer collides with the
            # corpse and fails to create at all — an unschedulable image turns into
            # a permanently unstartable performer.
            #
            # ``BaseException`` on purpose: cancellation is the likeliest way out of
            # a readiness wait, and it leaks exactly like a failure does.
            await self.stop(pod_name, performer_id=config.id)
            raise

        return StartedPerformer(handle=pod_name, endpoint=endpoint_for(pod_ip))

    async def _await_pod_ip(self, pod_name: str, *, timeout_s: int) -> str:
        """Wait for the Pod to be Running with an IP, or explain why it will not be.

        A Pod that cannot be scheduled or cannot pull its image stays Pending
        indefinitely. Reporting "timed out" for that would send an operator
        looking at the performer when the answer is in the Pod's status — so
        terminal reasons are surfaced as soon as they appear rather than waited on.
        """
        import asyncio
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            pod = await asyncio.to_thread(
                self._api.read_namespaced_pod, name=pod_name, namespace=self._namespace
            )
            phase = pod.status.phase
            pod_ip = pod.status.pod_ip

            if phase == "Running" and pod_ip:
                return pod_ip
            if phase in {"Failed", "Succeeded"}:
                raise classify_pod_failure(phase, message=pod.status.reason or "")

            for cs in pod.status.container_statuses or []:
                waiting = getattr(cs.state, "waiting", None)
                if waiting and waiting.reason in {
                    "ImagePullBackOff",
                    "ErrImagePull",
                    "CreateContainerError",
                    "CreateContainerConfigError",
                }:
                    raise classify_pod_failure(waiting.reason, message=waiting.message or "")

            await asyncio.sleep(1.0)

        raise performer_lifecycle.ReadinessTimeoutError(
            f"performer Pod {pod_name!r} did not reach Running with an IP within {timeout_s}s"
        )

    async def stop(
        self,
        handle: str,
        *,
        timeout_s: int = 10,
        host_log_dir: Path | None = None,
        performer_id: str | None = None,
    ) -> None:
        """Capture logs, then delete the Pod. Best-effort throughout.

        The ordering is the requirement, not an optimisation: Pod logs are
        unreachable the instant the Pod is deleted, so capturing afterwards
        captures nothing.
        """
        import asyncio

        from kubernetes.client.rest import ApiException

        if host_log_dir is not None:
            await self._dump_pod_logs(handle, host_log_dir, performer_id=performer_id)

        try:
            await asyncio.to_thread(
                self._api.delete_namespaced_pod,
                name=handle,
                namespace=self._namespace,
                grace_period_seconds=timeout_s,
            )
        except ApiException as exc:
            if exc.status == 404:
                return  # already gone; nothing to do
            _log.warning("kubernetes_runtime.delete_failed", pod=handle, status=exc.status)
            return

        await self._await_pod_gone(handle, timeout_s=timeout_s)

    async def _await_pod_gone(self, pod_name: str, *, timeout_s: int) -> None:
        """Block until the Pod is actually gone, matching Docker's ``stop`` semantics.

        ``delete_namespaced_pod`` only *requests* deletion; the Pod then sits in
        ``Terminating`` for up to its grace period. Returning early would be wrong
        in a way that surfaces later and confusingly:

        * ``pod_name_for`` is deterministic, so restarting the same performer while
          its predecessor is still terminating fails with "already exists" — a name
          collision Docker never has, because container names are freed immediately;
        * ``cleanup_orphaned`` would report Pods reaped that are still running.

        Best-effort: a Pod that outlives the wait is logged, not raised. ``stop`` is
        documented as best-effort and a slow termination is not a reason to fail the
        work that has already completed.
        """
        import asyncio
        import time

        from kubernetes.client.rest import ApiException

        deadline = time.monotonic() + timeout_s + 5  # grace period plus slack
        while time.monotonic() < deadline:
            try:
                await asyncio.to_thread(
                    self._api.read_namespaced_pod, name=pod_name, namespace=self._namespace
                )
            except ApiException as exc:
                if exc.status == 404:
                    return  # gone, as intended
                # Any other status says the *question* failed, not that the Pod
                # went away. Returning here would report deletion on a 500 or a
                # 401 and hand the collision to the next start of this performer.
                _log.debug(
                    "kubernetes_runtime.pod_gone_check_failed",
                    pod=pod_name,
                    status=exc.status,
                    detail="retrying; a failed query is not a deleted Pod",
                )
            await asyncio.sleep(0.5)

        _log.warning(
            "kubernetes_runtime.pod_still_terminating",
            pod=pod_name,
            detail="Pod outlived its grace period; a restart of this performer may collide",
        )

    async def _dump_pod_logs(
        self, pod_name: str, host_log_dir: Path, *, performer_id: str | None
    ) -> None:
        import asyncio

        from kubernetes.client.rest import ApiException

        try:
            logs = await asyncio.to_thread(
                self._api.read_namespaced_pod_log, name=pod_name, namespace=self._namespace
            )
        except ApiException as exc:
            _log.warning("kubernetes_runtime.logs_unavailable", pod=pod_name, status=exc.status)
            return

        try:
            host_log_dir.mkdir(parents=True, exist_ok=True)
            (host_log_dir / f"{performer_id or pod_name}.log").write_text(logs, encoding="utf-8")
        except OSError as exc:
            _log.warning("kubernetes_runtime.log_write_failed", pod=pod_name, error=str(exc))

    async def tail_logs(self, handle: str, *, lines: int = 200) -> list[str]:
        """Live log lines from a running Pod, via the same ``pods/log`` grant."""
        import asyncio

        from kubernetes.client.rest import ApiException

        try:
            raw = await asyncio.to_thread(
                self._api.read_namespaced_pod_log,
                name=handle,
                namespace=self._namespace,
                tail_lines=lines,
            )
        except ApiException as exc:
            # A Pod that is still creating has no log endpoint yet, which is
            # ordinary during startup rather than a fault worth surfacing.
            _log.debug(
                "kubernetes_runtime.tail_logs_unavailable",
                pod=handle,
                status=exc.status,
            )
            return []
        return (raw or "").splitlines()[-lines:]

    async def cleanup_orphaned(self, performer_id: str | None = None) -> int:
        """Delete Pods left by a previous coordinare.

        Scoped by label so a sweep never touches anything else in the namespace,
        and narrowable to one performer so it cannot reap another coordinare's
        running work when two share a namespace.
        """
        import asyncio

        from kubernetes.client.rest import ApiException

        # Both labels, and the second is what makes this safe rather than tidy.
        # Every performer Pod carries a performer-id; nothing else coordinare
        # deploys does. Selecting on managed-by ALONE swept up the controller's own
        # Pod, because the Helm chart labels it managed-by=coordinare too — so on
        # startup coordinare deleted itself, restarted, and deleted itself again.
        # Requiring the performer-id label to exist excludes the controller by
        # construction, whatever else happens to share the managed-by label.
        selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{PERFORMER_ID_LABEL}"
        if performer_id:
            # Narrow the existence check to one id.
            selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{PERFORMER_ID_LABEL}={performer_id}"

        try:
            pods = await asyncio.to_thread(
                self._api.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=selector,
            )
        except ApiException as exc:
            _log.warning("kubernetes_runtime.sweep_list_failed", status=exc.status)
            return 0

        stopped = 0
        for pod in pods.items:
            await self.stop(pod.metadata.name)
            stopped += 1
        if stopped:
            _log.info("kubernetes_runtime.swept", count=stopped, selector=selector)
        return stopped
