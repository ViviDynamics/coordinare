"""Issue #221 — performer Pod lifecycle under churn and contention.

Kubernetes gives coordinare a failure mode Docker structurally cannot have:
``pod_name_for`` is deterministic, so a Pod that outlives its stop collides with
the *next* start of that performer, while Docker frees a container name the
instant it is removed.

Two spec-146 bugs both funnelled into that collision — a Pod leaked when
readiness failed, and ``_await_pod_gone`` returning on any API error, reporting
deletion of a Pod still terminating. Both are fixed. What was missing is evidence
they hold when starts and stops overlap, rather than one at a time in a quiet
cluster.

These run against a fake API that models the parts of Pod lifecycle that matter
here — name uniqueness, deletion taking time, a terminating Pod still existing.
A real cluster produces those interleavings by luck; a fake produces them on
demand, which is the difference between testing the race and hoping to meet it.
The live-cluster counterpart is ``tests/integration/test_221_pod_churn_live.py``.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from coordinare.services import performer_lifecycle
from coordinare.services.kubernetes_runtime import KubernetesRuntime, pod_name_for


class FakeCluster:
    """An in-memory stand-in for the parts of the Pod API this exercises.

    Deliberately models **deletion as a process rather than an event**: a deleted
    Pod stays readable, in a terminating state, for ``termination_seconds``. That
    single behaviour is what the deterministic-name collision depends on, and
    what a fake that removed Pods instantly would hide.
    """

    def __init__(self, *, termination_seconds: float = 0.3, pod_ip: str = "10.244.0.5") -> None:
        self._pods: dict[str, dict] = {}
        self._termination_seconds = termination_seconds
        self._pod_ip = pod_ip
        self._lock = threading.Lock()
        self.create_conflicts = 0  # the collision this whole file exists to prevent

    # -- API surface used by KubernetesRuntime ---------------------------------

    def create_namespaced_pod(self, *, namespace, body):
        from kubernetes.client.rest import ApiException

        name = body["metadata"]["name"]
        with self._lock:
            if name in self._pods:
                self.create_conflicts += 1
                raise ApiException(status=409, reason="AlreadyExists")
            self._pods[name] = {"labels": body["metadata"].get("labels", {}), "deleting_at": None}
        return body

    def read_namespaced_pod(self, *, name, namespace):
        from kubernetes.client.rest import ApiException

        with self._lock:
            pod = self._pods.get(name)
            if pod is None:
                raise ApiException(status=404, reason="NotFound")
            if pod["deleting_at"] is not None:
                if _now() >= pod["deleting_at"]:
                    del self._pods[name]
                    raise ApiException(status=404, reason="NotFound")
                phase = "Running"  # terminating Pods still report Running
            else:
                phase = "Running"
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name),
            status=SimpleNamespace(
                phase=phase, pod_ip=self._pod_ip, reason="", container_statuses=[],
            ),
        )

    def list_namespaced_pod(self, *, namespace, label_selector=None, **kwargs):
        with self._lock:
            items = [
                SimpleNamespace(metadata=SimpleNamespace(name=n, labels=p["labels"]))
                for n, p in self._pods.items()
                if _matches(label_selector, p["labels"])
            ]
        return SimpleNamespace(items=items)

    def delete_namespaced_pod(self, *, name, namespace, **kwargs):
        from kubernetes.client.rest import ApiException

        with self._lock:
            pod = self._pods.get(name)
            if pod is None:
                raise ApiException(status=404, reason="NotFound")
            if pod["deleting_at"] is None:
                pod["deleting_at"] = _now() + self._termination_seconds
        return

    def read_namespaced_pod_log(self, *, name, namespace, **kwargs):
        return "fake log"

    # -- inspection ------------------------------------------------------------

    def live_pods(self) -> list[str]:
        """Pods that exist, terminating included — a leak is still a leak."""
        with self._lock:
            return [n for n, p in self._pods.items() if p["deleting_at"] is None]


def _now() -> float:
    import time

    return time.monotonic()


def _matches(label_selector: str | None, labels: dict) -> bool:
    if not label_selector:
        return True
    for clause in label_selector.split(","):
        key, _, value = clause.partition("=")
        if labels.get(key.strip()) != value.strip():
            return False
    return True


def _config(performer_id: str, **overrides):
    base = {
        "id": performer_id,
        "image": "coordinare-performer:base",
        "env": {},
        "readiness_timeout_s": 5,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRapidRestartOfTheSamePerformer:
    """The collision Docker never has."""

    def test_start_stop_start_never_collides(self) -> None:
        """``stop`` must not return until the Pod is really gone.

        If it returns while the Pod is still terminating, the next start reuses
        the same deterministic name and fails to create at all — so a performer
        that merely finished its work becomes one that cannot be started again.
        """
        cluster = FakeCluster(termination_seconds=0.4)
        runtime = KubernetesRuntime(core_v1=cluster)

        async def churn():
            for _ in range(6):
                started = await runtime.start_ephemeral(_config("impl"))
                await runtime.stop(started.handle, performer_id="impl")

        asyncio.run(churn())

        assert cluster.create_conflicts == 0, (
            f"{cluster.create_conflicts} restarts collided with a Pod that had not "
            "finished terminating — stop() returned too early"
        )
        assert cluster.live_pods() == [], f"Pods leaked: {cluster.live_pods()}"

    def test_the_pod_is_actually_gone_when_stop_returns(self) -> None:
        """Stated directly rather than inferred from the absence of a collision."""
        cluster = FakeCluster(termination_seconds=0.3)
        runtime = KubernetesRuntime(core_v1=cluster)

        async def scenario():
            started = await runtime.start_ephemeral(_config("impl"))
            await runtime.stop(started.handle, performer_id="impl")
            return started.handle

        handle = asyncio.run(scenario())
        assert handle not in cluster.live_pods()


class TestThisSuiteHasTeeth:
    """Proof the churn test would notice the defect it is written for.

    A test that passes whether or not the code is correct is worse than no test,
    because it reads as coverage. This session produced two of those before
    catching them, so the check is written down rather than performed once and
    remembered.

    Deliberately expressed as "break it and watch the assertion fail" rather than
    as a claim in a docstring.
    """

    def test_an_early_returning_stop_is_caught_by_the_churn_scenario(self) -> None:
        class StopThatDoesNotWait(KubernetesRuntime):
            """``stop`` as it would behave without ``_await_pod_gone`` blocking."""

            async def _await_pod_gone(self, pod_name: str, *, timeout_s: int) -> None:
                return

        cluster = FakeCluster(termination_seconds=1.5)
        runtime = StopThatDoesNotWait(core_v1=cluster)

        async def churn():
            for _ in range(6):
                started = await runtime.start_ephemeral(_config("impl"))
                await runtime.stop(started.handle, performer_id="impl")

        with pytest.raises(performer_lifecycle.ContainerStartError):
            asyncio.run(churn())

        assert cluster.create_conflicts > 0, (
            "the churn scenario did not notice a stop() that returns before the Pod "
            "is gone, so it would not have caught the bug it exists for"
        )


class TestConcurrentDistinctPerformers:
    """``PerformerRuntime`` documents that implementations must be safe here.

    Coordinare runs several cards at once, so this is the ordinary case rather
    than an edge one — and nothing proved it.
    """

    def test_many_performers_start_and_stop_concurrently(self) -> None:
        cluster = FakeCluster(termination_seconds=0.2)
        runtime = KubernetesRuntime(core_v1=cluster)

        async def scenario():
            ids = [f"perf-{i}" for i in range(12)]
            started = await asyncio.gather(*(runtime.start_ephemeral(_config(i)) for i in ids))
            assert len({s.handle for s in started}) == len(ids), (
                "distinct performers must get distinct Pod names"
            )
            await asyncio.gather(
                *(runtime.stop(s.handle, performer_id=i) for s, i in zip(started, ids, strict=True)),
            )

        asyncio.run(scenario())
        assert cluster.create_conflicts == 0
        assert cluster.live_pods() == []

    def test_one_performer_restarting_does_not_disturb_its_neighbours(self) -> None:
        """A stop must not reach past the performer it was called for."""
        cluster = FakeCluster(termination_seconds=0.2)
        runtime = KubernetesRuntime(core_v1=cluster)

        async def scenario():
            long_lived = await asyncio.gather(
                *(runtime.start_ephemeral(_config(f"stable-{i}")) for i in range(4)),
            )
            for _ in range(4):
                churned = await runtime.start_ephemeral(_config("churner"))
                await runtime.stop(churned.handle, performer_id="churner")
            return {s.handle for s in long_lived}

        expected = asyncio.run(scenario())
        assert set(cluster.live_pods()) == expected, (
            "churning one performer removed or disturbed another's Pod"
        )


class TestFailedStartsUnderLoad:
    """A leaked Pod is worse than a failed start: it blocks every later one."""

    def test_every_pod_from_a_failed_start_is_removed(self) -> None:
        class NeverReady(FakeCluster):
            def read_namespaced_pod(self, *, name, namespace):
                pod = super().read_namespaced_pod(name=name, namespace=namespace)
                pod.status.pod_ip = None  # Pending forever
                pod.status.phase = "Pending"
                return pod

        cluster = NeverReady(termination_seconds=0.1)
        runtime = KubernetesRuntime(core_v1=cluster)

        async def scenario():
            results = await asyncio.gather(
                *(
                    runtime.start_ephemeral(_config(f"doomed-{i}", readiness_timeout_s=1))
                    for i in range(5)
                ),
                return_exceptions=True,
            )
            assert all(isinstance(r, Exception) for r in results), (
                "a Pod that never gets an IP must not report a successful start"
            )

        asyncio.run(scenario())
        assert cluster.live_pods() == [], (
            f"failed starts leaked {cluster.live_pods()} — the next start of each of "
            "those performers would collide with its own corpse"
        )

    def test_a_performer_can_start_again_after_a_failed_start(self) -> None:
        """The consequence that makes the leak matter, asserted end to end.

        An unschedulable image should be a transient problem, not one that makes
        the performer permanently unstartable.
        """
        cluster = FakeCluster(termination_seconds=0.1)
        runtime = KubernetesRuntime(core_v1=cluster)
        original = cluster.read_namespaced_pod
        pending = {"on": True}

        def maybe_pending(*, name, namespace):
            pod = original(name=name, namespace=namespace)
            if pending["on"]:
                pod.status.pod_ip = None
                pod.status.phase = "Pending"
            return pod

        cluster.read_namespaced_pod = maybe_pending

        async def scenario():
            with pytest.raises(performer_lifecycle.LifecycleError):
                await runtime.start_ephemeral(_config("impl", readiness_timeout_s=1))
            pending["on"] = False  # the image lands; the performer should recover
            return await runtime.start_ephemeral(_config("impl"))

        started = asyncio.run(scenario())
        assert started.handle == pod_name_for("impl")
        assert cluster.create_conflicts == 0, (
            "the retry collided with the failed start's Pod, so an unpullable image "
            "would have made this performer permanently unstartable"
        )


class TestATerminatingPredecessor:
    """Starting a performer whose previous Pod has not finished going away.

    Found by the live churn tests: this failed outright with "already exists".
    ``stop`` waits for the Pod *it* deleted, so the ordinary path never reaches
    here — but coordinare killed between the delete request and the Pod actually
    going, an operator deleting a Pod by hand, and an eviction all land in this
    window, and refusing work the cluster is seconds away from allowing is the
    wrong answer in every one of them.
    """

    def test_a_terminating_predecessor_is_waited_for_rather_than_refused(self) -> None:
        cluster = FakeCluster(termination_seconds=0.5)
        runtime = KubernetesRuntime(core_v1=cluster)

        # A predecessor exists and is on its way out, exactly as it would be if
        # coordinare had died between requesting deletion and the Pod going.
        name = pod_name_for("impl")
        cluster.create_namespaced_pod(namespace="default", body={"metadata": {"name": name}})
        cluster.delete_namespaced_pod(name=name, namespace="default")

        started = asyncio.run(runtime.start_ephemeral(_config("impl")))
        assert started.handle == name

    def test_a_predecessor_that_never_goes_is_reported_clearly(self) -> None:
        """Not every collision is a terminating Pod, and the difference matters.

        A Pod that is simply *running* under this name means something else owns
        it — most likely a second coordinare against the same namespace. Retrying
        into that forever would turn a configuration mistake into a hang, so it
        fails, and the message names the likely cause rather than restating the
        status code.
        """
        cluster = FakeCluster(termination_seconds=0.2)
        runtime = KubernetesRuntime(core_v1=cluster)

        name = pod_name_for("impl")
        cluster.create_namespaced_pod(namespace="default", body={"metadata": {"name": name}})

        with pytest.raises(performer_lifecycle.ContainerStartError) as excinfo:
            asyncio.run(runtime.start_ephemeral(_config("impl", readiness_timeout_s=1)))

        message = str(excinfo.value)
        assert "already exists" in message
        assert "another coordinare" in message.lower() or "stuck terminating" in message.lower(), (
            f"the failure must point at a cause, not just a status: {message}"
        )

    def test_an_unrelated_create_failure_is_not_retried(self) -> None:
        """Only a name conflict earns the wait-and-retry.

        A quota rejection or an invalid manifest is not going to resolve itself,
        and retrying it would delay a clear error by the full readiness timeout.
        """
        from kubernetes.client.rest import ApiException

        class Forbidden(FakeCluster):
            def create_namespaced_pod(self, *, namespace, body):
                raise ApiException(status=403, reason="Forbidden")

        runtime = KubernetesRuntime(core_v1=Forbidden())

        with pytest.raises(performer_lifecycle.ContainerStartError) as excinfo:
            asyncio.run(runtime.start_ephemeral(_config("impl", readiness_timeout_s=30)))
        assert "403" in str(excinfo.value)
