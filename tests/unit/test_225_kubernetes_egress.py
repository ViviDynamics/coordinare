"""Issue #225 — opt-in egress restriction, and the proof that it works.

Spec 146 shipped the Kubernetes path with no egress control and said so, rather
than shipping a NetworkPolicy that might silently not apply. This adds one,
opt-in, plus the part that makes it honest: a probe that measures whether the
cluster enforces policy rather than inferring it from the CNI's name.

**Measured on kind: kindnet enforces INGRESS but not EGRESS.** Partial support is
the common case, which is why "does your CNI support NetworkPolicy" is the wrong
question and the probe asks about the direction actually relied on.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinare.services.kubernetes_egress import (
    MANAGED_BY_LABEL,
    PERFORMER_ID_LABEL,
    EnforcementVerdict,
    build_performer_network_policy,
)

CHART = "deploy/helm/coordinare"

#: Applied to every test that shells out to helm. Guarding on the BINARY, not on
#: an exit code: subprocess.run raises FileNotFoundError before there is a code to
#: inspect, so a returncode check never fires and the job ERRORS. That trap was
#: fixed in test_146 earlier and then rewritten here from scratch — hence a shared
#: marker rather than a per-test guard anyone can forget.
#:
#: These must therefore RUN somewhere that has helm: the Chart CI job, which is
#: asserted to include this file by tests/unit/test_147_helm_deployment.py.
needs_helm = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm is not installed; the Chart CI job runs these",
)


def _policy(**kwargs):
    kwargs.setdefault("name", "perf-egress")
    kwargs.setdefault("namespace", "coordinare")
    return build_performer_network_policy(**kwargs)


class TestThePolicyAppliesToPerformersOnly:
    """The controller must never be caught by it.

    Selecting on ``managed-by`` alone is exactly the mistake the orphan sweep made
    before issue #224, where coordinare deleted its own Pod. The chart labels the
    controller ``managed-by=coordinare`` too.
    """

    def test_it_requires_the_performer_id_label(self) -> None:
        selector = _policy()["spec"]["podSelector"]
        expressions = selector.get("matchExpressions", [])
        assert any(
            e["key"] == PERFORMER_ID_LABEL and e["operator"] == "Exists" for e in expressions
        ), "without this the policy also selects the controller"

    def test_it_still_scopes_to_coordinare(self) -> None:
        assert _policy()["spec"]["podSelector"]["matchLabels"][MANAGED_BY_LABEL] == "coordinare"

    @needs_helm
    def test_the_controller_pod_would_not_match(self) -> None:
        """Checked against the chart's real labels, not a guess at them."""
        rendered = subprocess.run(
            ["helm", "template", "t", CHART, "-n", "ns"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if rendered.returncode != 0:
            pytest.skip("helm could not render the chart")

        controller_labels: dict[str, str] = {}
        for doc in yaml.safe_load_all(rendered.stdout):
            if doc and doc.get("kind") == "StatefulSet":
                controller_labels = doc["spec"]["template"]["metadata"]["labels"]
        assert controller_labels
        assert PERFORMER_ID_LABEL not in controller_labels


class TestThePolicyIsUsable:
    def test_dns_is_always_allowed(self) -> None:
        """Without it nothing resolves, and every allowed destination fails in a
        way that looks like a wrong allowlist rather than a missing rule."""
        egress = _policy()["spec"]["egress"]
        dns_rule = egress[0]
        ports = {(p["protocol"], p["port"]) for p in dns_rule["ports"]}
        assert ports == {("UDP", 53), ("TCP", 53)}

    def test_it_restricts_egress_and_leaves_ingress_alone(self) -> None:
        """A default-deny ingress rule would cut coordinare's own connection to the
        performer's HTTP port, which is how work is dispatched at all."""
        assert _policy()["spec"]["policyTypes"] == ["Egress"]

    def test_allowed_cidrs_become_rules(self) -> None:
        egress = _policy(allowed_cidrs=["140.82.112.0/20", "10.0.0.0/8"])["spec"]["egress"]
        cidrs = [r["to"][0]["ipBlock"]["cidr"] for r in egress if "ipBlock" in r["to"][0]]
        assert cidrs == ["140.82.112.0/20", "10.0.0.0/8"]

    def test_with_no_cidrs_only_dns_is_permitted(self) -> None:
        """Default-deny is the point: an empty allowlist is maximally restrictive,
        not accidentally permissive."""
        egress = _policy()["spec"]["egress"]
        assert len(egress) == 1


class TestTheProbeNeverClaimsProtectionItCannotShow:
    """The property the whole feature rests on.

    ``enforced`` may only be True when the probe first proved the target was
    reachable WITHOUT a policy. Without that control, "blocked" is
    indistinguishable from "never reachable" — and that mistake tells an operator
    they are contained when they are not.

    This is not hypothetical: the first hand-written version of this probe used a
    target that never served, so the baseline was already blocked. It would have
    reported "enforced" on every cluster in existence.
    """

    def test_an_unreachable_baseline_is_inconclusive_not_enforced(self) -> None:
        verdict = EnforcementVerdict(
            enforced=False, baseline_reachable=False, detail="target never started",
        )
        assert not verdict.conclusive
        assert not verdict.enforced

    def test_a_verdict_is_only_conclusive_with_a_working_baseline(self) -> None:
        assert EnforcementVerdict(True, True, "blocked").conclusive
        assert not EnforcementVerdict(True, False, "blocked").conclusive

    def test_the_probe_establishes_the_control_before_applying_a_policy(self) -> None:
        """Ordering is the requirement, so it is asserted on the code.

        Applying the policy first and then measuring would make the control
        meaningless — which is precisely the bug the docstring above describes.
        """
        import inspect

        from coordinare.services import kubernetes_egress

        source = inspect.getsource(kubernetes_egress.probe_network_policy_enforcement)
        control_at = source.index("baseline_reachable=False")
        policy_at = source.index("create_namespaced_network_policy")
        assert control_at < policy_at, (
            "the control must be measured before any policy exists, or 'blocked' proves nothing"
        )

    def test_the_probe_cleans_up_whatever_it_created(self) -> None:
        """A diagnostic that litters a live namespace is its own problem."""
        import inspect

        from coordinare.services import kubernetes_egress

        source = inspect.getsource(kubernetes_egress.probe_network_policy_enforcement)
        assert "finally:" in source and "_cleanup()" in source


@needs_helm
class TestTheChartShipsItOffByDefault:
    @staticmethod
    def _render(*args: str) -> list:
        rendered = subprocess.run(
            ["helm", "template", "t", CHART, "-n", "ns", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if rendered.returncode != 0:
            pytest.skip(f"helm could not render: {rendered.stderr[:200]}")
        return [d for d in yaml.safe_load_all(rendered.stdout) if d]

    def test_no_policy_by_default(self) -> None:
        """Off is the honest default: on a cluster that does not enforce, a policy
        gives no protection while looking as though it does."""
        assert not [d for d in self._render() if d.get("kind") == "NetworkPolicy"]

    def test_enabling_it_renders_one(self) -> None:
        docs = self._render("--set", "performers.egress.enabled=true")
        policies = [d for d in docs if d.get("kind") == "NetworkPolicy"]
        assert len(policies) == 1
        assert policies[0]["spec"]["policyTypes"] == ["Egress"]

    def test_the_values_file_tells_the_operator_to_verify_enforcement(self) -> None:
        """The feature is only honest if the caveat travels with it."""
        values = Path(CHART, "values.yaml").read_text().lower()
        assert "doctor --check egress" in values
        assert "kindnet" in values, (
            "the measured example of partial support is what makes the warning concrete"
        )

    def test_the_values_file_says_cidrs_are_not_hostnames(self) -> None:
        """A NetworkPolicy cannot express 'allow api.github.com', and the Docker
        path's allowlist can — presenting them as equivalent would mislead."""
        values = Path(CHART, "values.yaml").read_text().lower()
        assert "hostname" in values


class TestTheProbeCannotReportEnforcementItDidNotObserve:
    """The fail-open path, driven end to end against fake clients.

    The earlier tests pinned the *dataclass* contract. This runs the probe itself,
    because the bug was in the probe: a restricted Pod that never ran produced no
    connection, and "no connection" was read as "blocked" — so the probe reported
    ENFORCED on a cluster enforcing nothing. That is the single worst outcome for
    this feature, since it tells an operator performers are contained when they
    are not.

    Found by asking the question directly rather than by reading the code.
    """

    @staticmethod
    def _run(denied_phase: str, denied_logs: str = ""):
        import asyncio
        from types import SimpleNamespace

        from coordinare.services.kubernetes_egress import probe_network_policy_enforcement

        class FakeCore:
            def create_namespaced_pod(self, *, namespace, body):
                return body

            def read_namespaced_pod(self, *, name, namespace):
                if "target" in name:
                    return SimpleNamespace(
                        status=SimpleNamespace(phase="Running", pod_ip="10.0.0.9"),
                    )
                if "control" in name:
                    return SimpleNamespace(
                        status=SimpleNamespace(phase="Succeeded", pod_ip="10.0.0.8"),
                    )
                return SimpleNamespace(status=SimpleNamespace(phase=denied_phase, pod_ip=None))

            def read_namespaced_pod_log(self, *, name, namespace):
                return "ATTEMPTED\nREACHED" if "control" in name else denied_logs

            def delete_namespaced_pod(self, **kwargs):
                return None

        class FakeNet:
            def create_namespaced_network_policy(self, **kwargs):
                return None

            def delete_namespaced_network_policy(self, **kwargs):
                return None

        return asyncio.run(
            probe_network_policy_enforcement(
                core_v1=FakeCore(),
                networking_v1=FakeNet(),
                namespace="ns",
                image="img",
                timeout_s=4,
            ),
        )

    def test_a_restricted_pod_that_never_ran_is_inconclusive(self) -> None:
        """Pending forever: unschedulable, ImagePullBackOff, out of quota."""
        verdict = self._run(denied_phase="Pending")
        assert not verdict.enforced, (
            "a Pod that never started produces no traffic; calling that 'blocked' "
            "reports containment the cluster does not provide"
        )
        assert not verdict.conclusive

    def test_a_pod_that_crashed_before_trying_is_inconclusive(self) -> None:
        """Ran, but never got as far as the connection.

        An import error, an OOM kill, a bad command: the Pod reaches a terminal
        phase with logs that contain no REACHED, which looked identical to "the
        network stopped it". Found by the review after I had already closed the
        never-started case — the same fail-open one level in.
        """
        verdict = self._run(denied_phase="Failed", denied_logs="Traceback: ModuleNotFoundError")
        assert not verdict.enforced
        assert not verdict.conclusive

    def test_a_restricted_pod_that_ran_and_could_not_connect_is_enforcement(self) -> None:
        """The only shape that counts as evidence: it tried, and was stopped."""
        verdict = self._run(denied_phase="Failed", denied_logs="ATTEMPTED\nURLError: timed out")
        assert verdict.enforced
        assert verdict.conclusive

    def test_a_restricted_pod_that_ran_and_connected_is_not_enforcement(self) -> None:
        verdict = self._run(denied_phase="Succeeded", denied_logs="ATTEMPTED\nREACHED")
        assert not verdict.enforced
        assert verdict.conclusive, "this IS a measurement — the answer is just 'no'"

    def test_the_two_ways_of_being_inconclusive_are_distinguishable(self) -> None:
        """An operator should be able to tell which half failed.

        A control that never worked and a restricted Pod that never ran both mean
        "unknown", but they need different fixes.
        """
        never_ran = self._run(denied_phase="Pending")
        assert never_ran.baseline_reachable, "the control DID work in this scenario"
        assert not never_ran.measured_under_policy


class TestCleanupIsExhaustiveAndLoud:
    """Teardown that stops early, or hides its failures, is its own defect.

    Both found by review. Catching only ``ApiException`` meant a socket timeout
    during teardown aborted the loop and left everything after it behind; and
    swallowing failures silently meant an operator would find probe Pods in their
    namespace with nothing to explain them.
    """

    def test_a_failure_deleting_one_resource_does_not_abandon_the_rest(self) -> None:
        import asyncio
        from types import SimpleNamespace

        from coordinare.services.kubernetes_egress import probe_network_policy_enforcement

        deleted: list[str] = []

        class FakeCore:
            def create_namespaced_pod(self, *, namespace, body):
                return body

            def read_namespaced_pod(self, *, name, namespace):
                if "target" in name:
                    return SimpleNamespace(
                        status=SimpleNamespace(phase="Running", pod_ip="10.0.0.9"),
                    )
                return SimpleNamespace(status=SimpleNamespace(phase="Succeeded", pod_ip=None))

            def read_namespaced_pod_log(self, *, name, namespace):
                return "ATTEMPTED\nREACHED"

            def delete_namespaced_pod(self, *, name, namespace):
                # Not an ApiException: a bare connection error, which the client
                # does not wrap and which used to abort the whole loop.
                if "target" in name:
                    raise ConnectionError("api server went away")
                deleted.append(name)

        class FakeNet:
            def create_namespaced_network_policy(self, **kwargs):
                return None

            def delete_namespaced_network_policy(self, *, name, namespace):
                deleted.append(name)

        asyncio.run(
            probe_network_policy_enforcement(
                core_v1=FakeCore(),
                networking_v1=FakeNet(),
                namespace="ns",
                image="img",
                timeout_s=4,
            ),
        )

        assert any("control" in d for d in deleted), (
            "a failure deleting the first Pod abandoned the ones after it"
        )
        assert any("coordinare-np-probe" in d for d in deleted), (
            "the NetworkPolicy was left behind when a Pod deletion failed"
        )


class TestTheProbeDoesNotConcludeFromASingleReach:
    """Spec 154 / issue #233 — applying a policy is not the same as it being programmed.

    The probe applied a deny-all and immediately measured. In the window before the
    CNI had programmed the rule, traffic flowed, and the probe read that as "this
    cluster does not enforce NetworkPolicy". Observed once in a full-suite run, then
    absent on a re-run — a flake whose content was the probe being wrong.

    The two cases are distinguishable over time and not at a point: a cluster that
    does not enforce lets traffic through every time, one whose policy had not landed
    lets it through once. So a reach under policy is measured again.
    """

    @staticmethod
    def _run(denied_sequence: list[tuple[str, str]]):
        """Drive the probe with a scripted outcome per denied Pod, in creation order.

        ``denied_sequence`` is [(phase, logs), ...]. Running out of entries means the
        probe measured more times than the test expected, which is itself a failure —
        an unbounded retry would turn "does not enforce" into a timeout.
        """
        import asyncio
        from types import SimpleNamespace

        from coordinare.services.kubernetes_egress import probe_network_policy_enforcement

        created_denied: list[str] = []
        deleted: list[str] = []

        class FakeCore:
            def create_namespaced_pod(self, *, namespace, body):
                name = body["metadata"]["name"]
                if "denied" in name:
                    created_denied.append(name)
                return body

            def _denied_script(self, name):
                idx = created_denied.index(name)
                assert idx < len(denied_sequence), (
                    f"the probe measured under policy {idx + 1} times; the test scripted "
                    f"{len(denied_sequence)}. An unbounded retry makes 'does not enforce' "
                    "arrive as a timeout instead of an answer."
                )
                return denied_sequence[idx]

            def read_namespaced_pod(self, *, name, namespace):
                if "target" in name:
                    return SimpleNamespace(
                        status=SimpleNamespace(phase="Running", pod_ip="10.0.0.9"),
                    )
                if "control" in name:
                    return SimpleNamespace(
                        status=SimpleNamespace(phase="Succeeded", pod_ip="10.0.0.8"),
                    )
                phase, _ = self._denied_script(name)
                return SimpleNamespace(status=SimpleNamespace(phase=phase, pod_ip=None))

            def read_namespaced_pod_log(self, *, name, namespace):
                if "control" in name:
                    return "ATTEMPTED\nREACHED"
                return self._denied_script(name)[1]

            def delete_namespaced_pod(self, *, name, **kwargs):
                deleted.append(name)
                return

        class FakeNet:
            def create_namespaced_network_policy(self, **kwargs):
                return None

            def delete_namespaced_network_policy(self, **kwargs):
                return None

        verdict = asyncio.run(
            probe_network_policy_enforcement(
                core_v1=FakeCore(),
                networking_v1=FakeNet(),
                namespace="ns",
                image="img",
                timeout_s=4,
            ),
        )
        return verdict, created_denied, deleted

    REACHED = ("Succeeded", "ATTEMPTED\nREACHED")
    BLOCKED = ("Succeeded", "ATTEMPTED")
    NEVER_RAN = ("Pending", "")

    def test_a_policy_that_lands_late_is_still_enforcement(self) -> None:
        """SC-001 — the defect. Reached once, blocked after: the cluster does enforce."""
        verdict, denied, _ = self._run([self.REACHED, self.BLOCKED])

        assert verdict.enforced, (
            "the probe concluded from the measurement taken before the CNI had "
            "programmed the policy, and told the operator they have no protection"
        )
        assert len(denied) == 2, "a reach under policy must be measured again"

    def test_a_cluster_that_never_enforces_is_still_reported(self) -> None:
        """SC-002 — the over-correction guard.

        Turning a true negative into "inconclusive" would trade a rare wrong answer
        for a common useless one. kindnet genuinely does not enforce egress, and an
        operator on kindnet needs to be told so.
        """
        verdict, denied, _ = self._run([self.REACHED, self.REACHED])

        assert not verdict.enforced
        assert verdict.conclusive, "reaching twice is an answer, not an absence of one"
        assert len(denied) == 2

    def test_blocked_first_time_costs_no_second_measurement(self) -> None:
        """FR-003 — the common path on a working cluster must not get slower."""
        verdict, denied, _ = self._run([self.BLOCKED])

        assert verdict.enforced
        assert len(denied) == 1, "nothing needed re-measuring; the first answer was the answer"

    def test_a_re_measurement_that_never_ran_is_inconclusive(self) -> None:
        """FR-005 — silence is not containment, on the second Pod as on the first."""
        verdict, _denied, _ = self._run([self.REACHED, self.NEVER_RAN])

        assert not verdict.enforced
        assert not verdict.measured_under_policy
        assert not verdict.conclusive

    def test_the_second_pod_is_cleaned_up(self) -> None:
        """SC-005 — re-measuring means more to clean, on every path."""
        _, denied, deleted = self._run([self.REACHED, self.REACHED])

        assert len(denied) == 2
        for name in denied:
            assert name in deleted, f"{name} was left behind in the namespace"

    def test_each_outcome_says_what_was_observed(self) -> None:
        """FR-009 / SC-003 — a probe that quietly retried would be one nobody could audit."""
        late, _, _ = self._run([self.REACHED, self.BLOCKED])
        never, _, _ = self._run([self.BLOCKED])
        open_, _, _ = self._run([self.REACHED, self.REACHED])

        details = {late.detail, never.detail, open_.detail}
        assert len(details) == 3, "three different observations must not read identically"
        assert "again" in late.detail or "second" in late.detail or "settl" in late.detail, (
            "the operator should be told the first attempt got through"
        )

    def test_a_failed_confirmation_says_what_it_saw(self) -> None:
        """Raised in review as "you already have proof"; kept inconclusive on purpose.

        A first reach looks like enough evidence, and it is not: an unprogrammed
        policy and an unenforced one are identical at that instant, which is why the
        confirming measurement exists. Concluding from the first reach alone would be
        the original bug reached by a different route. So the verdict stays
        inconclusive and the detail says exactly what happened, rather than reusing
        the generic "the Pod never ran" wording that fits the other case.
        """
        verdict, _, _ = self._run([self.REACHED, self.NEVER_RAN])
        first_never, _, _ = self._run([self.NEVER_RAN])

        assert not verdict.conclusive
        assert verdict.detail != first_never.detail, (
            "the two inconclusive outcomes saw different things and must not read alike"
        )
        assert "got through" in verdict.detail, "the operator should be told what was observed"
        assert "never ran" in verdict.detail, "and what was missing"
