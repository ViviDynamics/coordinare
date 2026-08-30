"""Egress restriction for performer Pods, and proof that it works (issue #225).

Spec 146 shipped the Kubernetes path with **no** egress control and said so,
rather than shipping a NetworkPolicy that might silently not apply. This adds one,
opt-in, together with the thing that makes it honest: a probe that empirically
establishes whether the cluster enforces policy at all.

**A NetworkPolicy cannot express a hostname.** ``egress.to`` accepts only
``ipBlock``, ``namespaceSelector`` and ``podSelector`` — verified against the live
API, not assumed. The Docker path's allowlist is hostname-based (resolved inside
the container with iptables), so the two are **not equivalent** and this must not
be described as parity. What is offered here is CIDR-level egress plus DNS. An
operator who needs hostname-level control needs a CNI that implements FQDN
policies, or an egress proxy.

**Why the probe matters more than the policy.** A NetworkPolicy is inert unless
the CNI implements it. Applying one on a cluster that ignores it produces exactly
the outcome spec 146 refused: an operator who believes performers are contained
when nothing contains them. The policy is easy; the proof is the feature.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: Label every performer Pod carries (spec 146). The policy selects on it, so it
#: applies to performers and never to the controller — which is the same mistake
#: the orphan sweep made before issue #224.
PERFORMER_ID_LABEL = "coordinare.vividynamics.com/performer-id"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "coordinare"

#: DNS must be reachable or nothing resolves and every allowed destination fails
#: in a way that looks like the allowlist is wrong. Port 53 to kube-dns, both
#: protocols, is the minimum a restrictive policy has to keep open.
DNS_PORTS = (("UDP", 53), ("TCP", 53))


@dataclass(frozen=True, slots=True)
class EnforcementVerdict:
    """What the cluster actually does with a NetworkPolicy.

    ``enforced`` is only ever True when the probe first proved the target was
    reachable *without* a policy. Without that control, "blocked" is
    indistinguishable from "never reachable" — and that failure claims protection
    the operator does not have, which is the direction that must never be guessed.
    """

    enforced: bool
    baseline_reachable: bool
    detail: str
    #: False when the restricted Pod never ran, so "did not connect" is not
    #: evidence of anything. Separate from ``baseline_reachable`` because the two
    #: fail for different reasons and an operator should be able to tell which.
    measured_under_policy: bool = True

    @property
    def conclusive(self) -> bool:
        return self.baseline_reachable and self.measured_under_policy


def build_performer_network_policy(
    *,
    name: str,
    namespace: str,
    allowed_cidrs: list[str] | None = None,
    dns_namespace: str = "kube-system",
) -> dict[str, Any]:
    """A default-deny egress policy for performer Pods, opening only what is listed.

    Selects on the performer-id label, so it applies to performers and never to
    the controller. Selecting on ``managed-by`` alone would have caught the
    controller too — the same mistake the orphan sweep made before issue #224.
    """
    egress: list[dict[str, Any]] = [
        {
            # DNS first. Without it every allowed destination fails to resolve and
            # the symptom looks like a wrong allowlist rather than a missing rule.
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": dns_namespace}
                    }
                }
            ],
            "ports": [{"protocol": protocol, "port": port} for protocol, port in DNS_PORTS],
        }
    ]

    for cidr in allowed_cidrs or []:
        egress.append({"to": [{"ipBlock": {"cidr": cidr}}]})

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "podSelector": {
                "matchLabels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
                "matchExpressions": [{"key": PERFORMER_ID_LABEL, "operator": "Exists"}],
            },
            # Egress only. Ingress is left alone deliberately: coordinare reaches
            # each performer's HTTP port, and a default-deny ingress rule would cut
            # the connection the whole system depends on.
            "policyTypes": ["Egress"],
            "egress": egress,
        },
    }


@dataclass
class _ProbeResources:
    """Names created by a probe, so teardown can be exhaustive and best-effort."""

    namespace: str
    pods: list[str] = field(default_factory=list)
    policies: list[str] = field(default_factory=list)


def _probe_pod(name: str, image: str, command: list[str], labels: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "probe",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": command,
                }
            ],
        },
    }


async def probe_network_policy_enforcement(
    *,
    core_v1: Any,
    networking_v1: Any,
    namespace: str,
    image: str,
    timeout_s: int = 180,
) -> EnforcementVerdict:
    """Find out whether this cluster actually enforces NetworkPolicy egress.

    Runs the experiment rather than inferring it from the CNI's name. Which CNI is
    installed is a poor proxy: implementations gain and lose policy support between
    versions, and a wrong guess here tells an operator they are protected when they
    are not.

    The order is the point:

    1. start a target that serves HTTP;
    2. reach it from a client with **no policy** — this is the control, and if it
       fails the probe reports *inconclusive*, never "enforced". A probe that
       cannot tell "blocked" from "never reachable" fails towards claiming safety;
    3. apply a deny-all **egress** policy to the client and try again.

    Blocked at step 3 having succeeded at step 2 is the only evidence that counts.

    Step 3 is measured twice when the first attempt gets through, because applying a
    policy and having the CNI program it are different events, and traffic in the
    window between them is not evidence of anything. Twice, never more: "this cluster
    does not enforce NetworkPolicy" is a true answer and must not arrive as a timeout.

    Worst case is therefore four Pod lifetimes -- target, control, and two restricted
    -- each bounded by *timeout_s*: twelve minutes at the default 180s, up from nine,
    and only on a cluster where every Pod hangs to its deadline. The second restricted
    Pod runs only when the first let traffic through, so a cluster that enforces (the
    reassuring case) is no slower than before.
    """
    import asyncio

    from kubernetes.client.rest import ApiException

    # Unique per run, not derived from the inputs. A deterministic name collides
    # with the previous run's Pods while they are still Terminating — the exact
    # trap spec 146 hit with performer Pods, and rewriting it here would make a
    # diagnostic fail for reasons that have nothing to do with what it measures.
    # A probe is independent per invocation, so it has no reason to want a stable
    # name in the first place.
    suffix = uuid.uuid4().hex[:8]
    target_name = f"coordinare-np-target-{suffix}"
    control_name = f"coordinare-np-control-{suffix}"
    denied_name = f"coordinare-np-denied-{suffix}"
    policy_name = f"coordinare-np-probe-{suffix}"
    created = _ProbeResources(namespace=namespace)

    async def _create_pod(name: str, command: list[str], labels: dict[str, str]) -> None:
        await asyncio.to_thread(
            core_v1.create_namespaced_pod,
            namespace=namespace,
            body=_probe_pod(name, image, command, labels),
        )
        created.pods.append(name)

    async def _pod_ip(name: str) -> str | None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            pod = await asyncio.to_thread(
                core_v1.read_namespaced_pod, name=name, namespace=namespace
            )
            if pod.status.phase == "Running" and pod.status.pod_ip:
                return pod.status.pod_ip
            if pod.status.phase in {"Failed", "Succeeded"}:
                return None
            await asyncio.sleep(2)
        return None

    async def _outcome(name: str) -> str:
        """``reached`` / ``blocked`` / ``never_ran`` for one probe Pod.

        Three states, not two, and the third is the whole point. A Pod that never
        ran — unschedulable, ImagePullBackOff, out of quota — produces no
        connection, and reading that as "blocked" is how a probe reports
        enforcement on a cluster that enforces nothing. That is the one direction
        this must never fail in, so "did not connect" only counts as evidence when
        the Pod demonstrably ran.
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            pod = await asyncio.to_thread(
                core_v1.read_namespaced_pod, name=name, namespace=namespace
            )
            if pod.status.phase in {"Succeeded", "Failed"}:
                try:
                    logs = await asyncio.to_thread(
                        core_v1.read_namespaced_pod_log, name=name, namespace=namespace
                    )
                except ApiException:
                    # The Pod ran but its output is gone. Not evidence either way.
                    return "never_ran"
                text = logs or ""
                if "REACHED" in text:
                    return "reached"
                if "ATTEMPTED" not in text:
                    # It ran, but never got as far as trying. Not evidence.
                    return "never_ran"
                return "blocked"
            await asyncio.sleep(2)
        return "never_ran"

    async def _cleanup() -> None:
        """Best-effort and exhaustive. Both words are load-bearing.

        Catches ``Exception``, not just ``ApiException``: a socket timeout during
        teardown is not wrapped by the client, and letting it propagate would abort
        the loop and leave everything after it behind.

        And it *logs*. Silently swallowing a failed delete means the namespace
        accumulates probe Pods with nothing to explain why — the operator sees
        litter and no reason for it.
        """
        for pod in created.pods:
            try:
                await asyncio.to_thread(
                    core_v1.delete_namespaced_pod, name=pod, namespace=namespace
                )
            except Exception as exc:
                _log.warning(
                    "kubernetes_egress.probe_cleanup_failed",
                    resource=f"pod/{pod}",
                    namespace=namespace,
                    error=str(exc),
                )
        for policy in created.policies:
            try:
                await asyncio.to_thread(
                    networking_v1.delete_namespaced_network_policy,
                    name=policy,
                    namespace=namespace,
                )
            except Exception as exc:
                _log.warning(
                    "kubernetes_egress.probe_cleanup_failed",
                    resource=f"networkpolicy/{policy}",
                    namespace=namespace,
                    error=str(exc),
                )

    try:
        await _create_pod(
            target_name,
            ["/app/.venv/bin/python", "-m", "http.server", "8080"],
            {"coordinare-np-probe": "target"},
        )
        target_ip = await _pod_ip(target_name)
        if not target_ip:
            return EnforcementVerdict(
                enforced=False,
                baseline_reachable=False,
                detail="the probe's target Pod never started, so nothing could be measured",
            )

        # Retries for a while before giving up. "Running with an IP" does not mean
        # the server inside has bound its port, and a control that connects too
        # early reports "blocked" for a target that was merely slow — which makes
        # every run inconclusive. Retrying costs nothing on the restricted Pod,
        # where the attempts simply keep failing.
        reach_script = (
            # ATTEMPTED first, before anything that can fail. Without it, a Pod that
            # ran and crashed *before trying* — an import error, an OOM kill, a bad
            # command — produces logs with no REACHED and is read as "blocked",
            # which reports enforcement that was never observed. Proving the attempt
            # happened is what separates "the network stopped it" from "it never got
            # that far".
            "print('ATTEMPTED', flush=True)\n"
            "import time,urllib.request\n"
            "deadline = time.time() + 25\n"
            "while time.time() < deadline:\n"
            "    try:\n"
            f"        urllib.request.urlopen('http://{target_ip}:8080', timeout=4)\n"
            "        print('REACHED')\n"
            "        break\n"
            "    except Exception:\n"
            "        time.sleep(2)\n"
        )

        # Control. Without this, "blocked" might only mean "never reachable".
        await _create_pod(
            control_name,
            ["/app/.venv/bin/python", "-c", reach_script],
            {"coordinare-np-probe": "control"},
        )
        if await _outcome(control_name) != "reached":
            return EnforcementVerdict(
                enforced=False,
                baseline_reachable=False,
                detail=(
                    "the probe could not reach its own target even with no policy in "
                    "place, so this cluster's enforcement cannot be determined. "
                    "Reporting inconclusive rather than 'enforced', because a probe "
                    "that mistakes unreachable for blocked would claim protection you "
                    "do not have."
                ),
            )

        await asyncio.to_thread(
            networking_v1.create_namespaced_network_policy,
            namespace=namespace,
            body={
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": policy_name, "namespace": namespace},
                "spec": {
                    "podSelector": {"matchLabels": {"coordinare-np-probe": "denied"}},
                    "policyTypes": ["Egress"],
                    "egress": [],
                },
            },
        )
        created.policies.append(policy_name)

        async def _measure_under_policy(name: str) -> str:
            """One Pod's attempt to reach the control target with the deny-all in place."""
            await _create_pod(
                name,
                ["/app/.venv/bin/python", "-c", reach_script],
                {"coordinare-np-probe": "denied"},
            )
            return await _outcome(name)

        def _never_ran(which: str) -> EnforcementVerdict:
            # The Pod that was supposed to demonstrate blocking never ran, so there
            # is nothing to conclude. Reporting "enforced" here would be the exact
            # failure this probe exists to avoid.
            return EnforcementVerdict(
                enforced=False,
                baseline_reachable=True,
                detail=(
                    f"the probe's {which} restricted Pod never ran, so whether the policy "
                    "would have blocked it is unknown. Reporting inconclusive rather than "
                    "'enforced': a Pod that never started produces no traffic, and "
                    "reading that as containment would claim protection you do not have."
                ),
                measured_under_policy=False,
            )

        first = await _measure_under_policy(denied_name)
        if first == "never_ran":
            return _never_ran("first")

        if first == "blocked":
            return EnforcementVerdict(
                enforced=True,
                baseline_reachable=True,
                detail=(
                    "a deny-all egress policy blocked traffic that flowed without it: this "
                    "cluster enforces NetworkPolicy"
                ),
            )

        # 154 (#233): traffic got through, and that has two causes that look identical
        # at this instant -- a cluster that does not enforce, and a cluster whose CNI
        # had not programmed the policy yet. Applying a policy and having it take
        # effect are different events. They are distinguishable over time rather than
        # at a point: a cluster that ignores policy lets traffic through every time.
        # So measure once more before telling an operator they have no protection.
        #
        # Once, not in a loop. "Your cluster does not enforce NetworkPolicy" is a true
        # and useful answer, and a loop would deliver it as a timeout.
        second = await _measure_under_policy(f"{denied_name}-again")
        if second == "never_ran":
            # Deliberately inconclusive, and worth spelling out because it reads at a
            # glance like enough evidence. Traffic did get through once -- but that is
            # precisely what a policy the CNI has not programmed yet looks like, which
            # is the whole reason a second measurement exists. Reporting "not enforced"
            # from the first reach alone would be the bug this function was changed to
            # fix, arrived at by another route.
            return EnforcementVerdict(
                enforced=False,
                baseline_reachable=True,
                detail=(
                    "traffic got through immediately after the deny-all egress policy "
                    "was applied, and the second Pod sent to confirm it never ran. That "
                    "first attempt is not enough on its own: a policy the CNI has not "
                    "programmed yet looks exactly like one that is never enforced. "
                    "Reporting inconclusive; re-run the check."
                ),
                measured_under_policy=False,
            )

        if second == "blocked":
            return EnforcementVerdict(
                enforced=True,
                baseline_reachable=True,
                detail=(
                    "traffic got through immediately after the deny-all egress policy was "
                    "applied, but a second attempt was blocked: this cluster enforces "
                    "NetworkPolicy, and takes a moment to settle after one is created. "
                    "Anything that measures enforcement the instant a policy is applied "
                    "will get the wrong answer here."
                ),
            )

        return EnforcementVerdict(
            enforced=False,
            baseline_reachable=True,
            detail=(
                "traffic flowed THROUGH a deny-all egress policy twice, the second time "
                "well after it was applied: this cluster does not enforce NetworkPolicy, "
                "and applying one here would give you no protection while looking as "
                "though it had"
            ),
        )
    finally:
        await _cleanup()
