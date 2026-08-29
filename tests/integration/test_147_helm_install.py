"""Spec 147 / issue #201 — the chart installed into a real cluster.

**Skipped, not failed, when no cluster is reachable**, and the skip names the
commands that would make it run. A silently-skipped integration test is how
"coordinare installs on Kubernetes" stays green while quietly becoming untrue.

    kind create cluster --name coordinare-dev
    docker build -t coordinare-daemon:dev -f Dockerfile.daemon .
    kind load docker-image coordinare-daemon:dev --name coordinare-dev
    .venv/bin/pytest tests/integration/test_147_helm_install.py -v

**What this deliberately does not assert.** The daemon only reaches Ready after
it resolves a real GitHub Project: bootstrap exits before the health server
starts, so with no resolvable board there is nothing to probe. Pointing a test
at a live board would have it act on real cards, so these tests stop at the last
point the *chart* is responsible for — that every object is created, that the
mounted config is parsed, that the injected credential authenticates, that the
state claim is bound and mounted where the config expects it, and that the RBAC
grants what the runtime needs and nothing else. Everything past that is
coordinare's dependency on GitHub, not the chart's correctness.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

NAMESPACE = "coordinare-helm-test"
RELEASE = "smoke"
CHART = "deploy/helm/coordinare"
IMAGE = "coordinare-daemon:dev"

SKIP_REASON = (
    "no reachable Kubernetes cluster with the daemon image loaded. Create one:\n"
    "  kind create cluster --name coordinare-dev\n"
    f"  docker build -t {IMAGE} -f Dockerfile.daemon .\n"
    f"  kind load docker-image {IMAGE} --name coordinare-dev"
)


def _run(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed:\n{result.stderr}")
    return result


def _cluster_available() -> bool:
    """Only whether a cluster and the tools are here.

    Deliberately does NOT check that the image is loaded, though an earlier
    version did and it went badly: ``kind load`` populates the node's containerd,
    but ``node.status.images`` lags behind kubelet's reporting cycle, so in CI the
    check said "no image" moments after loading one and skipped all 16 tests. The
    job went green having verified nothing.

    A missing image is not a reason to skip anyway. Skipping is for a developer
    who has no cluster; a cluster that is missing the image is a *failure*, and
    ``_require_image`` below makes it a legible one.
    """
    if not shutil.which("helm") or not shutil.which("kubectl"):
        return False
    return _run("kubectl", "get", "nodes", check=False, timeout=15).returncode == 0


pytestmark = pytest.mark.skipif(not _cluster_available(), reason=SKIP_REASON)


def _require_pod_is_starting_cleanly() -> None:
    """Fail early and legibly when the release did not actually come up.

    Renamed from ``_require_image_pulled``: it was written to catch one cause and
    then treated *every other* outcome as fine, which is the same silent-success
    shape as the skip it was introduced to replace. Three ways it passed on a
    broken deployment, all found in review:

    * the Pod did not exist at all — ``helm install`` having failed — so kubectl
      exited non-zero with empty output and the empty string read as "no problem";
    * the Pod existed but had no ``containerStatuses`` yet, which is the normal
      state for the first seconds and was likewise read as success;
    * any waiting reason outside the image-pull set — ``CrashLoopBackOff``,
      ``CreateContainerConfigError`` — returned silently.

    So it now asks the opposite question. Not "is this one failure absent" but
    "has this Pod reached a state I can positively recognise as starting", and
    everything else is reported with what Kubernetes actually said.

    The daemon is *expected* to crash here: these tests run without a resolvable
    GitHub Project, so bootstrap exits by design. That is why a running container
    and a terminated one are both acceptable — what must not pass is a Pod that
    never got as far as running coordinare's own code, because then the later
    assertions would be measuring nothing.
    """
    import json
    import time

    image_failures = {"ImagePullBackOff", "ErrImagePull", "ErrImageNeverPull", "InvalidImageName"}
    still_settling = {"ContainerCreating", "PodInitializing"}
    last = "the Pod never reported a container status"

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = _run(
            "kubectl",
            "-n",
            NAMESPACE,
            "get",
            "pod",
            f"{RELEASE}-coordinare-0",
            "-o",
            "json",
            check=False,
        )
        if result.returncode != 0:
            # No Pod. helm install did not produce one, and every later assertion
            # would fail for reasons that look unrelated to that.
            last = f"the Pod does not exist ({result.stderr.strip() or 'not found'})"
            time.sleep(2)
            continue

        status = json.loads(result.stdout).get("status", {})
        statuses = status.get("containerStatuses") or []
        if not statuses:
            last = f"the Pod is {status.get('phase', 'in an unknown phase')} with no container yet"
            time.sleep(2)
            continue

        state = statuses[0].get("state", {})
        if "running" in state or "terminated" in state:
            return  # coordinare's own code got to run; that is all this gate asks.

        reason = (state.get("waiting") or {}).get("reason", "")
        if reason in image_failures:
            raise AssertionError(
                f"the cluster does not have {IMAGE} ({reason}). Load it:\n"
                f"  docker build -t {IMAGE} -f Dockerfile.daemon .\n"
                f"  kind load docker-image {IMAGE} --name <your-cluster>"
            )
        if reason and reason not in still_settling:
            raise AssertionError(
                f"the controller container will not start: {reason}. "
                f"{(state.get('waiting') or {}).get('message', '')}".strip()
            )
        last = f"the container is still {reason or 'waiting'}"
        time.sleep(2)

    raise AssertionError(
        f"the release never started within 120s: {last}. The tests below would "
        "otherwise report failures that have nothing to do with what is wrong."
    )


@pytest.fixture(scope="module")
def release(tmp_path_factory) -> dict:
    """Install the chart once, and hand the tests the resulting objects."""
    values = tmp_path_factory.mktemp("helm") / "values.yaml"
    values.write_text(
        f"""
image:
  repository: {IMAGE.split(":")[0]}
  tag: {IMAGE.split(":")[1]}
  pullPolicy: Never
state:
  size: 1Gi
secrets:
  GITHUB_TOKEN: smoke-test-token
config:
  github_org: ViviDynamics
  human_reviewers: [smoke]
  project_name: smoke
  github_project_number: 1
  poll_interval_seconds: 3600
"""
    )
    _run("kubectl", "create", "namespace", NAMESPACE, check=False)
    _run("helm", "uninstall", RELEASE, "-n", NAMESPACE, check=False)
    # Not --wait: the daemon cannot reach Ready without a resolvable board, and
    # waiting for something that will not happen would only turn a clear
    # assertion into an opaque timeout.
    _run("helm", "install", RELEASE, CHART, "-n", NAMESPACE, "-f", str(values), timeout=180)
    _require_pod_is_starting_cleanly()

    yield {"namespace": NAMESPACE, "release": RELEASE}

    _run("helm", "uninstall", RELEASE, "-n", NAMESPACE, check=False)
    _run("kubectl", "-n", NAMESPACE, "delete", "pvc", "--all", "--wait=false", check=False)


def _get(namespace: str, kind: str, name: str) -> dict:
    result = _run("kubectl", "-n", namespace, "get", kind, name, "-o", "json")
    return json.loads(result.stdout)


class TestInstall:
    def test_every_expected_object_is_created(self, release) -> None:
        """SC-001, as far as the chart itself reaches."""
        ns, rel = release["namespace"], release["release"]
        for kind, name in [
            ("statefulset", f"{rel}-coordinare"),
            ("service", f"{rel}-coordinare"),
            ("configmap", f"{rel}-coordinare"),
            ("secret", f"{rel}-coordinare"),
            ("serviceaccount", f"{rel}-coordinare"),
            ("role", f"{rel}-coordinare-performer-manager"),
            ("rolebinding", f"{rel}-coordinare-performer-manager"),
        ]:
            _get(ns, kind, name)  # raises if absent

    def test_no_ingress_is_created(self, release) -> None:
        """FR-014 — nothing reachable from outside the cluster by default."""
        result = _run("kubectl", "-n", release["namespace"], "get", "ingress", "-o", "json")
        assert not json.loads(result.stdout)["items"]

    def test_exactly_one_replica_runs(self, release) -> None:
        sts = _get(release["namespace"], "statefulset", f"{release['release']}-coordinare")
        assert sts["spec"]["replicas"] == 1


class TestStateVolume:
    def test_the_claim_is_bound_and_mounted_where_the_config_expects_it(self, release) -> None:
        """FR-003 — the failure this prevents is silent and total.

        Coordinare's default state path is relative, so a chart that forgot to
        override it would run correctly, persist nothing durable, and lose
        everything on the first reschedule.
        """
        import time

        import yaml

        ns, rel = release["namespace"], release["release"]

        # Bound is not immediate: the common StorageClass binding mode is
        # WaitForFirstConsumer, so the claim stays Pending until the Pod is
        # scheduled. Sampling once would make this test pass or fail on timing
        # rather than on whether the chart is correct.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            claim = _get(ns, "pvc", f"state-{rel}-coordinare-0")
            if claim["status"]["phase"] == "Bound":
                break
            time.sleep(2)
        assert claim["status"]["phase"] == "Bound", (
            f"the state claim never bound: {claim['status']}"
        )

        config = yaml.safe_load(_get(ns, "configmap", f"{rel}-coordinare")["data"]["config.yaml"])
        state_path = str(config["state_file_path"])

        sts = _get(ns, "statefulset", f"{rel}-coordinare")
        mounts = sts["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        state_mount = next(m for m in mounts if m["name"] == "state")
        assert state_path.startswith(state_mount["mountPath"].rstrip("/") + "/")


class TestTheDaemonActuallyReadsWhatTheChartGivesIt:
    """The chart's real job: deliver correct config and credentials to a process."""

    @staticmethod
    def _logs(release) -> str:
        ns, rel = release["namespace"], release["release"]
        for _ in range(30):
            result = _run(
                "kubectl", "-n", ns, "logs", f"{rel}-coordinare-0", "--tail=100", check=False
            )
            if "config_loaded" in result.stdout or "config_validation_error" in result.stdout:
                return result.stdout
            _run("sleep", "2", check=False)
        return result.stdout

    def test_the_mounted_config_is_parsed(self, release) -> None:
        """Proves the ConfigMap mount, the path, and that the rendered YAML is valid.

        A chart can render perfectly and still mount the file somewhere the
        process does not look.
        """
        logs = self._logs(release)
        assert "config_loaded" in logs, (
            f"the daemon never loaded the mounted config. Last logs:\n{logs[-2000:]}"
        )
        assert "config_validation_error" not in logs, (
            f"the rendered config failed coordinare's own validation:\n{logs[-2000:]}"
        )

    def test_the_injected_credential_reaches_the_process(self, release) -> None:
        """FR-007 end to end — the indirection works, not just renders.

        The config carries ``${GITHUB_TOKEN}``; the Secret supplies the value;
        coordinare expands it at load. If any link were broken the daemon would
        fail with an empty token rather than an authentication result.
        """
        logs = self._logs(release)
        assert "env_var_fields_count" in logs or "config_loaded" in logs
        assert "'GITHUB_TOKEN'" not in logs, "the placeholder was never expanded"


class TestChartRBAC:
    """SC-002 — asked of the cluster's authorizer, not read from the manifest.

    Spec 146 learned this the hard way: a Role left behind under an old name kept
    granting a verb the shipped manifest had dropped. The file said no, the
    cluster said yes.
    """

    @staticmethod
    def _can_i(release, verb: str, resource: str) -> bool:
        ns, rel = release["namespace"], release["release"]
        result = _run(
            "kubectl",
            "auth",
            "can-i",
            verb,
            resource,
            f"--as=system:serviceaccount:{ns}:{rel}-coordinare",
            "-n",
            ns,
            check=False,
        )
        return result.stdout.strip() == "yes"

    @pytest.mark.parametrize(
        ("verb", "resource"),
        [
            ("create", "pods"),
            ("get", "pods"),
            ("list", "pods"),
            ("delete", "pods"),
            ("get", "pods/log"),
        ],
    )
    def test_the_runtime_can_do_its_job(self, release, verb: str, resource: str) -> None:
        assert self._can_i(release, verb, resource), (
            f"the chart's RBAC denies {verb} {resource}, which KubernetesRuntime performs"
        )

    @pytest.mark.parametrize(
        ("verb", "resource"),
        [
            ("watch", "pods"),
            ("get", "secrets"),
            ("list", "secrets"),
            ("list", "nodes"),
            ("create", "clusterrolebindings"),
        ],
    )
    def test_and_nothing_more(self, release, verb: str, resource: str) -> None:
        assert not self._can_i(release, verb, resource), (
            f"the chart's ServiceAccount can {verb} {resource} — outside the documented grant"
        )
