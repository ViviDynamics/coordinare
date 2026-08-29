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
            _config(image="myrepo/performer:1.2.3"), pod_name="p-1", performer_id="i"
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
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        m = build_pod_manifest(
            _config(), pod_name="p-1", performer_id="i", cache_claim="devenv-cache"
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
                        phase="Pending", pod_ip=None, reason="", container_statuses=[]
                    )
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
