"""Issue #225 — the enforcement probe against a real cluster.

The unit tests pin the probe's *logic*. This checks it tells the truth about an
actual cluster, which is the only thing that matters: the whole feature exists
because a NetworkPolicy is inert unless the CNI implements it, and no amount of
reasoning establishes whether yours does.

**Measured here: kind's kindnet enforces INGRESS but not EGRESS.** So on the
development cluster the probe should report *not enforced* — and a test that
demanded "enforced" would be asserting the CNI, not the probe.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

NAMESPACE = "default"
IMAGE = "coordinare-daemon:dev"

SKIP_REASON = (
    "no reachable Kubernetes cluster with the daemon image loaded. Create one:\n"
    "  kind create cluster --name coordinare-dev\n"
    f"  kind load docker-image {IMAGE} --name coordinare-dev"
)


def _cluster_available() -> bool:
    if shutil.which("kubectl") is None:
        return False
    return (
        subprocess.run(
            ["kubectl", "get", "nodes"], capture_output=True, text=True, timeout=15
        ).returncode
        == 0
    )


pytestmark = [
    pytest.mark.skipif(not _cluster_available(), reason=SKIP_REASON),
    pytest.mark.timeout(900),
]


@pytest.fixture
def clients():
    from kubernetes import client, config

    config.load_kube_config()
    return client.CoreV1Api(), client.NetworkingV1Api()


def _probe(clients):
    from coordinare.services.kubernetes_egress import probe_network_policy_enforcement

    core_v1, networking_v1 = clients
    return asyncio.run(
        probe_network_policy_enforcement(
            core_v1=core_v1,
            networking_v1=networking_v1,
            namespace=NAMESPACE,
            image=IMAGE,
        )
    )


def test_the_probe_reaches_a_conclusion(clients) -> None:
    """Conclusive means the control worked: the target was reachable with no policy.

    An inconclusive result is not a failure of the cluster, it is a failure to
    measure — and the probe reports it as such rather than guessing.
    """
    verdict = _probe(clients)
    assert verdict.conclusive, (
        f"the probe could not establish a baseline, so it measured nothing: {verdict.detail}"
    )


def test_the_verdict_matches_what_the_cluster_actually_does(clients) -> None:
    """Asserts agreement with a hand-run experiment, not a hardcoded expectation.

    Hardcoding "kind does not enforce egress" would make this a test of kindnet's
    version rather than of the probe. Instead the same thing is measured directly
    and the two are compared: if they ever disagree, the probe is wrong.
    """
    verdict = _probe(clients)

    # Independent measurement: deny-all egress on a Pod that demonstrably has
    # network access, then see whether it still reaches the outside.
    subprocess.run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "delete",
            "networkpolicy",
            "manual-deny",
            "--ignore-not-found",
        ],
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        ["kubectl", "-n", NAMESPACE, "apply", "-f", "-"],
        input=(
            "apiVersion: networking.k8s.io/v1\n"
            "kind: NetworkPolicy\n"
            "metadata: {name: manual-deny}\n"
            "spec:\n"
            "  podSelector: {matchLabels: {manual-egress-probe: 'yes'}}\n"
            "  policyTypes: [Egress]\n"
            "  egress: []\n"
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    try:
        result = subprocess.run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "run",
                "manual-probe",
                "--image",
                IMAGE,
                "--restart=Never",
                "--rm",
                "-i",
                "--quiet",
                "--labels",
                "manual-egress-probe=yes",
                "--overrides",
                '{"spec":{"containers":[{"name":"manual-probe","image":"' + IMAGE + '",'
                '"imagePullPolicy":"Never","command":["/app/.venv/bin/python","-c",'
                "\"import socket;socket.create_connection(('10.96.0.1',443),timeout=8);"
                "print('REACHED')\"]}]}}",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        manually_blocked = "REACHED" not in result.stdout
    finally:
        subprocess.run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "delete",
                "networkpolicy",
                "manual-deny",
                "--ignore-not-found",
            ],
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            ["kubectl", "-n", NAMESPACE, "delete", "pod", "manual-probe", "--ignore-not-found"],
            capture_output=True,
            timeout=60,
        )

    assert verdict.enforced == manually_blocked, (
        f"the probe says enforced={verdict.enforced} but a hand-run deny-all "
        f"{'blocked' if manually_blocked else 'did not block'} traffic. One of them "
        "is wrong, and it matters which: reporting enforcement that is not there "
        "tells an operator performers are contained when they are not."
    )


def test_the_probe_leaves_nothing_behind(clients) -> None:
    """A diagnostic that litters a live namespace is its own problem.

    Polls rather than sampling once. Deletion is a request, not an event: the
    probe issues deletes and returns, and a Pod is briefly still listed before the
    API records a deletionTimestamp. Checking instantly measured that lag rather
    than whether cleanup happened.
    """
    import time

    _probe(clients)

    deadline = time.monotonic() + 90
    stragglers: list[str] = []
    while time.monotonic() < deadline:
        stragglers = []
        for kind in ("pods", "networkpolicies"):
            listed = subprocess.run(
                ["kubectl", "-n", NAMESPACE, "get", kind, "-o", "name"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            for name in listed.stdout.split():
                if "coordinare-np-" not in name:
                    continue
                deleting = subprocess.run(
                    [
                        "kubectl",
                        "-n",
                        NAMESPACE,
                        "get",
                        name,
                        "-o",
                        "jsonpath={.metadata.deletionTimestamp}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if not deleting.stdout.strip():
                    stragglers.append(name)
        if not stragglers:
            return
        time.sleep(3)

    pytest.fail(f"the probe left these behind, not being deleted: {stragglers}")
