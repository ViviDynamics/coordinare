"""Issue #489 — kubernetes transport: ephemeral performer pods leak when the
job never reports terminal.

Two boundaries are pinned here:

1. Release-time teardown. ``daemon._merge_session_results`` is the single
   place sessions are removed (completed or missing-card-evicted). Every
   removal must first ask the session's performer service to stop the pod
   still tracked for ``agent_dispatch.session_id`` — keyed identically to
   ``HTTPPerformerService._active_jobs`` and the ``coordinare.session_id``
   pod label.
2. Kubernetes orphan sweep. The startup reconciliation seam gains a
   kubernetes counterpart to the Docker FR-005 sweep: delete managed pods
   whose ``coordinare.session_id`` is not among the daemon's active
   sessions, keeping in-flight sessions and live documenting sides.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services.kubernetes_runtime import KubernetesRuntime

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """Stands in for DockerRuntime/KubernetesRuntime on the service."""

    def __init__(self) -> None:
        self.stopped: list[str] = []

    async def stop(self, handle: str, **_kw: Any) -> None:
        self.stopped.append(handle)


class _FakeService:
    """Performer service exposing the release seam the daemon calls."""

    def __init__(self) -> None:
        self.released: list[str] = []
        self.fail = False

    async def release_session(self, session_id: str) -> None:
        if self.fail:
            raise RuntimeError("k8s delete failed")
        self.released.append(session_id)


class _FakeSweepRuntime:
    """KubernetesRuntime stand-in exposing the session sweep."""

    def __init__(self) -> None:
        self.calls: list[set[str]] = []
        self.result: list[str] = []

    async def sweep_orphaned_sessions(self, active_session_ids: set[str]) -> list[str]:
        self.calls.append(set(active_session_ids))
        return list(self.result)


class _FakeCoreV1:
    """CoreV1Api stand-in driving KubernetesRuntime through to_thread."""

    def __init__(self, pods: list[Any]) -> None:
        self._pods = pods
        self.list_selectors: list[str] = []
        self.deleted: list[str] = []

    def list_namespaced_pod(
        self, namespace: str = "default", label_selector: str = "",
    ) -> Any:
        self.list_selectors.append(label_selector)
        return SimpleNamespace(items=self._pods)

    def delete_namespaced_pod(
        self, name: str = "", namespace: str = "default",
        grace_period_seconds: int = 10,
    ) -> Any:
        self.deleted.append(name)
        return SimpleNamespace(status="Success")

    def read_namespaced_pod(self, name: str = "", namespace: str = "default") -> Any:
        from kubernetes.client.rest import ApiException

        raise ApiException(status=404, reason="Not Found")


def _pod(name: str, labels: dict[str, str]) -> Any:
    return SimpleNamespace(metadata=SimpleNamespace(name=name, labels={"coordinare.vividynamics.com/owner": "test-deployment", **labels}))


# ---------------------------------------------------------------------------
# Fix 1 — release-time teardown
# ---------------------------------------------------------------------------


async def test_release_session_stops_tracked_ephemeral_job() -> None:
    """release_session(session_id) stops the pod still tracked for that
    coordinare session and pops the bookkeeping — even though the job never
    reported terminal."""
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.http_performer_service import (
        HTTPPerformerService,
        _EphemeralJob,
    )

    config = PerformerEndpointConfig.model_validate({
        "id": "perf-p1", "mode": "persistent", "roles": ["implementing"],
        "image": "performer:base", "endpoint": "http://127.0.0.1:8080",
    })
    runtime = _FakeRuntime()
    svc = HTTPPerformerService(config, runtime=runtime)
    svc._active_jobs["sess-1"] = _EphemeralJob(
        container_id="pod-abc", endpoint="http://127.0.0.1:8080", client=None,
    )

    await svc.release_session("sess-1")

    assert runtime.stopped == ["pod-abc"]
    assert "sess-1" not in svc._active_jobs

    # Idempotent: releasing an unknown / already-released session is a no-op.
    await svc.release_session("sess-1")
    assert runtime.stopped == ["pod-abc"]


def _completed_state(
    service: Any, session_id: str | None = "sess-1",
) -> tuple[dict, dict, Any]:
    state: dict[str, Any] = {
        "performer_services": {"implementing": service},
        "active_sessions": {},
    }
    sess: dict[str, Any] = {
        "phase": "idle",
        "current_card": None,
        "performer_stage": "implementing",
    }
    if session_id is not None:
        sess["agent_dispatch"] = {"session_id": session_id}
    active_sessions: dict[str, Any] = {"PVTI_1": sess}
    return state, active_sessions, sess


async def _run_merge(state: dict, active_sessions: dict, sess: dict) -> list:
    from coordinare.daemon import (
        AsyncSessionTickResult,
        SessionEligibility,
        _merge_fanout_results,
    )

    results = [
        AsyncSessionTickResult(
            card_id="PVTI_1", ok=True, session_state=dict(sess),
        ),
    ]
    eligibilities = {
        "PVTI_1": SessionEligibility(card_id="PVTI_1", eligible=True, reason=""),
    }
    await _merge_fanout_results(state, active_sessions, eligibilities, results)
    return results


@pytest.mark.asyncio
async def test_completed_session_release_stops_tracked_pod() -> None:
    """A session reaching the release point must have its pod stopped before
    the session dict is dropped."""
    service = _FakeService()
    state, active_sessions, sess = _completed_state(service, "sess-1")

    await _run_merge(state, active_sessions, sess)

    assert service.released == ["sess-1"]
    assert "PVTI_1" not in active_sessions


@pytest.mark.asyncio
async def test_missing_card_eviction_releases_pod() -> None:
    """The skipped/missing-card eviction path releases too — eviction is a
    release path, not a leak."""
    from coordinare.daemon import (
        MISSING_CARD,
        AsyncSessionTickResult,
        SessionEligibility,
        _merge_fanout_results,
    )

    service = _FakeService()
    state: dict[str, Any] = {
        "performer_services": {"implementing": service},
        "active_sessions": {},
    }
    sess = {
        "phase": "monitoring_performer",
        "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "sess-2"},
    }
    active_sessions: dict[str, Any] = {"PVTI_2": sess}
    results = [
        AsyncSessionTickResult(
            card_id="PVTI_2", ok=False, session_state={}, skipped=True,
        ),
    ]
    eligibilities = {
        "PVTI_2": SessionEligibility(
            card_id="PVTI_2", eligible=False, reason=MISSING_CARD,
        ),
    }

    await _merge_fanout_results(state, active_sessions, eligibilities, results)

    assert service.released == ["sess-2"]
    assert "PVTI_2" not in active_sessions


@pytest.mark.asyncio
async def test_release_without_session_id_is_a_noop() -> None:
    """No agent_dispatch.session_id → nothing to release; the session is
    still removed."""
    service = _FakeService()
    state, active_sessions, sess = _completed_state(service, None)

    await _run_merge(state, active_sessions, sess)

    assert service.released == []
    assert "PVTI_1" not in active_sessions


@pytest.mark.asyncio
async def test_release_failure_does_not_block_session_removal() -> None:
    """A teardown failure is logged and swallowed; the session must not
    linger because its pod could not be stopped."""
    service = _FakeService()
    service.fail = True
    state, active_sessions, sess = _completed_state(service, "sess-1")

    await _run_merge(state, active_sessions, sess)

    assert "PVTI_1" not in active_sessions


@pytest.mark.asyncio
async def test_release_prefers_performer_id_keyed_service() -> None:
    """SlotManager may dispatch through any service in a stage pool, while
    performer_services[stage] is only the stage default. The dispatch result
    carries performer_id, so the ID-keyed registry is the authoritative
    service for release — Copilot round 1."""
    wrong = _FakeService()
    right = _FakeService()
    state: dict[str, Any] = {
        "performer_services": {"implementing": wrong},
        "performer_services_by_id": {"perf-b": right},
        "active_sessions": {},
    }
    sess = {
        "phase": "idle",
        "current_card": None,
        "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "sess-1", "performer_id": "perf-b"},
    }
    active_sessions: dict[str, Any] = {"PVTI_1": sess}

    await _run_merge(state, active_sessions, sess)

    assert right.released == ["sess-1"]
    assert wrong.released == []
    assert "PVTI_1" not in active_sessions


# ---------------------------------------------------------------------------
# Fix 2 — kubernetes orphan sweep
# ---------------------------------------------------------------------------


def _sweep_state(active_sessions: dict) -> dict:
    return {"active_sessions": active_sessions, "performer_services": {}}


@pytest.mark.asyncio
async def test_runtime_sweep_deletes_only_orphan_pods() -> None:
    """Pods whose session id is in the keep-set survive; the rest are stopped
    and reported."""
    core = _FakeCoreV1([
        _pod("pod-keep", {"coordinare.session_id": "sess-1"}),
        _pod("pod-orphan", {"coordinare.session_id": "sess-9"}),
    ])
    runtime = KubernetesRuntime(core_v1=core, owner="test-deployment")

    swept = await runtime.sweep_orphaned_sessions({"sess-1"})

    assert core.deleted == ["pod-orphan"]
    assert swept == ["pod-orphan"]


@pytest.mark.asyncio
async def test_runtime_sweep_selector_requires_managed_and_session_labels() -> None:
    """The selector must require BOTH managed-by=coordinare and the existence
    of a coordinare.session_id label — the same controller-safety lesson
    cleanup_orphaned already learned."""
    core = _FakeCoreV1([])
    runtime = KubernetesRuntime(core_v1=core, owner="test-deployment")

    await runtime.sweep_orphaned_sessions(set())

    selector = core.list_selectors[0]
    assert "app.kubernetes.io/managed-by=coordinare" in selector
    assert "coordinare.session_id" in selector


@pytest.mark.asyncio
async def test_runtime_sweep_survives_api_errors() -> None:
    """A failing list call yields an empty sweep, not a crash."""
    from kubernetes.client.rest import ApiException

    class _BrokenApi:
        def list_namespaced_pod(self, **_kw: Any) -> Any:
            raise ApiException(status=500, reason="Internal Server Error")

    runtime = KubernetesRuntime(core_v1=_BrokenApi(), owner="test-deployment")

    swept = await runtime.sweep_orphaned_sessions({"sess-1"})

    assert swept == []


@pytest.mark.asyncio
async def test_collect_active_session_ids_mirrors_docker_keep_set() -> None:
    """In-flight sessions plus live documenting sides are kept; terminal
    sessions without dispatches are not."""
    from coordinare.services.reconciliation import collect_active_session_ids

    state = {
        "active_sessions": {
            "PVTI_1": {
                "phase": "monitoring_performer",
                "agent_dispatch": {"session_id": "sess-1"},
            },
            "PVTI_2": {
                "phase": "monitoring_performer",
                "documenting_side": {
                    "session_id": "sess-side",
                    "status": "running",
                },
            },
            "PVTI_3": {
                "phase": "idle",
                "agent_dispatch": {"session_id": "sess-stale"},
            },
        },
    }

    ids = collect_active_session_ids(state)

    assert ids == {"sess-1", "sess-side"}


@pytest.mark.asyncio
async def test_kubernetes_orphan_sweep_delegates_to_runtime() -> None:
    """The reconciliation sweep collects the keep-set and hands it to the
    runtime."""
    from coordinare.services.reconciliation import run_kubernetes_orphan_sweep

    runtime = _FakeSweepRuntime()
    runtime.result = ["pod-orphan"]
    state = _sweep_state({
        "PVTI_1": {
            "phase": "monitoring_performer",
            "agent_dispatch": {"session_id": "sess-1"},
        },
    })
    state["performer_services"] = {
        "implementing": SimpleNamespace(_runtime=runtime),
    }

    swept = await run_kubernetes_orphan_sweep(state)

    assert runtime.calls == [{"sess-1"}]
    assert swept == ["pod-orphan"]


@pytest.mark.asyncio
async def test_kubernetes_orphan_sweep_skips_when_no_active_sessions() -> None:
    """An empty keep-set mirrors the Docker SC-002 fast path: sweep nothing,
    because we cannot tell a true orphan from work whose snapshot failed."""
    from coordinare.services.reconciliation import run_kubernetes_orphan_sweep

    runtime = _FakeSweepRuntime()
    state = _sweep_state({})
    state["performer_services"] = {
        "implementing": SimpleNamespace(_runtime=runtime),
    }

    swept = await run_kubernetes_orphan_sweep(state)

    assert runtime.calls == []
    assert swept == []


@pytest.mark.asyncio
async def test_kubernetes_orphan_sweep_without_runtime_is_a_noop() -> None:
    """No kubernetes runtime in the pool (docker/subprocess deployments) →
    no sweep, no crash."""
    from coordinare.services.reconciliation import run_kubernetes_orphan_sweep

    state = _sweep_state({
        "PVTI_1": {
            "phase": "monitoring_performer",
            "agent_dispatch": {"session_id": "sess-1"},
        },
    })
    state["performer_services"] = {
        "implementing": SimpleNamespace(_runtime=object()),
    }

    swept = await run_kubernetes_orphan_sweep(state)

    assert swept == []


@pytest.mark.asyncio
async def test_runtime_finder_scans_id_keyed_services() -> None:
    """Mixed pools keep the legacy subprocess service as the stage default
    and key the HTTP services by performer id — the finder must find the
    kubernetes runtime through the ID-keyed registry too (Copilot round 1)."""
    from coordinare.services.reconciliation import find_kubernetes_sweep_runtime

    runtime = _FakeSweepRuntime()
    state = {
        "performer_services": {"implementing": object()},  # legacy default
        "performer_services_by_id": {"perf-b": SimpleNamespace(_runtime=runtime)},
    }

    assert find_kubernetes_sweep_runtime(state) is runtime


@pytest.mark.asyncio
async def test_startup_reconciliation_pass_runs_kubernetes_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon seam: the kubernetes sweep runs at boot, after the Docker
    pass, finding the runtime through performer_services — and it runs even
    when the Docker reconciliation reports unreachable."""
    from coordinare.daemon import CoordinareDaemon
    from coordinare.services.docker_executor import DockerUnreachableError

    class _DownExec:
        async def list_containers_by_label(self, *a: Any, **kw: Any) -> Any:
            raise DockerUnreachableError("test")

    monkeypatch.setattr(
        "coordinare.services.docker_executor.DockerExecutor", _DownExec,
    )

    runtime = _FakeSweepRuntime()
    state: dict[str, Any] = {
        "active_sessions": {
            "PVTI_1": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "sess-1"},
            },
        },
        "performer_services": {
            "implementing": SimpleNamespace(_runtime=runtime),
        },
    }
    daemon = CoordinareDaemon.__new__(CoordinareDaemon)
    daemon._state = state

    await daemon._startup_reconciliation_pass()

    assert runtime.calls == [{"sess-1"}]
