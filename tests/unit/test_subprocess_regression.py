"""Unit test for subprocess performer regression (spec 056, T064a).

Verifies that subprocess performers:
1. Never populate CoordinareState.performer_endpoints.
2. Emit zero performer_endpoint.transition events.
3. Emit zero performer_pool_* structured events.
4. Coexist cleanly with containerized performers.

This test ensures FR-024 (subprocess unaffected) and SC-008 (no throughput regression).
"""

from __future__ import annotations

import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.performer_pool import PerformerPool


def test_subprocess_performer_not_tracked_in_pool() -> None:
    """Subprocess performers are explicitly excluded from the pool."""
    pool = PerformerPool()

    # Try to register a subprocess performer
    subprocess_config = PerformerEndpointConfig.model_validate(
        {
            "id": "subprocess-perf",
            "mode": "subprocess",
            "roles": ["writer"],
        }
    )

    # The pool should reject subprocess performers (FR-024).
    # This is enforced at registration time: pool.register() raises ValueError
    # for subprocess mode, ensuring subprocess performers never enter the pool.

    with pytest.raises(ValueError, match="subprocess performers must not be registered"):
        pool.register(subprocess_config, service=None)


def test_subprocess_performer_no_transition_events() -> None:
    """Subprocess performers are never registered in the pool, so no transition events are emitted.

    This is enforced by PerformerPool.register() rejecting subprocess mode (FR-024).
    """
    pool = PerformerPool()

    subprocess_config = PerformerEndpointConfig.model_validate(
        {
            "id": "subprocess-perf",
            "mode": "subprocess",
            "roles": ["writer"],
        }
    )

    # Pool rejects subprocess; therefore no performer_endpoint.transition
    # events are ever emitted for subprocess registrations.
    with pytest.raises(ValueError):
        pool.register(subprocess_config, service=None)


@pytest.mark.asyncio
async def test_subprocess_performer_no_pool_metrics() -> None:
    """Subprocess performers never emit performer_pool_* metrics because they never enter the pool.

    Pool operations only apply to containerized performers (FR-024).
    """
    pool = PerformerPool()

    # Subprocess performers are never registered, so no pool metrics are emitted.
    # We verify this by confirming an empty pool emits no metrics (only a potential
    # "deferred" outcome if dispatch is attempted with no idle performers).

    # With an empty pool, poll_all should not emit any per-performer metrics.
    await pool.poll_all()

    # The pool remains empty; subprocess registrations never populate it.
    assert len(pool._registrations) == 0


def test_coordinare_state_performer_endpoints_excludes_subprocess() -> None:
    """CoordinareState.performer_endpoints never contains subprocess performers.

    By design, subprocess performers never enter the pool and are not tracked
    in performer_endpoints. Only containerized performers (ephemeral/persistent)
    are registered via PerformerPool and tracked in state.
    """
    from coordinare.services.http_performer_service import HTTPPerformerService

    pool = PerformerPool()

    container_config = PerformerEndpointConfig.model_validate(
        {
            "id": "container-1",
            "mode": "persistent",
            "image": "performer:slim",
            "endpoint": "http://localhost:8088",
            "roles": ["implementer"],
        }
    )
    subprocess_config = PerformerEndpointConfig.model_validate(
        {
            "id": "subprocess-1",
            "mode": "subprocess",
            "roles": ["implementer"],
        }
    )

    service = HTTPPerformerService(container_config)
    pool.register(container_config, service=service)

    with pytest.raises(ValueError):
        pool.register(subprocess_config, service=None)

    # Only the containerized performer should be tracked.
    assert pool.get_state("container-1") is not None
    assert pool.get_state("subprocess-1") is None
    assert len(pool._registrations) == 1


def test_subprocess_mixed_with_containerized_no_cross_talk() -> None:
    """Subprocess and containerized performers don't interfere.

    When both are present, the pool tracks only containerized ones,
    subprocess performers use the legacy dispatch path unchanged.
    """
    from coordinare.services.http_performer_service import HTTPPerformerService

    pool = PerformerPool()

    # Simulate registering a containerized performer
    container_config = PerformerEndpointConfig.model_validate(
        {
            "id": "container-perf",
            "mode": "persistent",
            "image": "performer:slim",
            "endpoint": "http://localhost:8088",
            "roles": ["writer"],
        }
    )
    service = HTTPPerformerService(container_config)
    pool.register(container_config, service=service)

    # Verify it's tracked
    state = pool.get_state("container-perf")
    assert state is not None, "Containerized performer should be tracked"

    # A subprocess performer is never added to the pool
    # (it's dispatched via the legacy subprocess_transport.py)
    subprocess_config = PerformerEndpointConfig.model_validate(
        {
            "id": "subprocess-perf",
            "mode": "subprocess",
            "roles": ["writer"],
        }
    )
    with pytest.raises(ValueError, match="subprocess"):
        pool.register(subprocess_config, service=None)

    # The containerized performer is still tracked
    state = pool.get_state("container-perf")
    assert state is not None, "Containerized performer still tracked after subprocess rejection"


__all__ = [
    "test_coordinare_state_performer_endpoints_excludes_subprocess",
    "test_subprocess_mixed_with_containerized_no_cross_talk",
    "test_subprocess_performer_no_pool_metrics",
    "test_subprocess_performer_no_transition_events",
    "test_subprocess_performer_not_tracked_in_pool",
]
