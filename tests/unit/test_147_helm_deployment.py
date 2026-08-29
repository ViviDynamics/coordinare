"""Spec 147 / issue #201 — the Helm chart, verified by rendering it.

These tests shell out to ``helm template`` and assert on the parsed objects, the
same way spec 146's tests parse ``deploy/kubernetes/rbac.yaml``. Chart
verification therefore lives in the suite everyone already runs, rather than in a
separate tool with its own invocation that will be forgotten.

**Skipped, not failed, when helm is absent**, and the skip names the install
command — a silently-skipped check is how "the chart is verified" stays green
while quietly becoming untrue.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

CHART = "deploy/helm/coordinare"
RBAC_MANIFEST = "deploy/kubernetes/rbac.yaml"
RELEASE = "coordinare"
NAMESPACE = "coordinare-test"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm is not installed. Install it with: brew install helm",
)


def _render(*, release: str = RELEASE, namespace: str = NAMESPACE, **values) -> list[dict]:
    """Render the chart and return every object it produces.

    Values are passed as JSON through ``--set-json`` so nested keys, lists and
    booleans survive intact; ``--set`` would coerce them into strings.
    """
    args = [
        "helm",
        "template",
        release,
        CHART,
        "--namespace",
        namespace,
    ]
    for key, value in values.items():
        args += ["--set-json", f"{key}={json.dumps(value)}"]

    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RenderError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


class RenderError(Exception):
    """``helm template`` refused to render. The message is helm's own stderr."""


def _by_kind(objects: list[dict], kind: str) -> list[dict]:
    return [o for o in objects if o.get("kind") == kind]


def _one(objects: list[dict], kind: str) -> dict:
    matches = _by_kind(objects, kind)
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


def _container(objects: list[dict]) -> dict:
    return _one(objects, "StatefulSet")["spec"]["template"]["spec"]["containers"][0]


def _rendered_config(objects: list[dict]) -> dict:
    """The ``config.yaml`` the ConfigMap carries, parsed."""
    cm = _one(objects, "ConfigMap")
    return yaml.safe_load(cm["data"]["config.yaml"])


# ---------------------------------------------------------------------------
# FR-001, FR-002 — one replica, and it is a correctness constraint
# ---------------------------------------------------------------------------


class TestSingleReplica:
    def test_renders_a_single_replica_statefulset(self) -> None:
        """FR-001 — StatefulSet, not Deployment.

        The claim is bound to the workload's identity, so an upgrade keeps it and
        a reschedule reattaches the same volume. A Deployment's rolling update
        would also briefly run two Pods, and two coordinare processes on one state
        file is the corruption this chart exists to prevent.
        """
        objects = _render()
        sts = _one(objects, "StatefulSet")
        assert sts["spec"]["replicas"] == 1
        assert not _by_kind(objects, "Deployment"), "must not also ship a Deployment"

    def test_more_than_one_replica_fails_the_render(self) -> None:
        """FR-002 — refusing is the point.

        The issue asked only that >1 need an explicit ``--set``. That is too weak:
        ``--set replicaCount=3`` is exactly what an operator types when they assume
        coordinare scales horizontally, and nothing would have stopped them.
        """
        with pytest.raises(RenderError):
            _render(replicaCount=3)

    def test_the_refusal_explains_the_consequence(self) -> None:
        """A guard that says "not allowed" teaches nothing.

        The operator is about to do something reasonable-seeming, so the message
        has to carry the reason — this is the one moment they are guaranteed to
        read it, which a README cannot claim.
        """
        with pytest.raises(RenderError) as excinfo:
            _render(replicaCount=2)
        message = str(excinfo.value).lower()
        assert "state" in message, "the message must say what breaks"
        assert any(word in message for word in ("corrupt", "lose", "loss", "single")), (
            f"the message must name the data-loss consequence, got: {excinfo.value}"
        )

    def test_an_explicit_unsafe_override_is_honoured(self) -> None:
        """An escape hatch exists, and its name is the warning."""
        objects = _render(replicaCount=2, allowUnsafeMultiReplica=True)
        assert _one(objects, "StatefulSet")["spec"]["replicas"] == 2


# ---------------------------------------------------------------------------
# FR-003 — the state path (research R9: highest consequence, zero symptoms)
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_state_path_is_absolute_and_on_the_mounted_volume(self) -> None:
        """FR-003 — coordinare's default is *relative*.

        ``./coordinare.state.json`` resolves against the container's working
        directory. Left alone the daemon runs correctly, persists nothing durable,
        and loses everything on the first reschedule. There is no symptom until
        the moment it costs work, which is why it gets its own test.
        """
        objects = _render()
        config = _rendered_config(objects)
        state_path = str(config["state_file_path"])

        assert state_path.startswith("/"), (
            f"state_file_path {state_path!r} is relative; it would resolve onto the "
            "container filesystem and be lost on every reschedule"
        )

        mounts = _container(objects)["volumeMounts"]
        mount_paths = [m["mountPath"] for m in mounts]
        assert any(state_path.startswith(p.rstrip("/") + "/") for p in mount_paths), (
            f"state_file_path {state_path!r} is not under any mount: {mount_paths}"
        )

    def test_state_uses_a_volume_claim_template(self) -> None:
        """FR-004 — the claim belongs to the StatefulSet's identity, so upgrades keep it."""
        sts = _one(_render(), "StatefulSet")
        claims = sts["spec"].get("volumeClaimTemplates") or []
        assert claims, "state must come from a volumeClaimTemplate, not an emptyDir"

    def test_state_storage_class_can_be_named_for_a_cluster_with_no_default(self) -> None:
        """SC-008 — the controller's state volume is required, unlike the performer cache.

        On a cluster with storage but no *default* StorageClass, the operator must
        be able to name one, or the required claim never binds.
        """
        sts = _one(_render(**{"state.storageClass": "fast-ssd"}), "StatefulSet")
        claim = sts["spec"]["volumeClaimTemplates"][0]
        assert claim["spec"]["storageClassName"] == "fast-ssd"


# ---------------------------------------------------------------------------
# FR-006, FR-007, FR-008 — credentials
# ---------------------------------------------------------------------------


class TestSecretsStayInSecrets:
    SENTINEL = "ghp-SENTINEL-must-not-leak-000"

    def test_no_credential_value_appears_outside_a_secret(self) -> None:
        """FR-006 / SC-003 — a leaked token is worse than no deployment.

        Worse rather than merely bad: the operator believes they are secured. A
        values file is also the thing people commit to git.
        """
        objects = _render(**{"secrets.GITHUB_TOKEN": self.SENTINEL})
        for obj in objects:
            if obj.get("kind") == "Secret":
                continue
            assert self.SENTINEL not in yaml.safe_dump(obj), (
                f"the credential leaked into a {obj.get('kind')} named "
                f"{obj.get('metadata', {}).get('name')}"
            )

    def test_the_config_references_env_var_names_not_values(self) -> None:
        """FR-007 — use the indirection coordinare already has.

        ``config.py`` expands ``${VAR}`` over the whole config file at load time,
        which is how coordinare already separates config from credentials and how
        ``bin/start`` already works. The chart uses that seam rather than
        inventing a parallel one.
        """
        objects = _render(**{"secrets.GITHUB_TOKEN": self.SENTINEL})
        raw = _one(objects, "ConfigMap")["data"]["config.yaml"]
        assert "${GITHUB_TOKEN}" in raw, "config must reference the variable name"
        assert self.SENTINEL not in raw

    def test_the_container_receives_the_secret_by_reference(self) -> None:
        env_from = _container(_render(**{"secrets.GITHUB_TOKEN": self.SENTINEL})).get("envFrom", [])
        assert any("secretRef" in e for e in env_from), "credentials must arrive via envFrom"

    def test_an_existing_secret_replaces_the_charts_own(self) -> None:
        """FR-008 — for operators using External Secrets or Vault."""
        objects = _render(existingSecret="my-managed-secret")
        assert not _by_kind(objects, "Secret"), (
            "when existingSecret is set the chart must create no Secret of its own"
        )
        refs = [
            e["secretRef"]["name"]
            for e in _container(objects).get("envFrom", [])
            if "secretRef" in e
        ]
        assert "my-managed-secret" in refs


# ---------------------------------------------------------------------------
# FR-009, FR-010, FR-011 — permissions
# ---------------------------------------------------------------------------


class TestPermissions:
    @staticmethod
    def _verbs(rules: list[dict]) -> set[tuple[str, str]]:
        return {(res, verb) for r in rules for res in r["resources"] for verb in r["verbs"]}

    def test_chart_rbac_matches_the_standalone_manifest_exactly(self) -> None:
        """FR-009 — two copies will diverge, and the drifting one is unread.

        The chart cannot ``include`` a file outside its directory, so the
        duplication is real. Pinning them equal is what keeps it honest.
        """
        chart_role = _one(_render(), "Role")
        standalone = next(
            d
            for d in yaml.safe_load_all(Path(RBAC_MANIFEST).read_text())
            if d and d.get("kind") == "Role"
        )
        assert self._verbs(chart_role["rules"]) == self._verbs(standalone["rules"]), (
            "the chart's Role and deploy/kubernetes/rbac.yaml have drifted apart"
        )

    def test_watch_is_not_reintroduced(self) -> None:
        """Spec 146 removed it after finding nothing calls it.

        A grant nothing exercises makes the Role over-broad in exactly the way its
        own comment claims it is not, and the whole security claim for this path
        is that the daemon holds a minimal Role.
        """
        assert ("pods", "watch") not in self._verbs(_one(_render(), "Role")["rules"])

    def test_nothing_cluster_scoped_is_created(self) -> None:
        kinds = {o["kind"] for o in _render()}
        assert not (kinds & {"ClusterRole", "ClusterRoleBinding"}), (
            "namespace-scoped by design; a ClusterRole would break the security claim"
        )

    def test_performer_namespace_defaults_to_the_release_namespace(self) -> None:
        """FR-010 — defaulting to "default" would scatter Pods into an unrelated namespace."""
        config = _rendered_config(_render())
        assert config["kubernetes_namespace"] == NAMESPACE

    def test_performer_namespace_is_overridable(self) -> None:
        config = _rendered_config(_render(**{"performers.namespace": "perf"}))
        assert config["kubernetes_namespace"] == "perf"

    def test_the_controller_keeps_its_own_service_account_token(self) -> None:
        """FR-011 — the suppression in spec 146 is for *performer* Pods only.

        Performers run AI-generated code and must not hold a cluster credential.
        The controller *is* coordinare, and it cannot manage Pods without one.
        Copying the setting across would break the deployment on install.
        """
        pod_spec = _one(_render(), "StatefulSet")["spec"]["template"]["spec"]
        assert pod_spec.get("automountServiceAccountToken") is not False, (
            "the controller needs its token to call the Kubernetes API"
        )
        assert pod_spec.get("serviceAccountName"), "the controller must run as its ServiceAccount"


# ---------------------------------------------------------------------------
# FR-012, FR-013 — health and disclosure
# ---------------------------------------------------------------------------


class TestProbesAndMetrics:
    def test_liveness_and_readiness_use_the_real_endpoints(self) -> None:
        """FR-012 — /live and /ready exist and are read-only."""
        container = _container(_render())
        assert container["livenessProbe"]["httpGet"]["path"] == "/live"
        assert container["readinessProbe"]["httpGet"]["path"] == "/ready"

    def test_metrics_is_not_exposed_by_default(self) -> None:
        """FR-013 — /metrics discloses card counts, model identifiers and error rates."""
        objects = _render()
        for service in _by_kind(objects, "Service"):
            for port in service["spec"]["ports"]:
                assert port.get("name") != "metrics", (
                    "the metrics port must not be exposed by default"
                )


# ---------------------------------------------------------------------------
# FR-014, FR-015, FR-016 — the dashboard, and the widening it costs
# ---------------------------------------------------------------------------


class TestDashboardExposure:
    def test_no_ingress_by_default(self) -> None:
        """FR-014 — the dashboard is unauthenticated until spec 143."""
        assert not _by_kind(_render(), "Ingress")

    def test_a_cluster_ip_service_is_created(self) -> None:
        service = _one(_render(), "Service")
        assert service["spec"]["type"] == "ClusterIP"

    def test_the_service_dns_name_is_trusted_so_the_service_works(self) -> None:
        """FR-015 — without this the Service would 403 on every request.

        Spec 144's guard rejects any request whose Host is not loopback, so
        shipping a Service without the matching trusted-host entry would ship a
        resource that is broken on arrival.
        """
        config = _rendered_config(_render())
        trusted = config["trusted_dashboard_hosts"]
        assert any(NAMESPACE in host and RELEASE in host for host in trusted), (
            f"the Service's own DNS name must be trusted, got {trusted}"
        )

    def test_the_widening_is_narrow(self) -> None:
        """FR-015 — a wildcard would hand the guard away entirely.

        This entry widens a control spec 144 deliberately left opt-in. Narrowness
        is the condition on which that is acceptable.
        """
        trusted = _rendered_config(_render())["trusted_dashboard_hosts"]
        for host in trusted:
            assert "*" not in host, f"wildcard trusted host {host!r} defeats the guard"
            assert host not in ("0.0.0.0", "::"), f"bind-all address {host!r} in trusted hosts"

    def test_the_trusted_host_is_visible_in_values_not_only_in_a_template(self) -> None:
        """FR-016 — an operator must not have to read templates to find this.

        ``helm show values`` is where people look. A security concession made on
        their behalf belongs where they will actually see it.
        """
        values = yaml.safe_load(Path(CHART, "values.yaml").read_text())
        assert "trustedHosts" in values.get("dashboard", {}), (
            "dashboard.trustedHosts must appear in values.yaml"
        )

    def test_the_dashboard_binds_beyond_loopback_inside_the_pod(self) -> None:
        """A loopback bind is unreachable from a Service.

        The guard, not the bind address, is what constrains access here — which is
        exactly why the guard's entry has to be right.
        """
        assert _rendered_config(_render())["dashboard_host"] == "0.0.0.0"


# ---------------------------------------------------------------------------
# FR-005 — the image
# ---------------------------------------------------------------------------


class TestImagePinning:
    def test_the_image_tag_is_pinned(self) -> None:
        """FR-005 — a floating tag means a reschedule can change the running version."""
        image = _container(_render())["image"]
        assert image.count(":") >= 1, f"image {image!r} carries no tag"
        tag = image.rsplit(":", 1)[-1]
        assert tag and tag != "latest", f"image tag must be pinned, got {tag!r}"

    def test_image_pull_secrets_reach_performers(self) -> None:
        config = _rendered_config(_render(**{"performers.imagePullSecrets": ["regcred"]}))
        assert config["kubernetes_image_pull_secrets"] == ["regcred"]


# ---------------------------------------------------------------------------
# Contract: "optional means optional"
# ---------------------------------------------------------------------------


class TestOptionalPerformerCache:
    def test_renders_without_a_performer_cache_claim(self) -> None:
        """SC-008 — a chart demanding a PVC nobody can provision fails on exactly
        the small clusters this is meant to support."""
        config = _rendered_config(_render())
        assert not config.get("kubernetes_cache_claim"), (
            "the performer cache must be absent by default, not a required claim"
        )

    def test_a_cache_claim_is_passed_through_when_given(self) -> None:
        config = _rendered_config(_render(**{"performers.cacheClaim": "devenv-cache"}))
        assert config["kubernetes_cache_claim"] == "devenv-cache"


# ---------------------------------------------------------------------------
# FR-018, FR-019, FR-020 — the daemon image and its build
# ---------------------------------------------------------------------------


WORKFLOW = ".github/workflows/main-branch-build.yml"


class TestDaemonImageBuild:
    """The chart had nothing real to install before this.

    ``Dockerfile.daemon`` existed since spec 065 but no workflow ever referenced
    it, so no coordinare daemon image has ever been published.
    """

    @staticmethod
    def _workflow() -> dict:
        return yaml.safe_load(Path(WORKFLOW).read_text())

    def test_the_daemon_image_is_built_and_pushed(self) -> None:
        """FR-018 — a chart pointing at an image nobody publishes is not shippable."""
        jobs = self._workflow()["jobs"]
        assert "build-daemon" in jobs
        body = yaml.safe_dump(jobs["build-daemon"])
        assert "Dockerfile.daemon" in body
        assert "docker push" in body

    def test_it_follows_the_existing_image_jobs_rather_than_inventing_a_pattern(self) -> None:
        """Same registry, same version source, same runner as build-base/build-full."""
        jobs = self._workflow()["jobs"]
        daemon, base = jobs["build-daemon"], jobs["build-base"]
        assert daemon["needs"] == base["needs"], "version must come from the same job"
        assert daemon["runs-on"] == base["runs-on"]
        assert daemon["permissions"] == base["permissions"]

    def test_the_daemon_image_is_also_tagged_latest(self) -> None:
        jobs = self._workflow()["jobs"]
        assert "build-daemon" in jobs["tag-latest"]["needs"], (
            "tag-latest must wait for the daemon image, or it tags a version that "
            "may not have been pushed yet"
        )
        assert "registry-daemon" in yaml.safe_dump(jobs["tag-latest"])

    def test_the_chart_installs_the_image_this_workflow_publishes(self) -> None:
        """A chart and a pipeline that disagree on the image name fail at install.

        They are written in different files and nothing else would catch a
        mismatch until an operator's Pod sat in ImagePullBackOff.
        """
        published = Path("registry-daemon").read_text().strip()
        values = yaml.safe_load(Path(CHART, "values.yaml").read_text())
        assert values["image"]["repository"] == published


class TestChartReadmeIsHonest:
    """A security concession made on the operator's behalf must be legible.

    These assert on *substance*, not phrasing: that the README names the
    widening, and that it says plainly the dashboard has no authentication.
    Being true in the templates is not the same as being findable.
    """

    @staticmethod
    def _readme() -> str:
        return Path(CHART, "README.md").read_text().lower()

    def test_it_states_the_dashboard_is_unauthenticated(self) -> None:
        readme = self._readme()
        assert "no authentication" in readme or "unauthenticated" in readme

    def test_it_names_the_trusted_host_widening_the_chart_performs(self) -> None:
        """FR-016 — discoverable in prose, not only by reading a template."""
        readme = self._readme()
        assert "trustedhosts" in readme or "trusted-host" in readme or "trusted host" in readme
        assert "svc.cluster.local" in readme, (
            "the README must show the actual hostnames the chart trusts"
        )

    def test_it_explains_the_single_replica_constraint(self) -> None:
        readme = self._readme()
        assert "replica" in readme
        assert any(w in readme for w in ("corrupt", "races", "race")), (
            "the README must say why one replica, not merely that it is one"
        )

    def test_it_records_that_egress_control_is_unavailable(self) -> None:
        """Spec 146 chose to ship nothing rather than a policy that silently
        fails to apply. That choice is only safe if it is stated."""
        assert "networkpolicy" in self._readme()

    def test_it_says_the_state_claim_survives_uninstall(self) -> None:
        readme = self._readme()
        assert "uninstall" in readme and "survive" in readme


PR_WORKFLOW = ".github/workflows/pr-ci.yml"


class TestChartCIDoesNotDisturbTheExistingGate:
    """FR-020 / SC-010 — new checks must not silently change what gates a merge."""

    #: The jobs `Build Success` waits on. It is the required check, so this list is
    #: what actually gates a merge. Written out rather than diffed against
    #: origin/main: on a self-hosted runner that ref can be stale, and `git show`
    #: then succeeds with old content — a check comparing against fiction, which is
    #: worse than one that fails to run. Adding a job here should be a deliberate
    #: edit to this line, which is the moment of thought the test exists to create.
    GATING_JOBS: ClassVar[list[str]] = [
        "lint",
        "test",
        "coverage",
        "e2e",
        "performer",
        "docker",
        "benchmark",
    ]

    def test_build_success_gates_on_exactly_these_jobs(self) -> None:
        """New checks are visible, not gating, until they have proven steady.

        The kind-based install in particular has not earned it (research R7): an
        unreliable required check trains people to ignore red, which is worse than
        not having it.
        """
        build_success = self._workflow()["jobs"]["build-success"]
        assert build_success["needs"] == self.GATING_JOBS

    def test_the_new_checks_are_not_gating(self) -> None:
        for job in ("chart", "chart-install", "daemon-image"):
            assert job in self._workflow()["jobs"], f"{job} should exist"
            assert job not in self.GATING_JOBS, (
                f"{job} now gates merges; promote it deliberately in the branch "
                "ruleset once it has proven steady, not as a side effect"
            )

    @staticmethod
    def _workflow() -> dict:
        return yaml.safe_load(Path(PR_WORKFLOW).read_text())

    def test_the_chart_checks_exist(self) -> None:
        jobs = self._workflow()["jobs"]
        assert "chart" in jobs, "chart lint and render assertions must run in CI"
        assert "chart-install" in jobs, "the chart must be installed against a real cluster"

    def test_the_chart_job_installs_helm_rather_than_assuming_it(self) -> None:
        """Without helm the assertions skip, and a skip reads as a pass.

        This is the whole reason the chart checks are a separate job instead of
        steps added to Test: Test does not have helm, and adding the tests there
        would have produced a green run that verified nothing.
        """
        body = yaml.safe_dump(self._workflow()["jobs"]["chart"])
        assert "setup-helm" in body

    def test_the_kind_job_installs_its_own_tooling(self) -> None:
        """kind and helm are used by no other workflow, so nothing guarantees the
        self-hosted runner has them."""
        body = yaml.safe_dump(self._workflow()["jobs"]["chart-install"])
        assert "kind-action" in body and "setup-helm" in body


class TestTheChartsConfigActuallySatisfiesTheGuard:
    """Feed the chart's rendered config to the real guard and make requests of it.

    The earlier dashboard tests assert on the rendered *value* of
    ``trusted_dashboard_hosts``. That is not the same as proving the guard
    accepts a request to the Service, and the difference is where a real hole
    lived: the chart must set ``dashboard_host`` to ``0.0.0.0``, and the guard
    used to trust whatever that said — which admitted ``Host: 0.0.0.0``, a value
    browsers will happily send to a port-forwarded dashboard.

    Asserting on config values would never have caught it. Asking the guard does.
    """

    @staticmethod
    def _permitted_and_service(**values):
        """The guard as the chart configures it, and the Service it must admit.

        The Service name is taken from the rendered object rather than guessed:
        the chart collapses ``coordinare-coordinare`` to ``coordinare`` when the
        release is named after the chart, so a hardcoded expectation would test
        a hostname that is never served.
        """
        from coordinare.localhost_guard import build_permitted

        objects = _render(**values)
        config = _rendered_config(objects)
        service_name = _one(objects, "Service")["metadata"]["name"]
        permitted = build_permitted(
            dashboard_host=config["dashboard_host"],
            dashboard_port=int(config["dashboard_port"]),
            trusted_hosts=list(config["trusted_dashboard_hosts"]),
        )
        return permitted, service_name

    @classmethod
    def _permitted(cls, **values):
        return cls._permitted_and_service(**values)[0]

    def test_the_service_name_is_accepted_with_its_port(self) -> None:
        """FR-015 end to end. A Host header carries the port the client used, so
        an entry without one only works because the guard compares the host
        alone. That pairing is what makes the shipped Service usable."""
        permitted, service = self._permitted_and_service()
        assert permitted.is_host_allowed(f"{service}.{NAMESPACE}.svc.cluster.local:8090")
        assert permitted.is_host_allowed(f"{service}.{NAMESPACE}:8090")
        assert permitted.is_host_allowed(f"{service}:8090")

    def test_it_holds_for_a_release_not_named_after_the_chart(self) -> None:
        """The name-collapsing helper must not leave the trusted entry behind.

        With a release called "prod" the Service is `prod-coordinare`, and a
        trusted list still saying `coordinare` would 403 every request.
        """
        permitted, service = self._permitted_and_service(release="prod")
        assert service == "prod-coordinare"
        assert permitted.is_host_allowed(f"{service}.{NAMESPACE}.svc.cluster.local:8090")

    def test_port_forward_still_works(self) -> None:
        permitted = self._permitted()
        assert permitted.is_host_allowed("127.0.0.1:8090")

    def test_the_bind_all_address_is_not_admitted_by_the_charts_config(self) -> None:
        """The chart binds wide out of necessity; that must not become trust."""
        permitted = self._permitted()
        assert not permitted.is_host_allowed("0.0.0.0:8090"), (
            "the chart's dashboard_host=0.0.0.0 must not become a permitted Host: "
            "browsers route http://0.0.0.0:<port> to loopback, so this would let a "
            "malicious page reach a port-forwarded dashboard"
        )
        assert not permitted.is_origin_allowed("http://0.0.0.0:8090")

    def test_an_unrelated_host_is_still_refused(self) -> None:
        assert not self._permitted().is_host_allowed("evil.example.com")


class TestDocumentedDeletionIsScopedToOneRelease:
    """A destructive command in the docs must not reach past its own release.

    The quickstart said ``app.kubernetes.io/name=coordinare``, which is the CHART
    name and matches every coordinare release in the namespace. An operator
    cleaning up one install would have deleted the state of all of them —
    irreversibly, and while believing they were tidying up after themselves.
    """

    def test_the_chart_labels_every_object_with_its_release(self) -> None:
        for obj in _render():
            labels = obj.get("metadata", {}).get("labels", {})
            assert labels.get("app.kubernetes.io/instance") == RELEASE, (
                f"{obj['kind']} carries no release label, so no documented command "
                "could scope to a single release"
            )

    def test_the_state_claim_carries_the_release_label(self) -> None:
        """The claim is what a cleanup command targets, and it outlives the release."""
        claim = _one(_render(), "StatefulSet")["spec"]["volumeClaimTemplates"][0]
        assert claim["metadata"]["labels"]["app.kubernetes.io/instance"] == RELEASE

    @pytest.mark.parametrize(
        "doc",
        [
            "deploy/helm/coordinare/README.md",
            "deploy/helm/coordinare/templates/NOTES.txt",
            "specs/147-helm-deployment/quickstart.md",
        ],
    )
    def test_no_document_deletes_claims_by_chart_name(self, doc: str) -> None:
        for line in Path(doc).read_text().splitlines():
            if "delete pvc" not in line or line.lstrip().startswith("#"):
                continue
            assert "app.kubernetes.io/name=" not in line, (
                f"{doc} deletes claims by CHART name, which reaches every release "
                f"in the namespace: {line.strip()}"
            )
            assert "app.kubernetes.io/instance=" in line, (
                f"{doc} deletes claims without scoping to a release: {line.strip()}"
            )


class TestTheInstallGateCannotPassOnABrokenRelease:
    """The gate that guards the integration tests, tested itself.

    It exists because a CI job went green having run 16 skipped tests. Its first
    version then reproduced the same shape at a smaller scale: written to catch a
    missing image, it treated *every other* outcome as fine, so a Pod that never
    existed, one with no container yet, and one in CrashLoopBackOff all passed
    silently. Review found all three.

    Test infrastructure that fails open is worse than none, because it converts
    "broken" into "verified". These run without a cluster by stubbing the kubectl
    call, so every branch is covered on every commit rather than only the branch a
    given cluster happens to produce.
    """

    @staticmethod
    def _gate_with(state=None, *, phase="Pending", returncode=0, with_statuses=True):
        """Run the gate against one synthetic Pod status."""
        import importlib.util
        import json
        import subprocess
        import time

        spec = importlib.util.spec_from_file_location(
            "t147_integration", "tests/integration/test_147_helm_install.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        body = {"status": {"phase": phase}}
        if with_statuses:
            body["status"]["containerStatuses"] = [{"state": state or {}}]

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args, returncode, json.dumps(body) if returncode == 0 else "", "not found"
            )

        module._run = fake_run

        # Collapse the gate's own 120s deadline so a negative case does not spend
        # two real minutes proving it eventually gives up.
        real = time.monotonic
        start = real()
        time.monotonic = lambda: 0 if real() - start <= 0 else real() * 60
        try:
            module._require_pod_is_starting_cleanly()
            return "passed"
        except AssertionError:
            return "failed"
        finally:
            time.monotonic = real

    def test_it_passes_once_coordinares_own_code_is_running(self) -> None:
        assert self._gate_with({"running": {}}) == "passed"

    def test_it_passes_on_a_terminated_container(self) -> None:
        """Expected here: without a resolvable board, bootstrap exits by design.

        The gate asks whether the release got far enough to run coordinare, not
        whether coordinare was happy with what it found.
        """
        assert self._gate_with({"terminated": {"exitCode": 2}}) == "passed"

    @pytest.mark.parametrize(
        "reason",
        ["ErrImagePull", "ImagePullBackOff", "ErrImageNeverPull", "InvalidImageName"],
    )
    def test_it_fails_when_the_image_is_missing(self, reason: str) -> None:
        assert self._gate_with({"waiting": {"reason": reason}}) == "failed"

    @pytest.mark.parametrize("reason", ["CrashLoopBackOff", "CreateContainerConfigError"])
    def test_it_fails_on_a_container_that_will_not_start(self, reason: str) -> None:
        """Not an image problem, and previously passed silently for that reason."""
        assert self._gate_with({"waiting": {"reason": reason}}) == "failed"

    def test_it_fails_when_the_pod_never_appears(self) -> None:
        """helm install having failed is the case most worth catching.

        Every later assertion would fail for reasons that look unrelated to a
        release that was never created.
        """
        assert self._gate_with(returncode=1) == "failed"

    def test_it_fails_when_the_pod_never_starts_a_container(self) -> None:
        assert self._gate_with(with_statuses=False) == "failed"


class TestTheDockerPathIsCoveredToo:
    """Issue #223 — the Docker deployment path had no automated coverage.

    It was unbuildable for months and nobody noticed, because nothing built or
    ran that image. The Kubernetes path gained rendering tests and a live cluster
    install in the same PR that discovered this; the asymmetry was backwards,
    since Docker is the path most self-hosters try first.
    """

    @staticmethod
    def _workflow() -> dict:
        return yaml.safe_load(Path(PR_WORKFLOW).read_text())

    def test_the_daemon_image_is_built_and_exercised_in_pr_ci(self) -> None:
        jobs = self._workflow()["jobs"]
        assert "daemon-image" in jobs, (
            "the Docker path needs a check of its own: the image is built and "
            "published by the release pipeline, so without this a break is only "
            "found after merge"
        )
        assert "test_148_docker_daemon_image" in yaml.safe_dump(jobs["daemon-image"])
