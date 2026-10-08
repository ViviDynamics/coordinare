"""Spec 146 / issue #200 — Kubernetes transport.

  US1  an operator runs coordinare without handing it the host
  US2  it runs on whatever cluster the operator already has
  US3  a maintainer can add another runtime without surgery

Issue #200 described this work as "implement the 9-line ``KubernetesTransport``
stub". That was wrong in a way worth recording: the stub implemented
``transport.base.AgentTransport``, which is the *subprocess wire protocol*
(``send(message) -> response``) that HTTP performers never use. The code that
actually starts a containerised performer imported ``performer_lifecycle``
directly. There was no seam to implement against, so this spec builds one.
"""

from __future__ import annotations

import inspect

import pytest

from coordinare.services.performer_runtime import PerformerRuntime, StartedPerformer

# ---------------------------------------------------------------------------
# US3 — the seam
# ---------------------------------------------------------------------------


def test_docker_runtime_satisfies_the_protocol() -> None:
    """FR-001, FR-002 — structurally, not by inheritance.

    ``PerformerRuntime`` is a Protocol so a runtime need not import it to satisfy
    it. That matters because it keeps the Docker code free of a dependency on an
    abstraction introduced for Kubernetes' benefit.
    """
    from coordinare.services.docker_runtime import DockerRuntime

    runtime = DockerRuntime()
    assert isinstance(runtime, PerformerRuntime), (
        "DockerRuntime must satisfy the Protocol structurally, without inheriting it"
    )
    assert PerformerRuntime not in type(runtime).__mro__, (
        "it must satisfy the Protocol WITHOUT inheriting, so the Docker code carries "
        "no dependency on an abstraction introduced for Kubernetes' benefit"
    )

    for method in ("start_ephemeral", "stop", "cleanup_orphaned", "tail_logs"):
        assert inspect.iscoroutinefunction(getattr(runtime, method)), (
            f"{method} must be async; the orchestration path awaits it"
        )


def test_protocol_covers_only_what_actually_varies_by_runtime() -> None:
    """FR-001 — ``wait_ready`` is deliberately absent.

    It polls ``GET /status`` over HTTP and is already runtime-agnostic, so it
    stays a shared free function. Widening the Protocol to include it would make
    every implementation carry a copy of identical code, and the Kubernetes
    runtime would have nothing different to put there.

    ``tail_logs`` is the counter-example and earns its place: reading a running
    performer's logs is ``docker logs`` against a container id in one runtime and
    the Pod log endpoint against a Pod name in the other. It was added because
    calling ``docker logs`` directly at the call site did exactly what this
    Protocol exists to prevent — worked on Docker, failed silently on Kubernetes.
    """
    methods = {name for name in dir(PerformerRuntime) if not name.startswith("_")}
    assert methods == {"start_ephemeral", "stop", "cleanup_orphaned", "tail_logs"}, (
        f"Protocol surface drifted to {sorted(methods)}. If a further operation is "
        "genuinely runtime-specific, add it deliberately; if it is not, it belongs "
        "as a shared function."
    )

    from coordinare.services import performer_lifecycle

    assert hasattr(performer_lifecycle, "wait_ready"), (
        "wait_ready must remain available as a shared, runtime-agnostic function"
    )


def test_started_performer_is_not_container_shaped() -> None:
    """FR-001 — the returned type must not presuppose Docker.

    The endpoint is the only thing downstream consumes, which is precisely why a
    second runtime needs no changes to the HTTP client.
    """
    started = StartedPerformer(handle="abc123", endpoint="http://10.244.0.5:8088")
    assert started.handle == "abc123"
    assert started.endpoint == "http://10.244.0.5:8088"

    fields = set(StartedPerformer.__dataclass_fields__)
    assert fields == {"handle", "endpoint"}, (
        f"StartedPerformer grew {fields - {'handle', 'endpoint'}}; anything runtime-"
        "specific belongs behind the opaque handle, not in the shared shape"
    )
    assert not any("container" in f for f in fields)

    with pytest.raises((AttributeError, TypeError)):
        started.handle = "mutated"  # type: ignore[misc]


def test_docker_runtime_wraps_rather_than_reimplements() -> None:
    """FR-002 — the evidence that the Docker path cannot have regressed.

    ``performer_lifecycle`` is imported directly by ``bench/runner.py``,
    ``__main__.py`` and five test modules. It stays; the runtime delegates to it.
    Code that is not touched cannot change behaviour, which is what makes the
    "existing tests pass unmodified" requirement cheap to honour.
    """
    import coordinare.services.docker_runtime as dr

    source = inspect.getsource(dr)
    assert "performer_lifecycle.start_ephemeral" in source
    assert "performer_lifecycle.stop" in source
    assert "performer_lifecycle.cleanup_orphaned_containers" in source

    # No docker CLI invocation of its own: that would be a reimplementation.
    assert '"run"' not in source and "_run_docker" not in source, (
        "docker_runtime must delegate, not build its own docker commands"
    )


# ---------------------------------------------------------------------------
# US1 — the Kubernetes runtime
# ---------------------------------------------------------------------------
#
# The manifest, name mapping, failure classification and endpoint derivation are
# pure functions on purpose: they carry the logic most likely to be wrong, and
# none of it should require a cluster to test.


def _config(**over):
    """A minimal PerformerEndpointConfig for manifest tests."""
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

    # mode="ephemeral" is what a containerised performer is; the model rejects
    # `image` on a subprocess performer, which is the correct constraint.
    base = {
        "id": "impl-1",
        "mode": "ephemeral",
        "roles": ["implementer"],
        "image": "coordinare-performer:full",
    }
    base.update(over)
    return PerformerEndpointConfig(**base)


class TestPodName:
    """FR-010 — performer identifiers become valid Pod names.

    Docker accepts names Kubernetes rejects. A Pod name is an RFC 1123 subdomain:
    lowercase alphanumerics, '-' and '.', starting and ending alphanumeric, at
    most 253 characters — and 63 for the label components coordinare uses.
    """

    @staticmethod
    def _valid(name: str) -> bool:
        import re

        return bool(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name)) and len(name) <= 63

    @pytest.mark.parametrize(
        "raw",
        [
            "impl-1",
            "Implementer_1",  # uppercase + underscore: both illegal in k8s
            "card#42/reviewer",  # punctuation Docker tolerates
            "a" * 200,  # over length
            "..leading.dots..",  # illegal start/end
            "MiXeD_Case-123",
            "9-starts-with-digit",  # legal: digits may start
            "trailing-hyphen-",
        ],
    )
    def test_produces_a_valid_pod_name(self, raw: str) -> None:
        from coordinare.services.kubernetes_runtime import pod_name_for

        name = pod_name_for(raw)
        assert self._valid(name), f"{raw!r} produced invalid Pod name {name!r}"

    def test_distinct_inputs_do_not_collide(self) -> None:
        """Truncation without disambiguation is how two performers share a Pod."""
        from coordinare.services.kubernetes_runtime import pod_name_for

        long_a = "performer-" + "a" * 200 + "-one"
        long_b = "performer-" + "a" * 200 + "-two"
        assert pod_name_for(long_a) != pod_name_for(long_b), (
            "names that differ only past the truncation point must still differ; "
            "otherwise two performers collide on one Pod"
        )

    def test_is_deterministic(self) -> None:
        """The same performer must map to the same Pod name across calls."""
        from coordinare.services.kubernetes_runtime import pod_name_for

        assert pod_name_for("impl-1") == pod_name_for("impl-1")

    def test_remains_traceable_to_the_performer(self) -> None:
        """An operator running `kubectl get pods` must recognise what they see."""
        from coordinare.services.kubernetes_runtime import pod_name_for

        assert "impl-1" in pod_name_for("impl-1")


class TestPodManifest:
    """FR-005, FR-006, FR-008, FR-013 — what gets created."""

    def test_is_a_bare_pod_not_a_job(self) -> None:
        """Research: a Job would restart the performer coordinare just killed.

        The performer is a long-running server that coordinare terminates. Under a
        Job the container never exits 0, the termination reads as failure, and
        backoff spawns a second performer for work coordinare believes is over.
        """
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        m = build_pod_manifest(_config(), pod_name="p-1", performer_id="impl-1")
        assert m["kind"] == "Pod"
        assert m["apiVersion"] == "v1", "core API only (FR-011)"
        assert m["spec"]["restartPolicy"] == "Never", (
            "a restart would resurrect a performer coordinare has finished with"
        )

    def test_uses_the_configured_image_and_reuses_the_node_cache(self) -> None:
        """FR-013 — IfNotPresent, so a node pulls the 4 GB image once, not per performer."""
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        c = build_pod_manifest(
            _config(image="myrepo/performer:1.2.3"), pod_name="p-1", performer_id="i",
        )["spec"]["containers"][0]
        assert c["image"] == "myrepo/performer:1.2.3"
        assert c["imagePullPolicy"] == "IfNotPresent"

    def test_exposes_the_performer_port(self) -> None:
        from coordinare.services.kubernetes_runtime import PERFORMER_PORT, build_pod_manifest

        c = build_pod_manifest(_config(), pod_name="p-1", performer_id="i")["spec"]["containers"][0]
        assert {"containerPort": PERFORMER_PORT} in c["ports"]

    def test_labels_tie_the_pod_back_to_its_performer(self) -> None:
        """Needed by cleanup_orphaned, and by an operator reading the cluster."""
        from coordinare.services.kubernetes_runtime import PERFORMER_ID_LABEL, build_pod_manifest

        labels = build_pod_manifest(_config(), pod_name="p-1", performer_id="impl-1")["metadata"][
            "labels"
        ]
        assert labels[PERFORMER_ID_LABEL] == "impl-1"

    def test_076_coordinare_labels_are_valid_k8s_label_values(self) -> None:
        """Cluster E2E regression (2026-09-26): the 076 coordinare.* label set
        rides both the Docker and K8s runtimes, and the K8s API 422s a Pod
        whose label value carries ':' or '+' — which the old ISO8601
        daemon_started_at value did. Every value the dispatch site
        (http_performer_service.dispatch_card) builds must satisfy the K8s
        label-value rules once build_pod_manifest merges them into the
        manifest.
        """
        import re
        from uuid import uuid4

        from coordinare.daemon import get_daemon_started_at
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        extra_labels = {
            "coordinare.session_id": str(uuid4()),
            "coordinare.daemon_started_at": get_daemon_started_at(),
            "coordinare.spec_version": "076",
            "coordinare.card_id": "PVTI_lADOB-V2yy4ABc0Wzbae",
            "coordinare.performer_stage": "implementing",
        }
        labels = build_pod_manifest(
            _config(), pod_name="p-1", performer_id="impl-1", extra_labels=extra_labels,
        )["metadata"]["labels"]
        for key, value in labels.items():
            assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", value), (
                f"Pod label {key!r} value {value!r} is not a valid Kubernetes label value"
            )
            assert value[-1].isalnum(), f"Pod label {key!r} value {value!r} must end alphanumeric"

    def test_requests_no_capabilities(self) -> None:
        """FR-014 — egress control is not offered on Kubernetes.

        The Docker path adds NET_ADMIN to run in-container iptables. Porting that
        would require a capability the restricted Pod Security Standard forbids,
        and the Kubernetes-native answer (NetworkPolicy) needs a CNI that
        enforces it — which minikube and microk8s defaults often do not. Claiming
        egress control that silently does not apply is worse than not offering it.
        """
        import json

        from coordinare.services.kubernetes_runtime import build_pod_manifest

        m = build_pod_manifest(_config(), pod_name="p-1", performer_id="i")
        blob = json.dumps(m)
        assert "NET_ADMIN" not in blob
        assert "privileged" not in blob
        c = m["spec"]["containers"][0]
        assert "add" not in (c.get("securityContext") or {}).get("capabilities", {})

    def test_requests_no_volume_when_caching_is_unavailable(self) -> None:
        """FR-012 — a cluster with no StorageClass must still run performers.

        Requesting a PVC unconditionally fails on exactly the small clusters this
        is meant to support.
        """
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        m = build_pod_manifest(_config(), pod_name="p-1", performer_id="i", cache_claim=None)
        assert not m["spec"].get("volumes"), "no cache means no volume, not a failure"

    def test_mounts_the_cache_when_one_is_available(self) -> None:
        """159 narrowed this: the claim is mounted for the symphony whose cache
        this performer was given, not wholesale whenever a claim exists. A
        performer with no env cache now gets no mount rather than the claim root,
        which is what stops every performer seeing every symphony's cache on a
        shared NFS volume. See TestTheCacheMountMirrorsTheDockerPath."""
        from coordinare.models.performer_endpoint import VolumeMount
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        m = build_pod_manifest(
            _config(
                volumes=[
                    VolumeMount(
                        host_path="/var/lib/coordinare/devenv/website",
                        container_path="/devenv/website",
                        mode="ro",
                    ),
                ],
            ),
            pod_name="p-1",
            performer_id="i",
            cache_claim="devenv-cache",
        )
        vols = m["spec"]["volumes"]
        assert any(
            v.get("persistentVolumeClaim", {}).get("claimName") == "devenv-cache" for v in vols
        )

    def test_uses_only_core_apis(self) -> None:
        """FR-011 — no CRDs, no vendor extensions, so it runs on any conformant cluster."""
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        m = build_pod_manifest(_config(), pod_name="p-1", performer_id="i")
        assert m["apiVersion"] == "v1"
        assert "/" not in m["apiVersion"], "a group in the apiVersion means not core"


class TestFailureClassification:
    """FR-009 — Pod failures map onto existing error handling, no new states."""

    @pytest.mark.parametrize(
        "reason",
        [
            "Evicted",
            "OOMKilled",
            "NodeLost",
            "ImagePullBackOff",
            "ErrImagePull",
            "CreateContainerError",
        ],
    )
    def test_known_pod_failures_classify_as_existing_errors(self, reason: str) -> None:
        from coordinare.services import performer_lifecycle
        from coordinare.services.kubernetes_runtime import classify_pod_failure

        err = classify_pod_failure(reason, message=f"pod failed: {reason}")
        assert isinstance(err, performer_lifecycle.LifecycleError), (
            f"{reason} must classify into the existing error hierarchy so the "
            "daemon's handling is unchanged; a new exception type would mean a "
            "new terminal state (FR-009)"
        )

    def test_the_reason_survives_into_the_message(self) -> None:
        """An operator debugging an evicted performer needs to know it was evicted."""
        from coordinare.services.kubernetes_runtime import classify_pod_failure

        assert "OOMKilled" in str(classify_pod_failure("OOMKilled", message="out of memory"))

    def test_an_unknown_reason_still_classifies(self) -> None:
        """Kubernetes adds reasons over time; an unseen one must not escape as a bare Exception."""
        from coordinare.services import performer_lifecycle
        from coordinare.services.kubernetes_runtime import classify_pod_failure

        err = classify_pod_failure("SomeFutureReason", message="?")
        assert isinstance(err, performer_lifecycle.LifecycleError)


class TestEndpoint:
    """The spike proved a pod IP on this port is reachable from another pod."""

    def test_endpoint_is_derived_from_pod_ip(self) -> None:
        from coordinare.services.kubernetes_runtime import PERFORMER_PORT, endpoint_for

        assert endpoint_for("10.244.0.5") == f"http://10.244.0.5:{PERFORMER_PORT}"

    def test_ipv6_pod_ip_is_bracketed(self) -> None:
        """A bare IPv6 host in a URL is not parseable; it must be bracketed."""
        from coordinare.services.kubernetes_runtime import endpoint_for

        assert endpoint_for("fd00::1").startswith("http://[fd00::1]:")


# ---------------------------------------------------------------------------
# US2 — portability and permissions
# ---------------------------------------------------------------------------

RBAC_MANIFEST = "deploy/kubernetes/rbac.yaml"


def _rbac_docs():
    import pathlib

    import yaml

    text = (pathlib.Path(__file__).resolve().parents[2] / RBAC_MANIFEST).read_text()
    return [d for d in yaml.safe_load_all(text) if d]


class TestPortability:
    """FR-011 — must run on EKS, vanilla, microk8s, minikube, k3s."""

    def test_manifest_uses_no_custom_resources(self) -> None:
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        m = build_pod_manifest(_config(), pod_name="p", performer_id="i")
        assert m["apiVersion"] == "v1"

    def test_rbac_uses_only_stable_core_and_rbac_groups(self) -> None:
        for doc in _rbac_docs():
            assert doc["apiVersion"] in {"v1", "rbac.authorization.k8s.io/v1"}, (
                f"{doc['kind']} uses {doc['apiVersion']}; anything else risks not "
                "existing on some conformant cluster"
            )

    def test_no_cluster_scoped_permissions(self) -> None:
        """FR-017 — a namespaced Role, never a ClusterRole."""
        kinds = {d["kind"] for d in _rbac_docs()}
        assert "ClusterRole" not in kinds and "ClusterRoleBinding" not in kinds
        assert kinds == {"ServiceAccount", "Role", "RoleBinding"}

    def test_every_granted_verb_is_actually_called_by_the_transport(self) -> None:
        """No verb may be granted that no code path exercises.

        The Role's own comment claims every verb is exercised, and a comment
        cannot enforce that. This shipped granting ``watch`` while readiness was
        polled with ``get`` and the orphan sweep used ``list`` — nothing watched
        anything. An unused grant is a real defect and not a tidiness issue: the
        whole security claim for the Kubernetes path is that the daemon holds a
        minimal Role, and "it might be useful later" is how a minimal Role stops
        being one.
        """
        from pathlib import Path

        source = Path("src/coordinare/services/kubernetes_runtime.py").read_text()

        # The client call that would exercise each verb, per resource.
        calls = {
            ("pods", "create"): "create_namespaced_pod",
            ("pods", "get"): "read_namespaced_pod",
            ("pods", "list"): "list_namespaced_pod",
            ("pods", "delete"): "delete_namespaced_pod",
            ("pods", "watch"): "watch",
            ("pods", "patch"): "patch_namespaced_pod",
            ("pods", "update"): "replace_namespaced_pod",
            ("pods/log", "get"): "read_namespaced_pod_log",
        }

        role = next(d for d in _rbac_docs() if d["kind"] == "Role")
        for rule in role["rules"]:
            for resource in rule["resources"]:
                for verb in rule["verbs"]:
                    needle = calls.get((resource, verb))
                    assert needle is not None, (
                        f"the Role grants {resource}/{verb}, which this test does not know "
                        "how to trace to a call — add it to the mapping or drop the grant"
                    )
                    assert needle in source, (
                        f"the Role grants {resource}/{verb} but kubernetes_runtime.py never "
                        f"calls {needle!r}. Remove the grant, or the Role is over-broad and "
                        "the minimal-permissions claim is untrue."
                    )

    def test_rbac_grants_exactly_what_the_transport_needs(self) -> None:
        """FR-015, FR-016 — sufficient, and no more.

        The second half is the point. A Role that works because it grants
        everything satisfies "the transport works under these permissions" while
        defeating the reason the requirement exists.
        """
        role = next(d for d in _rbac_docs() if d["kind"] == "Role")
        granted = {
            (r["apiGroups"][0], res, verb)
            for r in role["rules"]
            for res in r["resources"]
            for verb in r["verbs"]
        }

        for verb in ("create", "get", "list", "delete"):
            assert ("", "pods", verb) in granted, f"transport needs pods/{verb}"
        assert ("", "pods/log", "get") in granted, "logs must be readable before delete"

        resources = {res for _, res, _ in granted}
        assert resources == {"pods", "pods/log"}, (
            f"Role grants {resources - {'pods', 'pods/log'}} beyond what the transport "
            "uses; the manifest is the security boundary, so extra grants are the defect"
        )
        assert "secrets" not in resources


class TestNoEgressClaim:
    """FR-014 — do not claim egress control that does not apply.

    NetworkPolicy needs a CNI that enforces it; kind, minikube and microk8s
    defaults often do not. A policy that silently does nothing is worse than an
    honest absence, because an operator would believe they were protected.
    """

    def test_no_network_policy_is_shipped(self) -> None:
        kinds = {d["kind"] for d in _rbac_docs()}
        assert "NetworkPolicy" not in kinds

    def test_the_runtime_documents_the_absence(self) -> None:
        import coordinare.services.kubernetes_runtime as kr

        assert "egress" in (kr.__doc__ or "").lower(), (
            "the module must say egress control is unavailable here, or an operator "
            "migrating from Docker will assume their allowlist still applies"
        )


class TestRuntimeBehaviour:
    """Behaviour of ``KubernetesRuntime`` itself, against a fake API.

    The rest of this module tests pure functions, which left the class's own
    error paths unexercised — and that is exactly where an adversarial review
    found a leaked Pod and a swallowed API error. These cover the paths that
    only run when something goes wrong.
    """

    @staticmethod
    def _config(**overrides):
        from types import SimpleNamespace

        base = {
            "id": "impl",
            "image": "coordinare-performer:base",
            "env": {},
            "readiness_timeout_s": 1,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_a_pod_that_never_becomes_ready_is_deleted_not_leaked(self) -> None:
        """A created Pod must not outlive a failed start.

        ``pod_name_for`` is deterministic, so a leaked Pod does not merely waste
        cluster resources — it makes the *next* start of that performer fail with
        "already exists". An unschedulable image would turn into a permanently
        unstartable performer, and the second failure would look unrelated to the
        first.
        """
        import asyncio

        from coordinare.services import performer_lifecycle
        from coordinare.services.kubernetes_runtime import KubernetesRuntime

        deleted: list[str] = []

        class FakeApi:
            def create_namespaced_pod(self, *, namespace, body):
                return body

            def read_namespaced_pod(self, *, name, namespace):
                from types import SimpleNamespace

                # Pending forever: never gets an IP, never reaches a terminal phase.
                return SimpleNamespace(
                    status=SimpleNamespace(
                        phase="Pending", pod_ip=None, reason="", container_statuses=[],
                    ),
                )

            def delete_namespaced_pod(self, *, name, namespace, **kwargs):
                deleted.append(name)
                from kubernetes.client.rest import ApiException

                raise ApiException(status=404)

        runtime = KubernetesRuntime(core_v1=FakeApi())
        with pytest.raises(performer_lifecycle.ReadinessTimeoutError):
            asyncio.run(runtime.start_ephemeral(self._config()))

        assert deleted, "a Pod created for a start that failed must be deleted, not leaked"

    def test_a_failed_liveness_query_is_not_read_as_a_deleted_pod(self) -> None:
        """Only a 404 proves the Pod is gone.

        Returning on any other status confuses "the question failed" with "the
        answer is no". The cost lands later and elsewhere: ``stop`` reports
        success, and the next start of the same performer collides with a Pod
        that is still terminating.
        """
        import asyncio

        from kubernetes.client.rest import ApiException

        from coordinare.services.kubernetes_runtime import KubernetesRuntime

        attempts = {"n": 0}

        class FakeApi:
            def read_namespaced_pod(self, *, name, namespace):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise ApiException(status=500)  # transient: must be retried
                raise ApiException(status=404)  # now genuinely gone

        runtime = KubernetesRuntime(core_v1=FakeApi())
        asyncio.run(runtime._await_pod_gone("p", timeout_s=5))

        assert attempts["n"] >= 3, (
            "a 500 was treated as proof the Pod was gone; only a 404 means deleted"
        )

    def test_tail_logs_returns_empty_rather_than_raising(self) -> None:
        """A Pod too young to have a log endpoint is normal, not a fault."""
        import asyncio

        from kubernetes.client.rest import ApiException

        from coordinare.services.kubernetes_runtime import KubernetesRuntime

        class FakeApi:
            def read_namespaced_pod_log(self, *, name, namespace, tail_lines):
                raise ApiException(status=400)

        runtime = KubernetesRuntime(core_v1=FakeApi())
        assert asyncio.run(runtime.tail_logs("p")) == []

    def test_tail_logs_reads_the_pod_log_endpoint(self) -> None:
        import asyncio

        from coordinare.services.kubernetes_runtime import KubernetesRuntime

        class FakeApi:
            def read_namespaced_pod_log(self, *, name, namespace, tail_lines):
                return "one\ntwo\nthree"

        runtime = KubernetesRuntime(core_v1=FakeApi())
        assert asyncio.run(runtime.tail_logs("p")) == ["one", "two", "three"]


class TestThePodEnvCarriesTheBackend:
    """Issue #486 — the pod env must carry BACKEND for the image's entrypoint.

    ``agent/performer/entrypoint.sh`` installs the backend CLI at container start
    only when the pod env carries ``BACKEND``. The pod env comes exclusively from
    ``performer_endpoints[].env``, and the daemon sends the resolved backend
    (``performers.default.backend`` / role-specific) only in the job payload — so
    every first deployment on this transport started without a CLI, and the
    performer died with FileNotFoundError spawning it. The runtime derives
    ``BACKEND`` from the backend resolved for the dispatch it is starting, unless
    the operator already set it in ``performer_endpoints[].env``.
    """

    @staticmethod
    def _pod_env(**start_kwargs):
        import asyncio
        from types import SimpleNamespace

        from coordinare.services.kubernetes_runtime import KubernetesRuntime

        captured = {}

        class FakeApi:
            def create_namespaced_pod(self, *, namespace, body):
                captured["body"] = body
                return body

            def read_namespaced_pod(self, *, name, namespace):
                # Running with an IP on the first poll: a successful start.
                return SimpleNamespace(
                    status=SimpleNamespace(
                        phase="Running", pod_ip="10.244.0.5", reason="",
                        container_statuses=[],
                    ),
                )

        config = SimpleNamespace(
            id="impl",
            image="performer:full",
            env=start_kwargs.pop("env", {}),
            volumes=[],
            container_devenv_root="/devenv",
            readiness_timeout_s=1,
        )
        asyncio.run(
            KubernetesRuntime(core_v1=FakeApi()).start_ephemeral(config, **start_kwargs),
        )
        container = captured["body"]["spec"]["containers"][0]
        return {e["name"]: e["value"] for e in container.get("env", [])}

    def test_the_pod_env_gains_the_resolved_dispatch_backend(self) -> None:
        env = self._pod_env(env={}, backend="claude_code")
        assert env.get("BACKEND") == "claude_code", (
            "the entrypoint installs the CLI at container start from BACKEND; a "
            "pod without it starts no CLI and every dispatch fails with a "
            "FileNotFoundError spawning the agent"
        )

    def test_the_pod_env_carries_the_canonical_backend_spelling(self) -> None:
        # Copilot review round 1: config validation and get_backend both
        # normalize kebab-case, but entrypoint.sh matches only the underscored
        # canonical names, so BACKEND must not carry "claude-code".
        env = self._pod_env(env={}, backend="claude-code")
        assert env.get("BACKEND") == "claude_code", (
            "entrypoint.sh's case statement matches only underscored names; a "
            "kebab-case spelling would take the unknown-backend branch and "
            "start no CLI despite passing config validation"
        )

    def test_an_unresolved_placeholder_is_not_an_operator_backend(self) -> None:
        # Copilot review round 1: os.path.expandvars leaves unset ${...} in
        # config.yaml literal; that must not count as the operator's choice.
        env = self._pod_env(env={"BACKEND": "${BACKEND}"}, backend="claude_code")
        assert env.get("BACKEND") == "claude_code", (
            "a literal ${BACKEND} placeholder reaches the entrypoint as an "
            "unknown backend and starts no CLI; it must not block derivation"
        )

    def test_an_empty_operator_backend_does_not_block_derivation(self) -> None:
        # Copilot review round 2: the entrypoint's case "${BACKEND:-}" treats
        # empty exactly like unset, so an empty operator value must not
        # suppress the derivation.
        env = self._pod_env(env={"BACKEND": ""}, backend="claude_code")
        assert env.get("BACKEND") == "claude_code"

    def test_the_operator_env_wins_over_the_derivation(self) -> None:
        env = self._pod_env(env={"BACKEND": "opencode"}, backend="claude_code")
        assert env.get("BACKEND") == "opencode", (
            "performer_endpoints[].env is the operator's explicit choice; the "
            "derivation must only fill the gap"
        )

    def test_no_resolved_backend_sets_no_backend_var(self) -> None:
        env = self._pod_env(env={}, backend=None)
        assert "BACKEND" not in env, (
            "with nothing resolved there is nothing to derive; the container must "
            "look exactly as it does today"
        )


class TestPerformerHasNoClusterCredential:
    """A performer runs AI-generated code and must not hold an API token."""

    def test_service_account_token_is_not_mounted_into_performer_pods(self) -> None:
        """Kubernetes mounts a ServiceAccount token by default; that must be off.

        Left unset, model-authored code running in the performer finds a live
        cluster credential at a well-known path and can do whatever the
        namespace's default ServiceAccount can do. A performer never calls the
        Kubernetes API, so there is nothing to weigh against closing this.
        """
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        spec = build_pod_manifest(_config(), pod_name="p-1", performer_id="i")["spec"]
        assert spec["automountServiceAccountToken"] is False, (
            "performer Pods must not automount a ServiceAccount token: the code "
            "running inside them is model output, not coordinare's own"
        )

    def test_the_pod_asks_for_no_extra_privilege(self) -> None:
        """Nothing privileged, host-scoped, or capability-widening."""
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        spec = build_pod_manifest(_config(), pod_name="p-1", performer_id="i")["spec"]
        assert "hostNetwork" not in spec
        assert "hostPID" not in spec
        assert "hostIPC" not in spec
        for container in spec["containers"]:
            security = container.get("securityContext", {})
            assert not security.get("privileged")
            assert not security.get("allowPrivilegeEscalation")
            assert "NET_ADMIN" not in (security.get("capabilities", {}) or {}).get("add", [])


class TestTheSweepCannotDeleteTheController:
    """Issue #224 — coordinare deleted its own Pod, restarted, and did it again.

    ``cleanup_orphaned`` selected on ``managed-by=coordinare`` alone. The Helm
    chart labels the *controller* with exactly that, so the startup sweep matched
    the Pod it was running in. The daemon killed itself on every start, and the
    symptom gave nothing away: the log simply stopped, and the events said
    "Killing container" with no error anywhere.

    Nothing caught it because no test had ever started a real daemon on
    Kubernetes — spec 146's tests drive the runtime directly, and spec 147's
    smoke test stopped before startup completed.
    """

    @staticmethod
    def _selector(performer_id=None) -> str:
        import asyncio

        from coordinare.services.kubernetes_runtime import KubernetesRuntime

        captured = {}

        class FakeApi:
            def list_namespaced_pod(self, *, namespace, label_selector=None, **kwargs):
                from types import SimpleNamespace

                captured["selector"] = label_selector
                return SimpleNamespace(items=[])

        asyncio.run(KubernetesRuntime(core_v1=FakeApi(), owner="test-deployment").cleanup_orphaned(performer_id))
        return captured["selector"]

    def test_the_sweep_requires_a_performer_id_label(self) -> None:
        """The label the controller does not have, which is what excludes it."""
        from coordinare.services.kubernetes_runtime import PERFORMER_ID_LABEL

        assert PERFORMER_ID_LABEL in self._selector()

    def test_the_controller_pod_does_not_match_the_sweep(self) -> None:
        """Asserted against the chart's real labels, not a guess at them."""
        import shutil
        import subprocess

        import yaml

        # shutil.which FIRST. Checking the return code cannot help: when helm is
        # absent, subprocess.run raises FileNotFoundError before there is a code to
        # inspect — so the guard never fired and the job ERRORED rather than
        # skipping. The Test job has no helm; the Chart job does, and runs this.
        if shutil.which("helm") is None:
            pytest.skip("helm is not installed; the Chart CI job covers this assertion")

        rendered = subprocess.run(
            ["helm", "template", "t", "deploy/helm/coordinare", "-n", "ns"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if rendered.returncode != 0:
            pytest.skip(f"helm could not render the chart: {rendered.stderr.strip()[:200]}")

        from coordinare.services.kubernetes_runtime import PERFORMER_ID_LABEL

        controller_labels: dict[str, str] = {}
        for doc in yaml.safe_load_all(rendered.stdout):
            if doc and doc.get("kind") == "StatefulSet":
                controller_labels = doc["spec"]["template"]["metadata"]["labels"]

        assert controller_labels, "could not find the controller Pod's labels"
        assert PERFORMER_ID_LABEL not in controller_labels, (
            "the controller carries a performer-id label, so the orphan sweep would "
            "delete the Pod it is running in"
        )

    def test_narrowing_to_one_performer_still_scopes_by_managed_by(self) -> None:
        from coordinare.services.kubernetes_runtime import MANAGED_BY_LABEL

        selector = self._selector("impl")
        assert MANAGED_BY_LABEL in selector
        assert "impl" in selector


class TestTheDaemonCanStartWithoutASubprocessTransport:
    """Issue #224 — with agent_transport kubernetes, coordinare could never boot.

    ``_bootstrap_services`` builds an ``AgentTransport`` unconditionally and exits
    on failure. There is no subprocess wire protocol on the Kubernetes path and
    none is needed, since performers are Pods spoken to over HTTP — so every
    Kubernetes deployment died at startup.
    """

    def test_an_unavailable_transport_stands_in_at_startup(self) -> None:
        from coordinare.__main__ import _UnavailableTransport

        assert hasattr(_UnavailableTransport("why"), "send")

    def test_it_raises_something_the_callers_actually_catch(self) -> None:
        """A bare RuntimeError would sail past every handler built for this.

        ``TransportError`` subclasses ``RuntimeError``, and the relationship does
        not run the other way — so ``except TransportError`` in AgentService,
        dispatch_performer and http_performer_service does not catch a plain
        RuntimeError. The stand-in exists to fail *like a transport failure*, so
        it has to raise like one or it escapes the graph instead of being handled.
        """
        import asyncio

        from coordinare.__main__ import _UnavailableTransport
        from coordinare.transport.base import TransportError

        with pytest.raises(TransportError):
            asyncio.run(_UnavailableTransport("no transport here").send("anything"))

    def test_it_fails_only_when_something_actually_uses_it(self) -> None:
        """Deferred, not dropped.

        A role genuinely configured for a wire protocol still fails — at the point
        it needs one, where the message can say so — rather than taking down every
        deployment that has no such role.
        """
        import asyncio

        from coordinare.__main__ import _UnavailableTransport

        transport = _UnavailableTransport("no subprocess transport on this path")
        with pytest.raises(RuntimeError, match="no subprocess transport"):
            asyncio.run(transport.send("anything"))


class TestOnlyTheKubernetesCaseIsTolerated:
    """The narrowing that keeps the startup fix from hiding real misconfiguration.

    Letting startup proceed without a subprocess transport was first written as a
    broad ``except ValueError``. ``_build_transport`` raises ValueError in three
    places, so that also swallowed a missing ``agent_executable`` and, worse,
    ``Unknown transport`` — an operator who misspells ``agent_transport`` would
    have got a daemon that starts and quietly does nothing, instead of being told.

    The decision is now made on the transport *name* before the call, so nothing
    unrelated can be caught by it.
    """

    def test_a_misspelled_transport_still_raises(self) -> None:
        from types import SimpleNamespace

        from coordinare.__main__ import _build_transport

        config = SimpleNamespace(
            agent_transport="kubernets",
            agent_executable="",
            transport_timeout_seconds=30,
            github_token=None,
        )
        with pytest.raises(ValueError, match="Unknown transport"):
            _build_transport(config)

    def test_startup_decides_on_the_name_rather_than_catching(self) -> None:
        """Asserts on the code, ignoring comments.

        An earlier version of this test matched "except ValueError" inside the
        explanatory comment, so it could never have failed.
        """
        import inspect

        from coordinare import __main__ as main_module

        source = inspect.getsource(main_module._bootstrap_services)
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert 'config.agent_transport == "kubernetes"' in code
        assert "except ValueError" not in code, (
            "a broad ValueError catch here also swallows 'Unknown transport' and a "
            "missing agent_executable, which must still stop the daemon"
        )


class TestTheCacheMountMirrorsTheDockerPath:
    """Spec 159 US2 — one writer per symphony, enforced by the mount.

    The Docker path gives read-write to the bootstrap performer and read-only to
    consumers. This builder mounted one claim read-write for everybody, which is
    safe on a local disk with one performer and unsafe on a shared NFS claim with
    several.
    """

    @staticmethod
    def _config(volumes):
        from coordinare.models.performer_endpoint import PerformerEndpointConfig

        return PerformerEndpointConfig(
            id="perf-1", mode="ephemeral", roles=["implementer"],
            image="coordinare-performer:full", port=8080, volumes=volumes,
        )

    @staticmethod
    def _mount(sanitised, mode):
        from coordinare.models.performer_endpoint import VolumeMount

        return VolumeMount(
            host_path=f"/var/lib/coordinare/devenv/{sanitised}",
            container_path=f"/devenv/{sanitised}",
            mode=mode,
        )

    def _built(self, volumes, cache_claim="coordinare-devenv-cache"):
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        return build_pod_manifest(
            self._config(volumes), pod_name="p", performer_id="perf-1",
            cache_claim=cache_claim,
        )

    def test_a_bootstrap_mount_is_read_write_at_its_own_subpath(self) -> None:
        manifest = self._built([self._mount("website", "rw")])
        mounts = manifest["spec"]["containers"][0]["volumeMounts"]

        assert len(mounts) == 1
        assert mounts[0]["mountPath"] == "/devenv/website"
        assert mounts[0]["subPath"] == "website"
        assert not mounts[0].get("readOnly", False)

    def test_a_consumer_mount_is_read_only(self) -> None:
        mounts = self._built([self._mount("website", "ro")])["spec"]["containers"][0]["volumeMounts"]

        assert mounts[0]["readOnly"] is True, (
            "a consumer holding a read-write mount is how two performers end up "
            "writing one cache tree on NFS"
        )

    def test_one_symphony_cannot_reach_another_through_its_mount(self) -> None:
        mounts = self._built([self._mount("website", "ro")])["spec"]["containers"][0]["volumeMounts"]

        assert mounts[0]["subPath"] == "website"
        assert all(m["mountPath"] != "/devenv" for m in mounts), (
            "mounting the claim root exposes every symphony's cache to every performer"
        )

    def test_several_symphonies_each_get_their_own_subpath(self) -> None:
        mounts = self._built(
            [self._mount("website", "ro"), self._mount("inhouse", "ro")],
        )["spec"]["containers"][0]["volumeMounts"]

        assert {m["subPath"] for m in mounts} == {"website", "inhouse"}
        assert len({m["name"] for m in mounts}) == 1, "one claim, several subpaths"

    def test_the_claim_is_declared_once_however_many_mounts(self) -> None:
        manifest = self._built([self._mount("website", "ro"), self._mount("inhouse", "ro")])
        volumes = manifest["spec"]["volumes"]

        assert len(volumes) == 1
        assert volumes[0]["persistentVolumeClaim"]["claimName"] == "coordinare-devenv-cache"

    def test_no_claim_means_no_mounts_and_no_failure(self) -> None:
        """Spec 146 FR-012: a cluster with no cache runs cold, not broken."""
        manifest = self._built([self._mount("website", "ro")], cache_claim=None)

        assert "volumeMounts" not in manifest["spec"]["containers"][0]
        assert "volumes" not in manifest["spec"]

    def test_a_claim_with_no_volumes_mounts_nothing(self) -> None:
        """A performer with no env cache yet must not get the claim root."""
        manifest = self._built([])

        assert "volumeMounts" not in manifest["spec"]["containers"][0]
        assert "volumes" not in manifest["spec"]

    def test_a_workspace_volume_never_reaches_the_nfs_claim(self) -> None:
        """Spec FR-008. Git trees and node_modules on NFS is the worst case for
        many-small-files and locking, and the claim is not where they belong.
        Only devenv paths translate; anything else is ignored here."""
        from coordinare.models.performer_endpoint import VolumeMount

        workspace = VolumeMount(
            host_path="/var/lib/coordinare/work/card-1",
            container_path="/workspace",
            mode="rw",
        )
        manifest = self._built([workspace, self._mount("website", "ro")])
        mounts = manifest["spec"]["containers"][0]["volumeMounts"]

        assert [m["mountPath"] for m in mounts] == ["/devenv/website"], (
            "a non-devenv volume was mounted onto the shared NFS claim"
        )


class TestTheCacheMountCannotBeTalkedIntoTheWrongPlace:
    """Review of #159: two ways the translation could betray its own purpose.

    Both were reproduced against the committed code, and both put a performer
    somewhere it must not be — which is the whole thing this mount exists to
    prevent, not a hardening extra.
    """

    @staticmethod
    def _config(volumes, devenv_root="/devenv"):
        from coordinare.models.performer_endpoint import PerformerEndpointConfig

        return PerformerEndpointConfig(
            id="perf-1", mode="ephemeral", roles=["implementer"],
            image="coordinare-performer:full", port=8080,
            container_devenv_root=devenv_root, volumes=volumes,
        )

    @staticmethod
    def _vol(host, container, mode="ro"):
        from coordinare.models.performer_endpoint import VolumeMount

        return VolumeMount(host_path=host, container_path=container, mode=mode)

    def _built(self, volumes, devenv_root="/devenv"):
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        return build_pod_manifest(
            self._config(volumes, devenv_root), pod_name="p", performer_id="perf-1",
            cache_claim="coordinare-devenv-cache",
        )

    def test_a_root_of_slash_does_not_mount_the_whole_filesystem(self) -> None:
        """`"/".rstrip("/")` is the empty string, and everything startswith("").

        With that, the filter admits every volume the performer has, and each
        one is mounted onto the shared cache claim — so a secrets mount would be
        served out of the env cache.
        """
        mounts = self._built(
            [
                self._vol("/var/lib/coordinare/devenv/website", "/devenv/website"),
                self._vol("/etc/secrets", "/secrets"),
            ],
            devenv_root="/",
        )["spec"]["containers"][0].get("volumeMounts", [])

        assert "/secrets" not in [m["mountPath"] for m in mounts], (
            "a non-devenv volume was mounted onto the cache claim"
        )

    def test_two_symphonies_cannot_be_given_the_same_subpath(self) -> None:
        """Unconstructable now, rather than detected.

        The subPath used to come from `host_path.name` while the mount path came
        from `container_path` — two sources for one relationship. Cache
        directories sharing a basename then collided on a single subPath with
        the second mounted read-write, so that performer wrote into the other
        symphony's cache. Deriving both from the container path means two
        different mounts cannot share a subPath at all, which is why this asserts
        the property rather than an exception.
        """
        mounts = self._built(
            [
                self._vol("/var/lib/coordinare/devenv/website", "/devenv/website", "ro"),
                self._vol("/mnt/other/website", "/devenv/website-2", "rw"),
            ],
        )["spec"]["containers"][0]["volumeMounts"]

        by_path = {m["mountPath"]: m for m in mounts}
        assert by_path["/devenv/website"]["subPath"] == "website"
        assert by_path["/devenv/website-2"]["subPath"] == "website-2"
        assert len({m["subPath"] for m in mounts}) == len(mounts), (
            "two mounts share a subPath; one symphony is inside another's cache"
        )
        # And the writer is not pointed at the reader's directory.
        writer = next(m for m in mounts if not m["readOnly"])
        reader = next(m for m in mounts if m["readOnly"])
        assert writer["subPath"] != reader["subPath"]

    def test_the_subpath_matches_the_directory_the_mount_path_names(self) -> None:
        """Derived from one source, so the two cannot disagree."""
        mounts = self._built(
            [self._vol("/anywhere/at/all", "/devenv/website")],
        )["spec"]["containers"][0]["volumeMounts"]

        assert mounts[0]["subPath"] == "website"
        assert mounts[0]["mountPath"] == "/devenv/website"

    def test_a_path_that_merely_starts_with_the_root_is_not_under_it(self) -> None:
        """`/devenvil/x`.startswith(`/devenv`) is True and means nothing."""
        mounts = self._built(
            [self._vol("/x/y", "/devenvil/x"), self._vol("/a/b", "/devenv/website")],
        )["spec"]["containers"][0]["volumeMounts"]

        assert [m["mountPath"] for m in mounts] == ["/devenv/website"]

    def test_a_nested_path_under_the_root_is_refused(self) -> None:
        """The claim is laid out one directory per symphony. A deeper path has
        no subPath that means the same thing."""
        import pytest

        with pytest.raises(ValueError, match="subpath"):
            self._built([self._vol("/a/b", "/devenv/website/inner")])

    def test_the_root_itself_is_not_a_symphony(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="subpath"):
            self._built([self._vol("/a/b", "/devenv")])
