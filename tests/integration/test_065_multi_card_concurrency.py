"""065 US2 — integration tests exploiting the silent multi-performer cap.

Background
----------
Operators can configure ``performers.<role>.max_concurrency: N`` to opt into
running up to N performer instances of a role in parallel.  In production
runs we observed that even with ``max_concurrency: 4`` configured, the
daemon never dispatched more than one card per role per cycle.  The cap
silently collapses to ``1`` in two places:

1. ``coordinare/__main__.py:704`` overwrites the operator-configured
   ``max_concurrency`` with ``len(service_lists[stage])`` before calling
   ``slot_manager.register_pool``.  No log, no warning.
2. ``SlotManager.register_pool`` (``services/slot_manager.py:93``) clamps
   ``max_concurrency`` to ``len(services)`` silently — unlike the
   hot-reload code path a few lines below which logs
   ``slot_manager.hot_reload.capped_by_services`` when it does the same
   thing.

The combined effect: a per-role concurrency setting that the operator
believes is in force is actually quietly disabled, and there is no log
record an SRE could grep for to discover the cap.

What these tests assert
-----------------------
- FR-006/FR-007: when the registered service pool is large enough to honor
  the configured ``max_concurrency``, ``register_pool`` MUST preserve the
  configured value (currently passes — kept as a control).
- FR-008 (cap visibility): when ``register_pool`` clamps
  ``max_concurrency`` because the service pool is smaller than the
  configured value, it MUST emit a structured ``slot_manager.register.
  capped_by_services`` log carrying ``stage``, ``requested``, and
  ``available`` — mirroring the hot-reload log key already in place.
  This FAILS on current ``main`` (the clamp is silent).
- SC-004: with 2 services + 2 distinct cards and a configured
  ``max_concurrency`` of 2, two slots are acquired in one cycle with no
  ``dispatch_performer.at_capacity`` records emitted (control — confirms
  the bug isolation is the silent-cap, not a SlotManager allocation bug).

These tests are written to fail loudly when the silent clamp regresses,
and pin the diagnostic-log contract that the 065 US2 fix must satisfy.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import structlog

from coordinare.__main__ import _compose_performer_pools
from coordinare.services.slot_manager import SlotManager


def _svc() -> MagicMock:
    """A minimal AgentService stand-in — register_pool only needs len()."""
    return MagicMock(name="AgentService")


# ---------------------------------------------------------------------------
# Exploits
# ---------------------------------------------------------------------------


def test_register_pool_logs_when_clamping_max_concurrency_to_service_count() -> None:
    """Exploit: operator configured ``max_concurrency=4`` but only one
    transport instance is registered for the stage.  ``register_pool`` is
    expected to clamp (it cannot allocate more slots than services), but
    the clamp must be observable in logs so SREs can diagnose why the
    configured concurrency is not in effect.

    On current ``main`` the clamp is silent — the hot-reload code path a
    few lines below logs ``slot_manager.hot_reload.capped_by_services``
    for the same situation but ``register_pool`` does not.  This test
    pins the missing log.
    """
    sm = SlotManager()
    with structlog.testing.capture_logs() as logs:
        sm.register_pool("implementing", [_svc()], max_concurrency=4)

    capped = [
        r for r in logs
        if r.get("event") == "slot_manager.register.capped_by_services"
    ]
    assert capped, (
        "register_pool clamped max_concurrency silently; expected a "
        f"'slot_manager.register.capped_by_services' log. Got events: "
        f"{[r.get('event') for r in logs]}"
    )
    record = capped[0]
    assert record.get("stage") == "implementing"
    assert record.get("requested") == 4
    assert record.get("available") == 1


def test_register_pool_preserves_configured_max_when_services_sufficient() -> None:
    """Control: when the service pool is at least as large as the
    configured concurrency, ``register_pool`` must keep the operator's
    value verbatim and emit no clamp log."""
    sm = SlotManager()
    with structlog.testing.capture_logs() as logs:
        sm.register_pool("implementing", [_svc(), _svc(), _svc()], max_concurrency=2)

    assert sm.pools["implementing"].max_concurrency == 2
    capped = [
        r for r in logs
        if r.get("event") == "slot_manager.register.capped_by_services"
    ]
    assert not capped, f"unexpected clamp log: {capped}"


def test_two_services_two_cards_acquire_concurrently_in_one_cycle() -> None:
    """SC-004 / FR-007: with two services registered for a role and the
    operator-configured ``max_concurrency=2``, two distinct cards must
    each acquire their own slot in the same cycle.  No
    ``dispatch_performer.at_capacity`` event should be emitted.

    This is a control: the SlotManager already honors this contract when
    ``max_concurrency`` reaches it intact.  The exploit above pins the
    upstream silent clamp that prevents that ``max_concurrency`` from
    ever reaching ``register_pool`` in real bootstrap (see
    ``__main__.py:704``).
    """
    sm = SlotManager()
    svc_a, svc_b = _svc(), _svc()
    sm.register_pool("implementing", [svc_a, svc_b], max_concurrency=2)

    with structlog.testing.capture_logs() as logs:
        got_a = sm.acquire("implementing", "PVTI_A")
        got_b = sm.acquire("implementing", "PVTI_B")

    assert got_a is not None and got_b is not None, (
        f"expected 2 concurrent acquires; got_a={got_a!r} got_b={got_b!r}"
    )
    assert got_a is not got_b, "both cards got the same service instance"
    assert sm.active_count("implementing") == 2

    at_capacity = [
        r for r in logs if r.get("event") == "dispatch_performer.at_capacity"
    ]
    assert not at_capacity, (
        f"unexpected at_capacity events during two concurrent acquires: {at_capacity}"
    )


def _stub_config(role_max: dict[str, int]) -> SimpleNamespace:
    """A minimal ProjectConfiguration ducktype for ``_compose_performer_pools``.

    Only ``config.performers.resolved_role(role)`` is consulted.
    """
    roles = {
        role: SimpleNamespace(max_concurrency=mx) for role, mx in role_max.items()
    }
    return SimpleNamespace(
        performers=SimpleNamespace(resolved_role=lambda r: roles.get(r)),
    )


def test_compose_performer_pools_preserves_operator_max_concurrency() -> None:
    """Exploit: operator configured ``performers.implementer.max_concurrency:
    4`` but only one HTTP transport instance is currently registered for the
    role.  The pool-composition helper must preserve the operator's value
    so the SlotManager can either honor it (when more services come up) or
    log a clamp event the operator can grep for.

    On current ``main`` the helper overwrites
    ``stage_max_c["implementing"]`` with ``len(merged_services) == 1``,
    silently disabling the operator's intent before ``register_pool`` ever
    sees it.  This test pins that contract violation.
    """
    config = _stub_config({"implementer": 4})
    http = {"implementing": [_svc()]}  # only one HTTP endpoint configured
    performer_services: dict = {}

    _service_lists, stage_max_c, _by_id = _compose_performer_pools(
        config=config,
        service_lists={},
        http_services_by_stage=http,
        performer_services=performer_services,
    )

    assert stage_max_c["implementing"] == 4, (
        "operator-configured max_concurrency was overwritten by pool "
        f"composition: stage_max_c={stage_max_c}. The SlotManager — not "
        "the bootstrap merge — is responsible for clamping when the pool "
        "is smaller than the configured cap."
    )


def test_compose_performer_pools_does_not_overwrite_when_pool_meets_max() -> None:
    """Control: when the configured max is already satisfied by the merged
    service list, the helper should still leave the operator's value as
    the source of truth — neither raise nor mutate."""
    config = _stub_config({"implementer": 2})
    http = {"implementing": [_svc(), _svc()]}
    _sl, stage_max_c, _ = _compose_performer_pools(
        config=config,
        service_lists={},
        http_services_by_stage=http,
        performer_services={},
    )
    assert stage_max_c["implementing"] == 2


def test_third_card_at_capacity_when_max_is_two() -> None:
    """Capacity ceiling still binds: a third card while two are in flight
    cannot acquire, and ``is_at_capacity`` reports True so the dispatcher
    can emit its skip log.  Locks the contract the eventual
    ``dispatcher.skip`` structured log (FR-006) will hang off."""
    sm = SlotManager()
    sm.register_pool("implementing", [_svc(), _svc()], max_concurrency=2)

    assert sm.acquire("implementing", "PVTI_A") is not None
    assert sm.acquire("implementing", "PVTI_B") is not None
    assert sm.acquire("implementing", "PVTI_C") is None
    assert sm.is_at_capacity("implementing") is True
