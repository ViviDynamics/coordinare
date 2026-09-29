"""Spec 076 T018 — Docker-label schema contract.

Asserts:
- ``start_ephemeral`` validates extra-label keys and values (FR-009).
- ``http_performer_service.dispatch_card`` constructs all 6 required
  coordinare.* labels on every ephemeral dispatch (FR-009 + contracts/docker-labels.md).
"""
from __future__ import annotations

import pytest

from coordinare.services.performer_lifecycle import (
    ContainerStartError,
    _validate_extra_label,
)

# ---------------------------------------------------------------------------
# Label validator unit tests
# ---------------------------------------------------------------------------


def test_validate_extra_label_accepts_canonical_keys() -> None:
    """All 5 spec-076 keys MUST validate cleanly."""
    for key in (
        "coordinare.session_id",
        "coordinare.card_id",
        "coordinare.performer_stage",
        "coordinare.daemon_started_at",
        "coordinare.spec_version",
    ):
        _validate_extra_label(key, "valid-value")  # must not raise


def test_validate_extra_label_rejects_non_coordinare_prefix() -> None:
    """Keys outside the ``coordinare.`` namespace are refused."""
    with pytest.raises(ContainerStartError, match="invalid extra_label key"):
        _validate_extra_label("not_a_coordinare_label", "value")


def test_validate_extra_label_rejects_uppercase_key() -> None:
    """Key namespace is lowercase only (matches Docker label-key conventions)."""
    with pytest.raises(ContainerStartError, match="invalid extra_label key"):
        _validate_extra_label("coordinare.Session_Id", "value")


def test_validate_extra_label_rejects_empty_value() -> None:
    """Empty-string values would silently match nothing during reconciliation."""
    with pytest.raises(ContainerStartError, match="must be non-empty"):
        _validate_extra_label("coordinare.session_id", "")


def test_validate_extra_label_rejects_oversized_value() -> None:
    """Docker labels accept long values; coordinare caps at 256 chars for safety."""
    long_value = "x" * 300
    with pytest.raises(ContainerStartError, match="exceeds 256 chars"):
        _validate_extra_label("coordinare.card_id", long_value)


def test_validate_extra_label_rejects_non_string_value() -> None:
    """Integer or other non-string values are refused."""
    with pytest.raises(ContainerStartError, match="must be non-empty"):
        _validate_extra_label("coordinare.card_id", 12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# End-to-end: dispatch_card builds all 6 labels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_emits_all_six_required_labels(monkeypatch) -> None:
    """Every coordinare-spawned ephemeral container MUST carry these 6 labels:

    - coordinare.performer.id        (existing, from PerformerEndpointConfig.id)
    - coordinare.session_id          (new, coordinare-allocated UUID)
    - coordinare.card_id             (new, from card_context.id)
    - coordinare.performer_stage     (new, from card_context.role)
    - coordinare.daemon_started_at   (new, daemon process identity)
    - coordinare.spec_version        (new, "076")

    See specs/076-qa-cycle/contracts/docker-labels.md.
    """
    captured_labels: dict[str, str] = {}

    async def fake_start(config, *, extra_labels=None, **_kwargs):
        from coordinare.services.performer_lifecycle import StartedContainer
        if extra_labels:
            captured_labels.update(extra_labels)
        return StartedContainer(container_id="ctr-x", endpoint="http://127.0.0.1:55555")

    async def fake_wait_ready(*args, **kwargs):
        return None

    from coordinare.services import http_performer_service as hps_mod
    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)

    # Build a minimal but realistic ephemeral service + card context
    import httpx

    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.transport.http_transport import PerformerHTTPClient

    config = PerformerEndpointConfig(
        id="perf-label-test",
        mode="ephemeral",
        image="coordinare-performer:test",
        roles=["implementer"],
        readiness_timeout_s=10,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "accepted": True,
                "job_id": "job-label-test",
                "started_at": "2026-05-28T22:00:00Z",
            },
        )

    client = PerformerHTTPClient(
        "http://127.0.0.1:55555",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    svc = HTTPPerformerService(config, client=client)

    # Disable log polling for the test
    async def _noop_log_poll(container_id, session_id):
        return

    monkeypatch.setattr(svc, "_poll_container_logs", _noop_log_poll)

    card_context = {
        "id": "PVTI_TEST123",
        "title": "Test card",
        "role": "implementing",
        "repo_url": "https://github.com/x/y.git",
        "branch": "test-branch",
    }
    workspace_info = None

    result = await svc.dispatch_card(card_context, workspace_info)
    assert result["status"] == "ok", result

    # All 5 new labels must be present (coordinare.performer.id is added
    # separately, BEFORE extra_labels, by start_ephemeral itself).
    assert "coordinare.session_id" in captured_labels
    assert captured_labels["coordinare.session_id"] == result["session_id"]
    assert captured_labels["coordinare.card_id"] == "PVTI_TEST123"
    assert captured_labels["coordinare.performer_stage"] == "implementing"
    assert captured_labels["coordinare.daemon_started_at"]  # non-empty ISO string
    assert captured_labels["coordinare.spec_version"] == "076"


@pytest.mark.asyncio
async def test_dispatch_card_omits_empty_card_labels_for_bootstrap(monkeypatch) -> None:
    """076 regression: a card-less dispatch (env_bootstrap) MUST NOT emit
    empty-string ``coordinare.card_id`` / ``coordinare.performer_stage`` labels.

    The env_bootstrap performer is symphony-scoped — its card_context carries
    no ``id`` or ``role``.  Before the fix, these were emitted as ``""`` which
    fails ``_validate_extra_label`` and aborts the launch, deadlocking dispatch
    (coordinare believes a bootstrap is in-flight while no container started).
    The session-scoped labels MUST still be present so dispatch succeeds.
    """
    captured_labels: dict[str, str] = {}

    async def fake_start(config, *, extra_labels=None, **_kwargs):
        from coordinare.services.performer_lifecycle import (
            StartedContainer,
            _validate_extra_label,
        )
        # Mirror start_ephemeral's real validation so an empty-string label
        # would surface here exactly as it does in production.
        if extra_labels:
            for k, v in extra_labels.items():
                _validate_extra_label(k, v)
            captured_labels.update(extra_labels)
        return StartedContainer(container_id="ctr-bootstrap", endpoint="http://127.0.0.1:55556")

    async def fake_wait_ready(*args, **kwargs):
        return None

    from coordinare.services import http_performer_service as hps_mod
    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)

    import httpx

    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.transport.http_transport import PerformerHTTPClient

    config = PerformerEndpointConfig(
        id="perf-bootstrap-test",
        mode="ephemeral",
        image="coordinare-performer:test",
        roles=["env_bootstrap"],
        readiness_timeout_s=10,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "accepted": True,
                "job_id": "job-bootstrap-test",
                "started_at": "2026-05-29T15:00:00Z",
            },
        )

    client = PerformerHTTPClient(
        "http://127.0.0.1:55556",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    svc = HTTPPerformerService(config, client=client)

    async def _noop_log_poll(container_id, session_id):
        return

    monkeypatch.setattr(svc, "_poll_container_logs", _noop_log_poll)

    # Card-less context: env_bootstrap has neither id nor role.
    card_context = {
        "title": "env bootstrap",
        "repo_url": "https://github.com/x/y.git",
        "branch": "main",
    }

    result = await svc.dispatch_card(card_context, None)
    assert result["status"] == "ok", result

    # Session-scoped labels survive...
    assert captured_labels["coordinare.session_id"] == result["session_id"]
    assert captured_labels["coordinare.daemon_started_at"]
    assert captured_labels["coordinare.spec_version"] == "076"
    # ...and the empty card-scoped labels are omitted entirely (not "").
    assert "coordinare.card_id" not in captured_labels
    assert "coordinare.performer_stage" not in captured_labels
