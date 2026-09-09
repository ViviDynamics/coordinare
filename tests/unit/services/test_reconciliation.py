"""Spec 076 T058 — Reconciliation pass unit tests.

Exercises each decision branch (ADOPTED, REAPED_AND_REPLACED,
FRESH_DISPATCHED, SKIPPED_PERSISTENT, ORPHAN_SWEPT) with a mocked
DockerExecutor and a stub performer service.  No live Docker daemon.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.services.docker_executor import ContainerInfo, DockerUnreachableError
from coordinare.services.reconciliation import (
    ReconciliationDecision,
    handle_potentially_stale_session,
    run_startup_reconciliation,
)


def _ts() -> datetime:
    return datetime(2026, 5, 28, 22, 0, 0, tzinfo=UTC)


def _make_container(*, session_id: str, container_id: str = "ctr-1", card_id: str = "PVTI_X") -> ContainerInfo:
    return ContainerInfo(
        container_id=container_id,
        name=f"name-{container_id}",
        image="coordinare-performer:full",
        started_at=_ts(),
        labels={
            "coordinare.performer.id": "claude-ephemeral",
            "coordinare.session_id": session_id,
            "coordinare.card_id": card_id,
            "coordinare.performer_stage": "implementing",
            "coordinare.daemon_started_at": "2026-05-28T21:14:17Z",
            "coordinare.spec_version": "076",
        },
    )


class _MockDockerExecutor:
    """Minimal in-memory mock supporting list/stop/port_of."""

    def __init__(self, *, containers: list[ContainerInfo] | None = None, port: int | None = 55555) -> None:
        self._containers = list(containers or [])
        self._port = port
        self.stopped: list[str] = []
        self.unreachable = False

    async def list_containers_by_label(self, label_filters, *, timeout=5.0):
        if self.unreachable:
            raise DockerUnreachableError("simulated")
        # Mimic Docker's AND-filter semantics
        return [
            c for c in self._containers
            if all(c.labels.get(k) == v for k, v in label_filters.items())
        ]

    async def stop_container(self, container_id, *, timeout=5.0):
        self.stopped.append(container_id)
        return True

    async def port_of(self, container_id, *, internal_port=8088):
        return self._port

    async def probe_healthz(self, container_id, *, timeout=15.0, internal_port=8088, auth_token=None):
        # Default: port resolvable → healthy.  Tests that need unhealthy
        # override by patching the instance.
        return self._port is not None


class _MockHTTPService:
    """Minimal performer-service stub for adoption tests."""

    def __init__(self, mode: str = "ephemeral") -> None:
        self._active_jobs: dict = {}
        # Mimic the real HTTPPerformerService config shape
        class _Cfg:
            pass
        cfg = _Cfg()
        cfg.mode = mode
        self._config = cfg

    def _auth_token(self):
        return None


# ---------------------------------------------------------------------------
# Top-level run_startup_reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_fast_path_when_no_in_flight_sessions() -> None:
    """SC-002: cold start with empty snapshot completes in <500ms WITHOUT
    calling Docker."""
    state: dict = {"active_sessions": {}}
    executor = _MockDockerExecutor()
    report = await run_startup_reconciliation(state, executor)
    assert report.cards_considered == 0
    assert report.decisions == {}
    assert report.orphans_swept == []
    assert executor.stopped == []
    # No Docker calls made
    assert not executor._containers


@pytest.mark.asyncio
async def test_reconciliation_fresh_dispatched_when_no_matching_container() -> None:
    """Session in snapshot, but no container with matching session_id
    label → fresh-dispatch (clear agent_dispatch)."""
    state: dict = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "missing-uuid"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }
    executor = _MockDockerExecutor(containers=[])  # no containers at all
    report = await run_startup_reconciliation(state, executor)
    assert report.decisions["PVTI_X"] == ReconciliationDecision.FRESH_DISPATCHED
    assert state["active_sessions"]["PVTI_X"]["agent_dispatch"] == {}
    assert state["active_sessions"]["PVTI_X"]["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_reconciliation_orphan_swept_when_container_has_no_matching_session() -> None:
    """Container exists on host but its session_id label is NOT in the
    snapshot's in-flight sessions → ORPHAN_SWEPT.  We need at least one
    in-flight session to enter the docker-enumeration path."""
    orphan = _make_container(session_id="totally-unknown-uuid", container_id="ctr-orphan")
    # Plus one tracked in-flight session whose container does not exist
    state: dict = {
        "active_sessions": {
            "PVTI_TRACKED": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "tracked-uuid"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }
    executor = _MockDockerExecutor(containers=[orphan])
    report = await run_startup_reconciliation(state, executor)
    # tracked session has no matching container → fresh-dispatched
    assert report.decisions["PVTI_TRACKED"] == ReconciliationDecision.FRESH_DISPATCHED
    # orphan container stopped
    assert "ctr-orphan" in report.orphans_swept
    assert "ctr-orphan" in executor.stopped


@pytest.mark.asyncio
async def test_reconciliation_skips_persistent_mode() -> None:
    """FR-011: services configured for persistent mode are reconciliation
    no-ops; no Docker calls are spent on them and the decision is
    SKIPPED_PERSISTENT (NOT fresh-dispatched / NOT reaped)."""
    state: dict = {
        "active_sessions": {
            "PVTI_PERSISTENT": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "persistent-uuid"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService(mode="persistent")},
    }
    executor = _MockDockerExecutor()
    report = await run_startup_reconciliation(state, executor)
    assert report.decisions["PVTI_PERSISTENT"] == ReconciliationDecision.SKIPPED_PERSISTENT
    # No stops called for persistent-mode sessions
    assert executor.stopped == []
    # agent_dispatch should NOT have been touched
    assert state["active_sessions"]["PVTI_PERSISTENT"]["agent_dispatch"] == {"session_id": "persistent-uuid"}


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_on_docker_unreachable() -> None:
    """FR-012: when Docker is unreachable, the report sets
    docker_unreachable=True and no per-card decisions are made.  The
    daemon's caller MUST refuse to dispatch any ephemeral performer in
    this state."""
    state: dict = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "uuid-x"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }
    executor = _MockDockerExecutor()
    executor.unreachable = True
    report = await run_startup_reconciliation(state, executor)
    assert report.docker_unreachable is True
    assert report.decisions == {}
    # state.agent_dispatch MUST NOT have been touched
    assert state["active_sessions"]["PVTI_X"]["agent_dispatch"] == {"session_id": "uuid-x"}


@pytest.mark.asyncio
async def test_reconciliation_stashes_decisions_for_notification_dedup() -> None:
    """FR-010 / contracts/notification-dedup.md: the reconciliation pass
    stashes per-card decisions in state.reconciliation_decisions_last_startup
    so notify.py can suppress duplicate card_dispatched events on
    ADOPTED outcomes."""
    state: dict = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "missing-uuid"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }
    executor = _MockDockerExecutor(containers=[])
    await run_startup_reconciliation(state, executor)
    assert state["reconciliation_decisions_last_startup"] == {
        "PVTI_X": "fresh_dispatched",
    }


# ---------------------------------------------------------------------------
# handle_potentially_stale_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_adopt_falls_back_to_reap_when_port_unresolvable() -> None:
    """Round-1 code-review #1 regression: if port_of returns None at
    adopt-time, the pass MUST fall through to reap-and-replace rather
    than leave a dead client in _active_jobs.  This catches a port
    resolution race that the first review highlighted."""
    matching = _make_container(session_id="uuid-x", container_id="ctr-keep")
    state = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "uuid-x"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }

    class _PortUnresolvable(_MockDockerExecutor):
        async def probe_healthz(self, container_id, *, timeout=15.0, **kw):
            return True  # healthy probe...

        async def port_of(self, container_id, *, internal_port=8088):
            return None  # ...but port_of fails

    executor = _PortUnresolvable(containers=[matching])
    report = await run_startup_reconciliation(state, executor)
    # Adoption attempted, failed → reap-and-replace branch fires
    assert report.decisions["PVTI_X"] == ReconciliationDecision.REAPED_AND_REPLACED
    assert "ctr-keep" in executor.stopped


@pytest.mark.asyncio
async def test_reconciliation_session_id_collision_logged_and_recent_wins(monkeypatch) -> None:
    """When two containers carry the same session_id label, the
    pass MUST keep the most-recently-started container and emit a
    collision warning."""
    from datetime import timedelta

    older = ContainerInfo(
        container_id="ctr-old",
        name="old",
        image="coordinare-performer:full",
        started_at=_ts() - timedelta(minutes=5),
        labels={
            "coordinare.session_id": "uuid-x",
            "coordinare.card_id": "PVTI_X",
            "coordinare.spec_version": "076",
            "coordinare.performer.id": "claude-ephemeral",
            "coordinare.performer_stage": "implementing",
            "coordinare.daemon_started_at": "2026-05-28T21:00:00Z",
        },
    )
    newer = _make_container(session_id="uuid-x", container_id="ctr-new")

    state = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "uuid-x"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }
    executor = _MockDockerExecutor(containers=[older, newer])
    report = await run_startup_reconciliation(state, executor)
    # Newer container kept → adopted
    assert report.decisions["PVTI_X"] == ReconciliationDecision.ADOPTED
    job = state["performer_services"]["implementing"]._active_jobs["uuid-x"]
    # The adopted container_id matches the newer one
    assert job.container_id == "ctr-new"


@pytest.mark.asyncio
async def test_reconciliation_handles_legacy_flat_wedge_window() -> None:
    """A snapshot from an earlier 076 dev build might have
    wedge_count_window as a flat list rather than dict[str, list].
    detect_wedged_state MUST tolerate this and reset to the proper
    dict shape."""
    from coordinare.services.reconciliation import detect_wedged_state

    state = {
        "active_card": {"id": "PVTI_LEGACY", "title": "Card"},
        "current_card": {"id": "PVTI_LEGACY", "title": "Card"},
        "active_sessions": {},
        "performer_stage": None,
        "phase": None,
        "wedge_count_window": [_ts()],  # legacy flat-list shape
    }
    detect_wedged_state(state)
    # Resets to dict, records the current wedge
    assert isinstance(state["wedge_count_window"], dict)
    assert "PVTI_LEGACY" in state["wedge_count_window"]


@pytest.mark.asyncio
async def test_handle_stale_session_docker_unreachable_clears() -> None:
    """When Docker is down during the per-card stale check, fall back
    to fresh-dispatch (clear agent_dispatch) and emit the
    reason=docker_unreachable log event."""
    state = {
        "active_sessions": {},
        "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "stale-uuid"},
        "phase": "monitoring_performer",
        "performer_services": {"implementing": _MockHTTPService()},
    }
    executor = _MockDockerExecutor()
    executor.unreachable = True
    decision = await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)
    assert decision == ReconciliationDecision.FRESH_DISPATCHED
    assert state["agent_dispatch"] == {}
    assert state["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_handle_stale_session_missing_session_id_returns_fresh() -> None:
    """Stale-check call with no agent_dispatch.session_id → fresh-dispatch."""
    state = {
        "active_sessions": {},
        "performer_stage": "implementing",
        "agent_dispatch": {},  # no session_id
        "phase": "monitoring_performer",
        "performer_services": {"implementing": _MockHTTPService()},
    }
    decision = await handle_potentially_stale_session(state, "PVTI_X", docker_executor=_MockDockerExecutor())
    assert decision == ReconciliationDecision.FRESH_DISPATCHED


def test_collect_in_flight_sessions_skips_non_dict_entries() -> None:
    """Defensive: malformed entries in active_sessions shouldn't crash
    _collect_in_flight_sessions."""
    from coordinare.services.reconciliation import _collect_in_flight_sessions

    state = {
        "active_sessions": {
            "PVTI_BAD": "not a dict",
            "PVTI_OK": {
                "phase": "monitoring_performer",
                "agent_dispatch": {"session_id": "uuid"},
                "performer_stage": "implementing",
            },
            "PVTI_NO_SESSION": {
                "phase": "monitoring_performer",
                "agent_dispatch": {},
                "performer_stage": "implementing",
            },
            "PVTI_WRONG_PHASE": {
                "phase": "idle",
                "agent_dispatch": {"session_id": "x"},
            },
        }
    }
    result = _collect_in_flight_sessions(state)
    assert list(result.keys()) == ["PVTI_OK"]


def test_collect_in_flight_sessions_no_active_sessions() -> None:
    """No active_sessions key → empty dict."""
    from coordinare.services.reconciliation import _collect_in_flight_sessions

    assert _collect_in_flight_sessions({}) == {}
    assert _collect_in_flight_sessions({"active_sessions": "not a dict"}) == {}


def test_wedge_window_parses_iso_strings_from_snapshot() -> None:
    """Pydantic deserializes datetime fields from JSON as datetime
    objects, but a snapshot from a different writer might leave ISO
    strings.  The window-trim path tolerates both."""
    from coordinare.services.reconciliation import detect_wedged_state

    now = datetime.now(UTC)
    state = {
        "active_card": {"id": "PVTI_X", "title": "Card"},
        "current_card": {"id": "PVTI_X", "title": "Card"},
        "active_sessions": {},
        "performer_stage": None,
        "phase": None,
        "wedge_count_window": {
            "PVTI_X": [
                now.isoformat(),  # ISO string with tz (round-trip from datetime.now(UTC))
                now.replace(tzinfo=None).isoformat(),  # naive ISO string (line 348 path)
                "garbage-not-a-date",  # malformed entry — should be skipped
                12345,  # non-string non-datetime — should be skipped
            ]
        },
    }
    detect_wedged_state(state)
    # Two valid ISO strings + the new wedge timestamp = 3 entries
    assert len(state["wedge_count_window"]["PVTI_X"]) == 3


@pytest.mark.asyncio
async def test_reconciliation_skips_containers_with_no_session_id_label() -> None:
    """A container labelled coordinare.spec_version=076 but missing
    coordinare.session_id label is malformed; the pass MUST skip it
    rather than crash."""
    no_session_id = ContainerInfo(
        container_id="ctr-malformed",
        name="malformed",
        image="coordinare-performer:full",
        started_at=_ts(),
        labels={"coordinare.performer.id": "claude-ephemeral", "coordinare.spec_version": "076"},
    )
    state = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "uuid-x"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }
    executor = _MockDockerExecutor(containers=[no_session_id])
    report = await run_startup_reconciliation(state, executor)
    # The malformed container is silently skipped — not in orphans_swept
    # because it has no session_id (collision detection key)
    assert "ctr-malformed" not in executor.stopped
    assert report.decisions["PVTI_X"] == ReconciliationDecision.FRESH_DISPATCHED


@pytest.mark.asyncio
async def test_reconciliation_keeps_newer_on_collision_iteration_order_matters() -> None:
    """When iteration order presents newer before older for the same
    session_id, the older is skipped (line 145 branch)."""
    from datetime import timedelta

    newer = _make_container(session_id="uuid-x", container_id="ctr-new")
    older = ContainerInfo(
        container_id="ctr-old",
        name="old",
        image="coordinare-performer:full",
        started_at=_ts() - timedelta(minutes=10),
        labels={
            "coordinare.session_id": "uuid-x",
            "coordinare.card_id": "PVTI_X",
            "coordinare.spec_version": "076",
            "coordinare.performer.id": "claude-ephemeral",
            "coordinare.performer_stage": "implementing",
            "coordinare.daemon_started_at": "2026-05-28T21:00:00Z",
        },
    )
    state = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "uuid-x"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }
    # Newer first, older second — older container's started_at < newer's
    executor = _MockDockerExecutor(containers=[newer, older])
    report = await run_startup_reconciliation(state, executor)
    assert report.decisions["PVTI_X"] == ReconciliationDecision.ADOPTED
    job = state["performer_services"]["implementing"]._active_jobs["uuid-x"]
    assert job.container_id == "ctr-new"


@pytest.mark.asyncio
async def test_reap_failed_on_stuck_container_continues() -> None:
    """daemon.reap_failed log fires when stop_container returns False;
    the pass MUST proceed (FR-004 best-effort)."""
    matching = _make_container(session_id="uuid-x", container_id="ctr-stuck")
    state = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "uuid-x"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }

    class _StickyExec(_MockDockerExecutor):
        async def stop_container(self, container_id, *, timeout=5.0):
            self.stopped.append(container_id)
            return False  # stuck — stop failed

        async def probe_healthz(self, container_id, *, timeout=15.0, **kw):
            return False  # unhealthy → reap path

    executor = _StickyExec(containers=[matching])
    report = await run_startup_reconciliation(state, executor)
    # Pass proceeds despite the stuck stop
    assert report.decisions["PVTI_X"] == ReconciliationDecision.REAPED_AND_REPLACED
    assert "ctr-stuck" in executor.stopped


@pytest.mark.asyncio
async def test_orphan_sweep_failure_continues() -> None:
    """daemon.reap_failed for orphan sweep — pass proceeds, reports
    container in orphans_swept only if stopped successfully."""
    orphan = _make_container(session_id="totally-unknown", container_id="ctr-orphan-stuck")
    state = {
        "active_sessions": {
            "PVTI_TRACKED": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "tracked-uuid"},
            },
        },
        "performer_services": {"implementing": _MockHTTPService()},
    }

    class _StickyExec(_MockDockerExecutor):
        async def stop_container(self, container_id, *, timeout=5.0):
            self.stopped.append(container_id)
            return False

    executor = _StickyExec(containers=[orphan])
    report = await run_startup_reconciliation(state, executor)
    # Container was attempted but not in orphans_swept (returned False)
    assert "ctr-orphan-stuck" in executor.stopped
    assert "ctr-orphan-stuck" not in report.orphans_swept


def test_is_persistent_mode_handles_missing_config() -> None:
    """Edge case: a service object with no _config attribute should
    NOT be treated as persistent."""
    from coordinare.services.reconciliation import _is_persistent_mode

    class _BareService:
        pass

    assert _is_persistent_mode(_BareService()) is False


def test_resolve_service_handles_missing_performer_services() -> None:
    """No performer_services key on state → return None."""
    from coordinare.services.reconciliation import _resolve_service

    assert _resolve_service({}, "implementing") is None
    assert _resolve_service({"performer_services": "not a dict"}, "implementing") is None


@pytest.mark.asyncio
async def test_handle_stale_session_clears_when_no_container() -> None:
    """The legacy `check_board._is_stale` path: state-level agent_dispatch
    set but no matching container → clear and fresh-dispatch."""
    state: dict = {
        "active_sessions": {},
        "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "old-stale-uuid"},
        "phase": "monitoring_performer",
        "performer_services": {"implementing": _MockHTTPService()},
    }
    executor = _MockDockerExecutor(containers=[])
    decision = await handle_potentially_stale_session(state, "PVTI_LEGACY", docker_executor=executor)
    assert decision == ReconciliationDecision.FRESH_DISPATCHED
    assert state["agent_dispatch"] == {}
    assert state["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_175_early_documenter_adopted_without_foreground_dispatch():
    from coordinare.config import PerformerEndpointConfig
    from coordinare.services.http_performer_service import HTTPPerformerService
    service = HTTPPerformerService(PerformerEndpointConfig(id="doc", roles=["documenting"], mode="ephemeral", image="example:latest"))
    sess = {"phase": "dispatching", "performer_stage": "documenting", "agent_dispatch": {},
            "documenting_side": {"status": "running", "session_id": "side-session", "job_id": "runner-job"}}
    state = {"active_sessions": {"c1": sess}, "performer_services": {"documenting": service}}
    docker = _MockDockerExecutor(containers=[_make_container(session_id="side-session")])
    report = await run_startup_reconciliation(state, docker_executor=docker)
    assert service.has_live_session("side-session")
    assert service._active_jobs["side-session"].endpoint == "http://127.0.0.1:55555"
    assert service._active_jobs["side-session"].job_id == "runner-job"
    from unittest.mock import AsyncMock
    client = service._active_jobs["side-session"].client
    client.get_job = AsyncMock(side_effect=RuntimeError("stop after URL observation"))
    with pytest.raises(RuntimeError, match="URL observation"):
        await service.check_status("side-session")
    client.get_job.assert_awaited_once_with("runner-job")
    assert not docker.stopped and not report.orphans_swept
    assert sess["agent_dispatch"] == {}
    await service._active_jobs["side-session"].client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [True, False])
async def test_175_restart_requires_confirmed_writer_stop(stopped):
    from coordinare.config import PerformerEndpointConfig
    from coordinare.services.http_performer_service import HTTPPerformerService
    service = HTTPPerformerService(PerformerEndpointConfig(id="doc", roles=["documenting"], mode="ephemeral", image="example:latest"))
    sess = {"phase": "blocked", "documenting_side": {"status": "running", "session_id": "side-session", "job_id": "runner-job"}}
    docker = _MockDockerExecutor(containers=[_make_container(session_id="side-session")], port=None)
    async def stop(*args, **kwargs):
        return stopped
    docker.stop_container = stop
    await run_startup_reconciliation({"active_sessions": {"c1": sess}, "performer_services": {"documenting": service}}, docker_executor=docker)
    assert sess["documenting_side"]["status"] == "failed"
    assert sess["documenting_side"]["writer_active"] is (not stopped)
