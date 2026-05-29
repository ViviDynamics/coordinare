"""Spec 076 T143 (SC-006) — dispatch success-rate regression bound.

Validates that the 076 dispatcher-dedup machinery (in-flight guard +
per-card mutex + multi-PR divergence detection) does NOT refuse a
legitimate fresh-card dispatch.  The bug class 076 closes is
DUPLICATE dispatch, not FAILED first dispatch.

Acceptance: 50/50 fresh-card guard checks proceed (advice="proceed",
no mutex deadlock, no divergence false-positive).

Tests the guard layers directly rather than invoking the full dispatch
body, which would attempt real workspace setup.  The guard layers are
what SC-006 is bounded against.

Baseline: 100% on pre-076 main (the guard layers didn't exist).
Tolerance per SC-006: ≥99% on this branch.
"""
from __future__ import annotations

import pytest

from coordinare.services.dispatch_guard import (
    check_inflight,
    detect_multi_pr_divergence,
    dispatch_mutex,
)

pytestmark = pytest.mark.benchmark


class _NeverLiveSvc:
    """A fresh service with no in-flight sessions."""

    def has_live_session(self, session_id: str) -> bool:
        return False


class _NoPriorPRsGithub:
    async def list_prs_by_branch_prefix(self, *a, **kw):
        return []


def _fresh_card_state(card_id: str) -> dict:
    return {
        "current_card": {"id": card_id, "title": "Fresh card", "status": "IN_PROGRESS"},
        "active_sessions": {
            card_id: {
                "current_card": {"id": card_id, "title": "Fresh card"},
            },
        },
        "performer_stage": "implementing",
        "agent_dispatch": {},  # fresh card → no in-flight session
        "performer_services": {"implementing": _NeverLiveSvc()},
    }


@pytest.mark.asyncio
async def test_sc006_guard_layers_allow_fresh_dispatch_at_full_rate() -> None:
    """SC-006: the 076 dispatcher-dedup guard layers MUST allow every
    fresh-card dispatch through.  A regression that drops the
    pass-through rate below 99% means one of the guards is incorrectly
    refusing legitimate first dispatches.

    Three guard layers exercised:
    1. check_inflight (FR-001) — must return advice="proceed"
    2. dispatch_mutex (FR-006) — must acquire without deadlock
    3. detect_multi_pr_divergence (FR-024) — must return None
    """
    trials = 50
    passes = 0

    for i in range(trials):
        card_id = f"PVTI_TRIAL_{i}"
        state = _fresh_card_state(card_id)

        # Layer 1: in-flight guard
        guard = await check_inflight(state, card_id, "implementing")
        if guard.advice != "proceed":
            continue

        # Layer 2: mutex acquisition (no contention on a fresh tuple)
        async with dispatch_mutex(card_id, "implementing"):
            pass  # must release cleanly

        # Layer 3: multi-PR divergence (no prior PRs → no divergence)
        divergence = await detect_multi_pr_divergence(
            state, card_id,
            github_service=_NoPriorPRsGithub(),
            owner="x", repo="y",
            trigger="dispatch",
        )
        if divergence is not None:
            continue

        passes += 1

    pass_rate = passes / trials
    assert pass_rate >= 0.99, (
        f"076 guard layers refused {trials - passes}/{trials} fresh "
        f"dispatches ({pass_rate:.1%}); expected ≥99% per SC-006. "
        f"Baseline against pre-076 main: 100% (guards didn't exist)."
    )
