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

import asyncio
import hashlib
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, TypedDict, Unpack

import structlog
from kubernetes.client.rest import ApiException

from coordinare.env_placeholders import drop_unset_placeholders
from coordinare.services import performer_lifecycle
from coordinare.services.kubernetes_verifier import (
    MAX_VERIFIER_SOURCE_SECRETS,
    OWNER_LABEL,
    VERIFIER_CONTAINER,
    VERIFIER_DEADLINE_S,
    VERIFIER_LOG_MAX_BYTES,
    VERIFIER_LOG_TAIL_LINES,
    VERIFIER_POLL_INTERVAL_S,
    VERIFIER_REMOVAL_TIMEOUT_S,
    VERIFIER_ROLE,
    VERIFIER_ROLE_LABEL,
    VerifierConfigurationError,
    VerifierSpec,
    build_verifier_credentials_secret,
    build_verifier_pod_manifest,
    build_verifier_source_secrets,
    source_secret_name,
    verifier_pod_name,
)
from coordinare.services.performer_runtime import StartedPerformer
from coordinare.services.run_log import LOG_CHUNK_BYTES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncGenerator

    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.run_log import RunLogIO


@dataclass
class _VerifierAttempt:
    name: str
    secret_attempted: bool = False
    pod_attempted: bool = False
    removed: bool = True
    credentials_removed: bool = True
    source_names: list[str] = field(default_factory=list)
    sources_removed: bool = True

    def creation_conflict(self, resource: str) -> None:
        if resource.startswith('source:'):
            name = resource.removeprefix('source:')
            if name in self.source_names:
                self.source_names.remove(name)
        elif resource == 'secret':
            self.secret_attempted = False
        else:
            self.pod_attempted = False
            self.removed = False


async def _drain_verifier_task(task: asyncio.Task[Any]) -> None:
    """Let an in-flight API request finish despite repeated caller cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break


async def _create_verifier_resource(
    call: Any, *, attempt: _VerifierAttempt, resource: str, **kwargs: Any,
) -> Any:
    task = asyncio.create_task(asyncio.to_thread(call, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain_verifier_task(task)
        failure = task.exception() if not task.cancelled() else None
        if isinstance(failure, ApiException) and failure.status == HTTPStatus.CONFLICT:
            attempt.creation_conflict(resource)
        raise

_log = structlog.get_logger(__name__)

#: The port the performer image serves on. Fixed by the image's entrypoint
#: (``python -m performer --serve --port 8088``), verified in the spike.
VERIFIER_INFRASTRUCTURE_REASONS = frozenset({"DeadlineExceeded", "Evicted", "NodeLost", "Shutdown", "OutOfcpu", "OutOfmemory"})

VERIFIER_WAITING_INFRASTRUCTURE_REASONS = frozenset({
    "ErrImagePull", "ImagePullBackOff", "CreateContainerConfigError",
})

PERFORMER_PORT = 8088

#: Label tying a Pod back to the performer that owns it. Used by the orphan sweep
#: and by an operator reading `kubectl get pods`.
PERFORMER_ID_LABEL = "coordinare.vividynamics.com/performer-id"

#: Marks a Pod as coordinare's, so a sweep never touches anything else running in
#: the namespace.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "coordinare"

#: Label tying a Pod back to the coordinare-allocated session (the daemon's
#: ``_active_jobs`` key). Existence-matched by the session-aware orphan sweep
#: (issue #489) so pods belonging to other coordinare deployments (the
#: controller itself) are excluded by construction.
SESSION_ID_LABEL = "coordinare.session_id"
_UNOWNED_SESSION = "cannot reconcile Kubernetes session without deployment ownership"
_AMBIGUOUS_SESSION = "multiple owned pods match restored performer session"
_UNREADY_SESSION = "restored performer pod is not ready for adoption"
_UNOWNED_PREDECESSOR = "cannot replace performer pod without proof of deployment ownership"

#: RFC 1123 label: lowercase alphanumerics and hyphens, start and end
#: alphanumeric, at most 63 characters.
_MAX_NAME = 63
_INVALID = re.compile(r"[^a-z0-9-]+")
# 120: CSI and OSC terminal escapes, stripped from live log lines.
# 132: connect and read timeouts for a Pod log stream. A stream silent for longer than the
# read timeout fails, so a hung read cannot hold a worker for ever.
_LOG_REQUEST_TIMEOUT = (10.0, 60.0)


async def _run_log_read(io: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """132: one blocking log read, on the run-log pool when one is given, else the default pool."""
    if io is None:
        return await asyncio.to_thread(fn, *args, **kwargs)
    return await io.run(fn, *args, **kwargs)


_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_BYTES_REPR = re.compile(r"""^b(['"]).*\1$""", re.DOTALL)


def _decode_pod_log(raw: Any) -> str:
    """120: turn whatever ``read_namespaced_pod_log`` returned into real text.

    The call is declared to return ``str``, but the kubernetes client builds
    that by calling ``str()`` on the response body when it is not JSON, so a
    plain-text log comes back as the *repr* of a bytes object: one line that
    starts ``b'`` and carries literal ``\\n`` and ``\\xe2`` sequences. Accept
    every shape the client can hand over (a response object with ``.data``,
    bytes, a proper string, or that repr) and decode as UTF-8.
    """
    import ast

    data = getattr(raw, "data", raw)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    if not isinstance(data, str):
        return ""
    if _BYTES_REPR.match(data):
        try:
            literal = ast.literal_eval(data)
        except (ValueError, SyntaxError):
            return data
        if isinstance(literal, bytes):
            return literal.decode("utf-8", "replace")
    return data



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
    pod_env = dict(env or {})
    routing = getattr(config, "kubernetes_routing_configmap", None)
    if routing is not None:
        pod_env["SELFHOSTED_ROUTING_CONFIG"] = "/etc/coordinare/routing/routing.yaml"
    if pod_env:
        container["env"] = [{"name": k, "value": v} for k, v in sorted(pod_env.items())]

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

    # 159: mirror the Docker path rather than inventing a policy here.
    # get_env_volume_for_symphony has already decided which symphony this
    # performer may see and whether it may write: rw for the bootstrap dispatch
    # that BUILDS the cache, ro for every consumer that reads it. Mounting the
    # claim root read-write for everybody was safe on a local disk with one
    # performer and is not on a shared NFS claim, where several performers can
    # write one tree at once.
    #
    # The subpath and the mount path come from ONE source, the container path.
    # Review found the first version taking the subpath from `host_path.name`
    # while the mount path came from `container_path` -- two sources for one
    # relationship, and two symphonies whose cache directories shared a basename
    # collided on a single subpath with the second mounted read-write. That
    # performer would write into the other symphony's cache, which is precisely
    # what this mount exists to prevent.
    #
    # getattr rather than attribute access: this function is documented as pure
    # and testable without a cluster, and its tests pass minimal duck-typed
    # configs that carry only what the case needs. Defaults match the model's.
    devenv_root = str(getattr(config, "container_devenv_root", None) or "/devenv").rstrip("/")
    devenv_mounts: list[dict[str, Any]] = []
    # A configured root of "/" rstrips to "", and `relative_to("")` refuses every
    # absolute path -- so nothing is treated as a cache mount, which is the right
    # answer: with the root at "/" there is no symphony segment to name, and a
    # string-prefix filter would have admitted every volume the performer has,
    # including a secrets mount. Verified rather than assumed; an explicit
    # empty-root guard here was dead code no test could distinguish.
    for volume in getattr(config, "volumes", None) or []:
        container_path = PurePosixPath(str(volume.container_path))
        try:
            relative = container_path.relative_to(devenv_root)
        except ValueError:
            # Not under the root at all. `/devenvil/x` starts with `/devenv`
            # as a string and is a different directory.
            continue
        parts = relative.parts
        if len(parts) != 1:
            raise ValueError(
                f"cache volume {container_path} has no single-segment subpath under "
                f"{devenv_root}: the claim holds one directory per symphony, and a "
                f"path {'at the root' if not parts else 'nested deeper'} names no "
                f"symphony to mount",
            )
        devenv_mounts.append(
            {
                "name": "devenv-cache",
                "mountPath": str(container_path),
                "subPath": parts[0],
                "readOnly": volume.mode == "ro",
            },
        )

    if cache_claim and devenv_mounts:
        container["volumeMounts"] = devenv_mounts
        spec["volumes"] = [
            {"name": "devenv-cache", "persistentVolumeClaim": {"claimName": cache_claim}},
        ]

    if routing is not None:
        # Project just the routing key in its own directory. The kubelet fetches
        # it; the daemon needs no ConfigMap API access or local copy of the file.
        container.setdefault("volumeMounts", []).append({
            "name": "selfhosted-routing",
            "mountPath": "/etc/coordinare/routing",
            "readOnly": True,
        })
        spec.setdefault("volumes", []).append({
            "name": "selfhosted-routing",
            "configMap": {
                "name": routing.name,
                "items": [{"key": routing.key, "path": "routing.yaml"}],
            },
        })

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


class _OwnershipOptions(TypedDict, total=False):
    owner: str | None


class KubernetesRuntime:
    """Runs performers as Pods in a single namespace."""

    def __init__(
        self,
        *,
        namespace: str = "default",
        cache_claim: str | None = None,
        image_pull_secrets: list[str] | None = None,
        core_v1: Any = None,
        image_resolver: Any = None,
        **ownership: Unpack[_OwnershipOptions],
    ) -> None:
        if set(ownership) - {"owner"}:
            raise TypeError("unexpected KubernetesRuntime ownership option")
        self._namespace = namespace
        self._owner = ownership.get("owner")
        self._cache_claim = cache_claim
        self._image_pull_secrets = image_pull_secrets or []
        self._image_resolver = image_resolver  # #526 digest pinning (optional)
        if core_v1 is None:
            from kubernetes import client as k8s_client

            _load_kube_config()
            core_v1 = k8s_client.CoreV1Api()
        self._api = core_v1

    async def _pin_image(self, config: PerformerEndpointConfig) -> PerformerEndpointConfig:
        """Resolve the configured image tag to its registry digest (#526).

        No-op when no resolver is wired (tests, bench) or the image is unset.
        """
        if self._image_resolver is None or config.image is None:
            return config
        image = await self._image_resolver.pin(config.image)
        if image == config.image:
            return config
        return config.model_copy(update={"image": image})

    # -- lifecycle ---------------------------------------------------------

    async def start_ephemeral(
        self,
        config: PerformerEndpointConfig,
        *,
        extra_labels: dict[str, str] | None = None,
        backend: str | None = None,
    ) -> StartedPerformer:
        """Create the Pod and return once it is *addressable*.

        Addressable, not ready: the caller then polls ``/status`` via the shared
        ``performer_lifecycle.wait_ready``. This mirrors Docker, where ``run -d``
        returns before the service inside has bound.
        """
        import asyncio

        from kubernetes.client.rest import ApiException

        pod_name = pod_name_for(f"{self._owner}/{config.id}" if self._owner else config.id)
        # Issue #486: the image's entrypoint.sh installs the backend CLI at
        # container start from $BACKEND, and the pod env comes exclusively from
        # performer_endpoints[].env — the resolved backend reached the performer
        # only in the job payload, after the container already existed. Derive
        # BACKEND from the dispatch's resolved backend unless the operator
        # already set it. An unresolved ${VAR} placeholder in the operator env
        # does not count as set (the docker path drops those literals, and the
        # entrypoint would treat the literal as an unknown backend either way).
        # 252: an entry with an unset placeholder is left out of the pod, as the docker path leaves it out.
        pod_env, env_dropped = drop_unset_placeholders(config.env or {})
        for env_key in env_dropped:
            _log.info("kubernetes_runtime.env_skipped_unresolved_placeholder", pod=pod_name, env_key=env_key)
        if backend and not performer_lifecycle.env_carries_set_value(pod_env, "BACKEND"):
            pod_env["BACKEND"] = performer_lifecycle.normalize_backend_name(backend)
            _log.info(
                "kubernetes_runtime.backend_env_derived",
                pod=pod_name,
                performer_id=config.id,
                backend=backend,
            )
        config = await self._pin_image(config)
        manifest = build_pod_manifest(
            config,
            pod_name=pod_name,
            performer_id=config.id,
            extra_labels={
                **(extra_labels or {}),
                **({OWNER_LABEL: self._owner} if self._owner else {}),
            },
            cache_claim=self._cache_claim,
            image_pull_secrets=self._image_pull_secrets,
            env=pod_env,
        )

        try:
            await asyncio.to_thread(
                self._api.create_namespaced_pod, namespace=self._namespace, body=manifest,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise performer_lifecycle.ContainerStartError(
                    f"could not create performer Pod {pod_name!r} in namespace "
                    f"{self._namespace!r}: {exc.status} {exc.reason}",
                ) from exc

            if not self._owner or not await self._predecessor_is_owned(pod_name):
                raise performer_lifecycle.ContainerStartError(_UNOWNED_PREDECESSOR) from exc

            # The name is taken. Because ``pod_name_for`` is deterministic, the
            # Pod holding it is this performer's *own* predecessor by
            # construction — same deployment, performer, namespace and name. That
            # holds however the predecessor is doing: terminating (coordinare
            # killed between the delete request and the Pod actually going, an
            # operator deletion, an eviction) or still Running, because a turn
            # that ended without a stop leaves a live, successfully-completed
            # Pod behind and this is the next turn of the same performer.
            # Docker never reaches this state because its ``stop`` frees the
            # name immediately; the equivalent here is delete-then-recreate,
            # not only waiting. Deleting is safe precisely because the name is
            # deterministic and owned.
            predecessor_phase = await self._predecessor_phase(pod_name)
            try:
                await self.stop(pod_name, timeout_s=5)
            except Exception as exc:
                # ``stop`` is best-effort for API rejections but lets transport
                # failures escape, and a dispatch should see a normal start
                # failure rather than a raw connection error. Absorb it and
                # keep the one-shot recreate: the create either succeeds (the
                # delete landed) or 409s into the escalation below.
                _log.warning(
                    "kubernetes_runtime.predecessor_cleanup_failed",
                    pod=pod_name,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            try:
                await asyncio.to_thread(
                    self._api.create_namespaced_pod, namespace=self._namespace, body=manifest,
                )
            except ApiException as retry_exc:
                # The name survived a delete-and-wait. Either it is not ours at
                # all — a second coordinare against this namespace — or
                # termination is stuck beyond what a delete can resolve. Both
                # are situations to report rather than to keep retrying into.
                raise performer_lifecycle.ContainerStartError(
                    f"performer Pod {pod_name!r} already exists in namespace "
                    f"{self._namespace!r} and did not go away: "
                    f"{retry_exc.status} {retry_exc.reason}. Another coordinare may be "
                    "running against this namespace, or the Pod is stuck terminating.",
                ) from retry_exc
            _log.info(
                "kubernetes_runtime.pod_recreated_after_predecessor",
                pod=pod_name,
                performer_id=config.id,
                predecessor_phase=predecessor_phase,
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
            await self.stop(pod_name)
            raise

        return StartedPerformer(handle=pod_name, endpoint=endpoint_for(pod_ip), image_ref=config.image)

    async def _predecessor_is_owned(self, pod_name: str) -> bool:
        try:
            pod = await asyncio.to_thread(
                self._api.read_namespaced_pod, name=pod_name, namespace=self._namespace,
            )
        except Exception:
            return False
        return (getattr(pod.metadata, "labels", None) or {}).get(OWNER_LABEL) == self._owner

    async def _predecessor_phase(self, pod_name: str) -> str:
        """Best-effort read of the Pod holding the name, for the log line only.

        Evidence, not control flow: any read failure — an API rejection or a
        transport error — reports ``unknown`` and the delete-then-recreate
        proceeds regardless, because the 409 already proved a Pod is sitting
        on the name.
        """
        try:
            pod = await asyncio.to_thread(
                self._api.read_namespaced_pod, name=pod_name, namespace=self._namespace,
            )
        except Exception as exc:
            _log.debug(
                "kubernetes_runtime.predecessor_read_failed",
                pod=pod_name,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return "unknown"
        phase = str(getattr(pod.status, "phase", None) or "unknown")
        terminating = getattr(pod.metadata, "deletion_timestamp", None) is not None
        return f"{phase}/terminating" if terminating else phase

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
                self._api.read_namespaced_pod, name=pod_name, namespace=self._namespace,
            )
            phase = pod.status.phase
            pod_ip = pod.status.pod_ip

            if phase == "Running" and pod_ip:
                return str(pod_ip)
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
            f"performer Pod {pod_name!r} did not reach Running with an IP within {timeout_s}s",
        )

    async def find_session(self, session_id: str) -> StartedPerformer | None:
        """Resolve a restored session only within this deployment's ownership."""
        if not self._owner:
            raise RuntimeError(_UNOWNED_SESSION)
        selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{SESSION_ID_LABEL}={session_id},{OWNER_LABEL}={self._owner}"
        pods = await asyncio.to_thread(
            self._api.list_namespaced_pod, namespace=self._namespace, label_selector=selector,
        )
        matches = [pod for pod in pods.items if (
            (getattr(pod.metadata, "labels", None) or {}).get(OWNER_LABEL) == self._owner
            and (getattr(pod.metadata, "labels", None) or {}).get(SESSION_ID_LABEL) == session_id
        )]
        if not matches:
            return None
        if len(matches) != 1:
            raise RuntimeError(_AMBIGUOUS_SESSION)
        pod = matches[0]
        phase = str(getattr(pod.status, "phase", None) or "")
        if phase in {"Failed", "Succeeded"}:
            return StartedPerformer(handle=str(pod.metadata.name), endpoint="")
        pod_ip = getattr(pod.status, "pod_ip", None)
        if phase != "Running" or not pod_ip:
            raise RuntimeError(_UNREADY_SESSION)
        return StartedPerformer(handle=str(pod.metadata.name), endpoint=endpoint_for(str(pod_ip)))

    async def stop(self, handle: str, *, timeout_s: int = 10) -> None:
        """Delete the Pod. Best-effort throughout.

        The output is not copied here (spec 208): the full run log (132) is read through
        ``iter_logs`` before this call, since Pod logs are unreachable once the Pod is
        deleted.
        """
        import asyncio

        from kubernetes.client.rest import ApiException

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

    async def confirm_stopped(self, handle: str, *, timeout_s: int = 10) -> bool:
        """218 (review round 3): True only when the Pod is gone, proven by a read that returns 404.

        Deleting a Pod only requests it, and the Pod sits in ``Terminating`` for its grace period. So
        the delete is followed by reads until the Pod reads 404 or the budget runs out. A read that
        fails for any other reason is not proof, and neither is a Pod that still reads.
        """
        try:
            await asyncio.to_thread(
                self._api.delete_namespaced_pod,
                name=handle,
                namespace=self._namespace,
                grace_period_seconds=timeout_s,
            )
        except ApiException as exc:
            if exc.status == 404:
                return True
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                await asyncio.to_thread(
                    self._api.read_namespaced_pod, name=handle, namespace=self._namespace,
                )
            except ApiException as exc:
                return bool(exc.status == 404)
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

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
                    self._api.read_namespaced_pod, name=pod_name, namespace=self._namespace,
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

    @property
    def verifies_clean_room(self) -> bool:
        """265: this runtime can run the clean-room verifier pod (see run_verifier)."""
        return True

    async def run_verifier(
        self, *, slug: str, spec: VerifierSpec, poll_interval_s: float = VERIFIER_POLL_INTERVAL_S,
    ) -> tuple[bool | None, str]:

        try:
            return await self._run_verifier_with_source(slug=slug, spec=spec, poll_interval_s=poll_interval_s)
        finally:
            if not spec.walk_only and spec.execution_source_workspace is not None:
                shutil.rmtree(spec.execution_source_workspace)

    async def _run_verifier_with_source(
        self,
        *,
        slug: str,
        spec: VerifierSpec,
        poll_interval_s: float = VERIFIER_POLL_INTERVAL_S,
    ) -> tuple[bool | None, str]:
        """265: run the daemon's verify text in a clean-room verifier pod and return the verdict.

        ``(True, _)`` when the pod succeeded, ``(False, reason)`` when the verify failed, and ``(None, reason)``
        when the pod could not be run or timed out: infrastructure, not a verdict. The pod is removed in every path.
        Invalid inputs raise VerifierConfigurationError before any pod is made. Success also requires confirmed removal
        of the pod, so another pod can safely start only after this method returns a pass.
        """
        if not self._cache_claim:
            return None, "no devenv cache claim is configured, so the verifier cannot mount the cache"
        try:
            manifest = build_verifier_pod_manifest(
                replace(spec, cache_claim=self._cache_claim),
                name=verifier_pod_name(slug),
                namespace=self._namespace,
                image_pull_secrets=tuple(self._image_pull_secrets),
                owner=self._owner,
            )
        except (ValueError, UnicodeError) as exc:
            message = f"verifier pod spec refused: {exc}"
            raise VerifierConfigurationError(message) from exc
        name = str(manifest["metadata"]["name"])
        attempt = _VerifierAttempt(name)
        configuration_error = None
        try:
            creation_error = await self._prepare_verifier_resources(attempt, spec, manifest)
            if creation_error is not None:
                result: tuple[bool | None, str] = (None, creation_error)
            else:
                result = await self._await_verifier(name, walk_only=spec.walk_only, poll_interval_s=poll_interval_s)
        except VerifierConfigurationError as exc:
            configuration_error = exc
            result = None, str(exc)
        except Exception as exc:
            result = None, f'verifier pod could not be read: {type(exc).__name__}'
        finally:
            await self._finish_verifier_attempt(attempt)
        if not attempt.removed:
            return None, 'verifier pod could not be confirmed removed; retry before starting another pod'
        if not attempt.sources_removed:
            return None, "verifier source Secrets could not be confirmed removed"
        if not attempt.credentials_removed:
            return None, 'verifier credential Secret could not be confirmed removed'
        if configuration_error is not None:
            raise configuration_error
        return result

    async def _prepare_verifier_resources(
        self, attempt: _VerifierAttempt, spec: VerifierSpec, manifest: dict[str, Any],
    ) -> str | None:
        # A missing Secret keeps the Pod pending. Its returned UID lets Kubernetes
        # garbage-collect credentials if the daemon crashes before explicit cleanup.
        attempt.pod_attempted = True
        try:
            pod = await _create_verifier_resource(
                self._api.create_namespaced_pod, attempt=attempt, resource='pod',
                namespace=self._namespace, body=manifest,
            )
        except ApiException as exc:
            if exc.status == HTTPStatus.CONFLICT:
                attempt.creation_conflict('pod')
            return 'verifier pod could not be created'
        except Exception as exc:
            return f'verifier pod could not be created: {type(exc).__name__}'
        if (spec.execution_source is not None or spec.project_files) and not spec.walk_only:
            error = await self._prepare_verifier_sources(attempt, spec, pod)
            if error is not None:
                return error
        if not spec.transient_credentials:
            return None
        return await self._prepare_verifier_credentials(attempt, spec, pod)

    async def _prepare_verifier_sources(self, attempt: _VerifierAttempt, spec: VerifierSpec, pod: Any) -> str | None:
        metadata = pod.get("metadata", {}) if isinstance(pod, dict) else getattr(pod, "metadata", None)
        pod_uid = metadata.get("uid") if isinstance(metadata, dict) else getattr(metadata, "uid", None)
        if not isinstance(pod_uid, str) or not pod_uid:
            return "verifier Pod UID is missing; refusing unowned source Secret"
        for secret in build_verifier_source_secrets(spec, name=attempt.name, namespace=self._namespace,
                                                   owner=self._owner, pod_uid=pod_uid):
            secret_name = secret["metadata"]["name"]
            attempt.source_names.append(secret_name)
            try:
                await _create_verifier_resource(self._api.create_namespaced_secret,
                    attempt=attempt, resource="source:" + secret_name, namespace=self._namespace, body=secret)
            except ApiException as exc:
                if exc.status == HTTPStatus.CONFLICT:
                    attempt.creation_conflict("source:" + secret_name)
                return "verifier source Secret could not be created"
            except Exception:
                return "verifier source Secret could not be created"
        return None

    async def _prepare_verifier_credentials(
        self, attempt: _VerifierAttempt, spec: VerifierSpec, pod: Any,
    ) -> str | None:
        metadata = pod.get('metadata', {}) if isinstance(pod, dict) else getattr(pod, 'metadata', None)
        pod_uid = metadata.get('uid') if isinstance(metadata, dict) else getattr(metadata, 'uid', None)
        if not isinstance(pod_uid, str) or not pod_uid:
            return 'verifier Pod UID is missing; refusing unowned credential Secret'
        attempt.secret_attempted = True
        try:
            await _create_verifier_resource(
                self._api.create_namespaced_secret, attempt=attempt, resource='secret',
                namespace=self._namespace, body=build_verifier_credentials_secret(
                    spec, name=attempt.name, namespace=self._namespace, owner=self._owner, pod_uid=pod_uid,
                ),
            )
        except ApiException as exc:
            if exc.status == HTTPStatus.CONFLICT:
                attempt.creation_conflict('secret')
            return 'verifier credential Secret could not be created'
        except Exception as exc:
            return f'verifier credential Secret could not be created: {type(exc).__name__}'
        return None

    async def _cleanup_verifier_attempt(self, attempt: _VerifierAttempt) -> None:
        if attempt.pod_attempted:
            attempt.removed = await self._remove_verifier(attempt.name)
        if attempt.removed:
            for name in attempt.source_names:
                removed = await self._remove_verifier_credentials(name)
                attempt.sources_removed = attempt.sources_removed and removed
        elif attempt.source_names:
            attempt.sources_removed = False
        if attempt.secret_attempted and attempt.removed:
            attempt.credentials_removed = await self._remove_verifier_credentials(attempt.name + '-credentials')

    async def _finish_verifier_attempt(self, attempt: _VerifierAttempt) -> None:
        task = asyncio.create_task(self._cleanup_verifier_attempt(attempt))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _drain_verifier_task(task)
            await task
            raise

    async def _remove_verifier_credentials(self, name: str) -> bool:
        """Delete only after the consuming Pod is gone and confirm API absence."""
        try:
            await asyncio.to_thread(self._api.delete_namespaced_secret, name=name, namespace=self._namespace)
        except ApiException as exc:
            return bool(exc.status == HTTPStatus.NOT_FOUND)
        except Exception:
            return False
        deadline = asyncio.get_running_loop().time() + VERIFIER_REMOVAL_TIMEOUT_S
        while True:
            try:
                await asyncio.to_thread(self._api.read_namespaced_secret, name=name, namespace=self._namespace)
            except ApiException as exc:
                return bool(exc.status == HTTPStatus.NOT_FOUND)
            except Exception:
                return False
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _await_verifier(self, name: str, *, walk_only: bool, poll_interval_s: float) -> tuple[bool | None, str]:
        deadline = asyncio.get_running_loop().time() + VERIFIER_DEADLINE_S
        while True:
            pod = await asyncio.to_thread(self._api.read_namespaced_pod, name=name, namespace=self._namespace)
            status = getattr(pod, "status", None)
            statuses = list(getattr(status, "container_statuses", None) or ())
            statuses.extend(getattr(status, "init_container_statuses", None) or ())
            for container in statuses:
                waiting = getattr(getattr(container, "state", None), "waiting", None)
                reason = getattr(waiting, "reason", None)
                if reason == "InvalidImageName":
                    message = "verifier image refused by Kubernetes: InvalidImageName; correct kubernetes_verifier_image"
                    raise VerifierConfigurationError(message)
                if reason in VERIFIER_WAITING_INFRASTRUCTURE_REASONS:
                    return None, f"verifier container infrastructure failure: {reason}"
            phase = getattr(status, "phase", None)
            if phase == "Succeeded":
                return True, await self._verifier_logs(name)
            if phase == "Failed" and getattr(pod.status, "reason", None) in VERIFIER_INFRASTRUCTURE_REASONS:
                return None, f"verifier pod infrastructure failure: {pod.status.reason}"
            if phase == "Failed":
                try:
                    detail = await self._verifier_logs(name)
                except OSError:
                    if walk_only:
                        raise
                    detail = "verifier pod failed; logs unavailable"
                return False, detail
            if asyncio.get_running_loop().time() >= deadline:
                return None, f"verifier timed out after {VERIFIER_DEADLINE_S}s"
            await asyncio.sleep(poll_interval_s)

    async def _verifier_logs(self, name: str) -> str:
        try:
            text = await asyncio.to_thread(
                self._api.read_namespaced_pod_log, name=name, namespace=self._namespace,
                container=VERIFIER_CONTAINER, tail_lines=VERIFIER_LOG_TAIL_LINES, limit_bytes=VERIFIER_LOG_MAX_BYTES,
            )
        except Exception as exc:
            message = "verifier logs unavailable"
            raise OSError(message) from exc
        return str(text).encode("utf-8")[-VERIFIER_LOG_MAX_BYTES:].decode("utf-8", errors="replace")

    async def _remove_verifier(self, name: str) -> bool:
        try:
            return await self.confirm_stopped(name, timeout_s=VERIFIER_REMOVAL_TIMEOUT_S)
        except Exception as exc:
            _log.warning("kubernetes_runtime.verifier_remove_failed", pod=name, error=type(exc).__name__)
            return False

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
        return [_ANSI_ESCAPE.sub("", line) for line in _decode_pod_log(raw).splitlines()[-lines:]]

    async def iter_logs(self, handle: str, *, io: RunLogIO | None = None) -> AsyncGenerator[bytes, None]:
        """132: the Pod's complete log, read from the API in chunks.

        ``_preload_content=False`` returns the raw response, so the bytes arrive as they
        are read and the writer decodes them. There is no ``tail_lines``: this is the
        whole log. The Pod's logs vanish with the Pod, so the caller reads this before
        the delete. Every read runs on the run-log pool ``io`` (never the default pool),
        and carries a read timeout: a silent stream ends its read, so it cannot hold a
        worker for ever.
        """
        response = await _run_log_read(
            io,
            self._api.read_namespaced_pod_log,
            name=handle,
            namespace=self._namespace,
            _preload_content=False,
            _request_timeout=_LOG_REQUEST_TIMEOUT,
        )
        chunks = response.stream(LOG_CHUNK_BYTES)
        try:
            while chunk := await _run_log_read(io, next, chunks, b""):
                yield chunk
        finally:
            response.close()

    async def cleanup_orphaned(self, performer_id: str | None = None) -> int:
        """Delete Pods left by a previous coordinare.

        Scoped by stable deployment owner and optionally performer ID. Foreign
        and legacy unlabelled pods are preserved; without an owner, sweep nothing.
        """
        import asyncio

        from kubernetes.client.rest import ApiException

        if not self._owner:
            _log.warning("kubernetes_runtime.cleanup_skipped_no_owner")
            return 0

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
        selector += f",{OWNER_LABEL}={self._owner}"

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
            if (getattr(pod.metadata, "labels", None) or {}).get(OWNER_LABEL) != self._owner:
                continue
            await self.stop(pod.metadata.name)
            stopped += 1
        if stopped:
            _log.info("kubernetes_runtime.swept", count=stopped, selector=selector)
        return stopped

    async def sweep_orphaned_verifiers(self) -> list[str]:
        """Remove this deployment's interrupted verify and walk pods at startup.

        Verifiers cannot be adopted after restart, so no session keep set is needed.
        As with the session sweep, managed-by alone would also select the controller.
        Require the verifier role and deployment owner, and preserve legacy unowned pods.
        """
        if not self._owner:
            _log.warning("kubernetes_runtime.verifier_sweep_skipped_no_owner")
            return []
        required = {
            MANAGED_BY_LABEL: MANAGED_BY_VALUE,
            VERIFIER_ROLE_LABEL: VERIFIER_ROLE,
            OWNER_LABEL: self._owner,
        }
        selector = ",".join(f"{key}={value}" for key, value in required.items())
        try:
            pods = await asyncio.to_thread(
                self._api.list_namespaced_pod, namespace=self._namespace, label_selector=selector,
            )
        except Exception as exc:
            _log.warning("kubernetes_runtime.verifier_sweep_list_failed", error=str(exc))
            return []
        swept: list[str] = []
        for pod in pods.items:
            labels = getattr(pod.metadata, "labels", None) or {}
            if any(labels.get(key) != value for key, value in required.items()):
                continue
            metadata = pod.metadata
            annotations = getattr(metadata, "annotations", None) or {}
            try:
                count = int(annotations.get("coordinare.vividynamics.com/source-secrets", "0"))
            except (TypeError, ValueError):
                continue
            if not 0 <= count <= MAX_VERIFIER_SOURCE_SECRETS:
                continue
            source_names = []
            for index in range(count):
                secret_name = source_secret_name(metadata.name, index)
                if not await self._source_secret_owned(secret_name, getattr(metadata, "uid", None)):
                    break
                source_names.append(secret_name)
            else:
                if await self._remove_verifier(metadata.name):
                    removed = True
                    for secret_name in source_names:
                        gone = await self._remove_verifier_credentials(secret_name)
                        removed = removed and gone
                    if removed:
                        swept.append(metadata.name)
        return swept

    async def _source_secret_owned(self, name: str, pod_uid: str | None) -> bool:
        """Never delete a colliding Secret belonging to another attempt/deployment."""
        if not pod_uid:
            return False
        try:
            secret = await asyncio.to_thread(self._api.read_namespaced_secret, name=name, namespace=self._namespace)
        except ApiException as exc:
            return bool(exc.status == HTTPStatus.NOT_FOUND)
        except Exception:
            return False
        metadata = secret.get("metadata", {}) if isinstance(secret, dict) else getattr(secret, "metadata", None)
        if isinstance(metadata, dict):
            labels, references = metadata.get("labels", {}), metadata.get("ownerReferences", [])
        else:
            labels = getattr(metadata, "labels", None) or {}
            references = getattr(metadata, "owner_references", None) or []
        if (labels.get(OWNER_LABEL) != self._owner
                or labels.get(VERIFIER_ROLE_LABEL) != "env-verifier-source"
                or labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE):
            return False
        return any((ref.get("uid") if isinstance(ref, dict) else getattr(ref, "uid", None)) == pod_uid
                   for ref in references)

    async def sweep_orphaned_sessions(
        self, active_session_ids: set[str],
    ) -> list[str]:
        """Issue #489: delete managed Pods whose ``coordinare.session_id`` is
        not among the daemon's active sessions and owned by this deployment.

        Kubernetes counterpart of the Docker FR-005 orphan sweep. The
        selector requires BOTH ``managed-by=coordinare`` and the existence of
        a session-id label — the same controller-safety lesson
        ``cleanup_orphaned`` learned — and pods whose session id is in
        ``active_session_ids`` (in-flight sessions plus live documenting
        sides) are kept. Returns the swept Pod names; a failing list call
        yields an empty sweep rather than a crash.
        """
        # NOTE: `asyncio` is imported at module top; the kubernetes client
        # stays lazily imported like every other method here, but this sweep
        # is best-effort by contract, so a failing list call of ANY kind
        # yields an empty sweep rather than a crash.
        if not self._owner:
            _log.warning("kubernetes_runtime.session_sweep_skipped_no_owner")
            return []
        selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{SESSION_ID_LABEL},{OWNER_LABEL}={self._owner}"
        try:
            pods = await asyncio.to_thread(
                self._api.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=selector,
            )
        except Exception as exc:
            _log.warning(
                "kubernetes_runtime.session_sweep_list_failed", error=str(exc),
            )
            return []

        active = set(active_session_ids)
        swept: list[str] = []
        for pod in pods.items:
            labels = getattr(pod.metadata, "labels", None) or {}
            if labels.get(OWNER_LABEL) != self._owner:
                continue
            session_id = labels.get(SESSION_ID_LABEL)
            if session_id and session_id in active:
                continue
            await self.stop(pod.metadata.name)
            swept.append(pod.metadata.name)
        if swept:
            _log.info(
                "kubernetes_runtime.session_swept",
                count=len(swept),
                pods=swept,
            )
        return swept
