"""Spec 076 T059 — Reconciliation log-event contract.

Per ``specs/076-qa-cycle/contracts/reconciliation-pass.md``, the
reconciliation pass MUST emit specific structured-log events under
each branch.  These tests assert event NAMES and required field shapes,
so operators relying on grep recipes (per quickstart.md) keep working
across refactors.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.services import reconciliation as recon_mod
from coordinare.services.docker_executor import ContainerInfo, DockerUnreachableError


class _Capture:
    def __init__(self, monkeypatch) -> None:
        self.events: list[tuple[str, str, dict]] = []  # (level, event, kwargs)
        for level in ("debug", "info", "warning", "error"):
            original = getattr(recon_mod.logger, level)

            def _make(lvl: str, orig):
                def _cap(event: str, **kwargs):
                    self.events.append((lvl, event, kwargs))
                    return orig(event, **kwargs)
                return _cap

            monkeypatch.setattr(recon_mod.logger, level, _make(level, original))

    def names(self) -> list[str]:
        return [e[1] for e in self.events]

    def find(self, name: str) -> list[dict]:
        return [kwargs for _, evt, kwargs in self.events if evt == name]


class _MockDocker:
    def __init__(self, *, containers=None, healthy=True, unreachable=False) -> None:
        self._containers = list(containers or [])
        self._healthy = healthy
        self._unreachable = unreachable
        self.stopped: list[str] = []

    async def list_containers_by_label(self, label_filters, *, timeout=5.0):
        if self._unreachable:
            raise DockerUnreachableError("simulated")
        return [
            c for c in self._containers
            if all(c.labels.get(k) == v for k, v in label_filters.items())
        ]

    async def stop_container(self, container_id, *, timeout=5.0):
        self.stopped.append(container_id)
        return True

    async def port_of(self, container_id, *, internal_port=8088):
        return 55555 if self._healthy else None

    async def probe_healthz(self, container_id, *, timeout=15.0, **kw):
        return self._healthy


class _SvcCfg:
    mode: str = "ephemeral"


class _Svc:
    def __init__(self, mode: str = "ephemeral") -> None:
        self._active_jobs: dict = {}
        cfg = _SvcCfg()
        cfg.mode = mode
        self._config = cfg

    def _auth_token(self):
        return None


def _container(session_id: str, container_id: str = "ctr") -> ContainerInfo:
    return ContainerInfo(
        container_id=container_id,
        name="x",
        image="coordinare-performer:full",
        started_at=datetime(2026, 5, 28, 22, 0, 0, tzinfo=UTC),
        labels={
            "coordinare.performer.id": "claude-ephemeral",
            "coordinare.session_id": session_id,
            "coordinare.card_id": "PVTI_X",
            "coordinare.performer_stage": "implementing",
            "coordinare.daemon_started_at": "2026-05-28T21:00:00Z",
            "coordinare.spec_version": "076",
        },
    )


def _state(session_id="uuid-x"):
    return {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": session_id},
            },
        },
        "performer_services": {"implementing": _Svc()},
    }


@pytest.mark.asyncio
async def test_pass_started_event_has_required_fields(monkeypatch) -> None:
    cap = _Capture(monkeypatch)
    await recon_mod.run_startup_reconciliation(_state(), _MockDocker())
    started = cap.find("daemon.reconciliation_pass_started")
    assert started, f"missing event; saw {cap.names()}"
    fields = started[0]
    assert "cards_to_process" in fields
    assert "budget_seconds" in fields


@pytest.mark.asyncio
async def test_pass_complete_event_has_required_fields(monkeypatch) -> None:
    cap = _Capture(monkeypatch)
    await recon_mod.run_startup_reconciliation(_state(), _MockDocker())
    complete = cap.find("daemon.reconciliation_pass_complete")
    assert complete, f"missing event; saw {cap.names()}"
    fields = complete[0]
    assert "wall_clock_seconds" in fields
    assert "decisions" in fields
    assert "orphans_swept_count" in fields


@pytest.mark.asyncio
async def test_docker_unreachable_event_emitted(monkeypatch) -> None:
    cap = _Capture(monkeypatch)
    await recon_mod.run_startup_reconciliation(_state(), _MockDocker(unreachable=True))
    aborted = cap.find("daemon.reconciliation_pass_aborted_docker_unreachable")
    assert aborted, f"missing event; saw {cap.names()}"
    assert "error" in aborted[0]


@pytest.mark.asyncio
async def test_orphan_swept_event_has_required_fields(monkeypatch) -> None:
    cap = _Capture(monkeypatch)
    orphan = _container("totally-unknown-uuid", container_id="ctr-orphan")
    state = _state()  # tracked session uuid-x, doesn't match orphan
    await recon_mod.run_startup_reconciliation(state, _MockDocker(containers=[orphan]))
    swept = cap.find("daemon.orphan_swept")
    assert swept, f"missing event; saw {cap.names()}"
    fields = swept[0]
    assert fields["container_id"] == "ctr-orphan"
    assert fields["name"] == "x"
    assert fields["image"] == "coordinare-performer:full"
    assert "started_at" in fields
    assert "labels" in fields
    assert fields["stopped"] is True


@pytest.mark.asyncio
async def test_session_adopted_event_has_required_fields(monkeypatch) -> None:
    cap = _Capture(monkeypatch)
    matching = _container("uuid-x", container_id="ctr-keep")
    await recon_mod.run_startup_reconciliation(
        _state(), _MockDocker(containers=[matching], healthy=True)
    )
    adopted = cap.find("daemon.session_adopted")
    assert adopted, f"missing event; saw {cap.names()}"
    fields = adopted[0]
    assert fields["card_id"] == "PVTI_X"
    assert fields["session_id"] == "uuid-x"
    assert fields["container_id"] == "ctr-keep"
    assert "endpoint" in fields


@pytest.mark.asyncio
async def test_stale_session_reconciled_event_has_required_fields(monkeypatch) -> None:
    cap = _Capture(monkeypatch)
    # Legacy state-root shape — handle_potentially_stale_session called
    # from check_board
    state = {
        "active_sessions": {},
        "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "legacy-uuid"},
        "phase": "monitoring_performer",
        "performer_services": {"implementing": _Svc()},
    }
    decision = await recon_mod.handle_potentially_stale_session(
        state, "PVTI_LEGACY", docker_executor=_MockDocker()
    )
    reconciled = cap.find("check_board.stale_session_reconciled")
    assert reconciled, f"missing event; saw {cap.names()}"
    fields = reconciled[0]
    assert fields["card_id"] == "PVTI_LEGACY"
    assert fields["session_id"] == "legacy-uuid"
    assert "decision" in fields
    assert decision is not None


@pytest.mark.asyncio
async def test_stale_session_deferred_event_has_required_fields(monkeypatch) -> None:
    """401: a docker ps timeout is an unknown. The per-card path emits
    ``check_board.stale_session_deferred`` (WARNING) with the fields an operator
    greps for, leaves the session untouched, and only after
    DOCKER_UNREACHABLE_ESCALATION_STREAK consecutive timeouts falls back to the
    ``stale_session_reconciled`` event with reason=docker_unreachable."""
    cap = _Capture(monkeypatch)
    state = {
        "active_sessions": {},
        "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "slow-uuid"},
        "phase": "monitoring_performer",
        "performer_services": {"implementing": _Svc()},
    }
    docker = _MockDocker(unreachable=True)

    decision = await recon_mod.handle_potentially_stale_session(state, "PVTI_SLOW", docker_executor=docker)

    deferred = cap.find("check_board.stale_session_deferred")
    assert deferred, f"missing event; saw {cap.names()}"
    fields = deferred[0]
    assert fields["card_id"] == "PVTI_SLOW"
    assert fields["session_id"] == "slow-uuid"
    assert fields["decision"] == "deferred"
    assert fields["reason"] == "docker_unreachable"
    assert fields["streak"] == 1
    assert fields["escalate_at"] == recon_mod.DOCKER_UNREACHABLE_ESCALATION_STREAK
    assert str(decision) == "deferred"
    assert not cap.find("check_board.stale_session_reconciled"), "a first timeout must not emit the reconciled event"
    assert state["agent_dispatch"] == {"session_id": "slow-uuid"}

    for _ in range(recon_mod.DOCKER_UNREACHABLE_ESCALATION_STREAK - 1):
        await recon_mod.handle_potentially_stale_session(state, "PVTI_SLOW", docker_executor=docker)
    reconciled = cap.find("check_board.stale_session_reconciled")
    assert reconciled and reconciled[-1]["reason"] == "docker_unreachable"
    assert reconciled[-1]["decision"] == "fresh_dispatched"
    assert reconciled[-1]["streak"] == recon_mod.DOCKER_UNREACHABLE_ESCALATION_STREAK
