"""L1 base-precondition gate (spec 090, US1).

The L1 gate refuses to merge an approved, head-green PR while a *required*
check on its **base** branch is red. It is the prevention layer of the
three-layer baseline-repair design: pure, default-off, and byte-identical to
the pre-feature baseline when disabled (the wiring in `monitor_pr.py` only calls
this when `baseline_prevention_gate.enabled`).

This module is intentionally thin. It reuses the spec-064 machinery wholesale:
the base required set comes from `required_checks_resolver.resolve` and the
red/pending/green verdict comes from `pr_checks_policy.decide` over the base
rollup. The only new surface is the typed `BaseGateDecision` so the wiring can
branch on a `decision` literal — BLOCK / PROCEED / INDETERMINATE — and name the
offending *base* check(s) in the hold record distinctly from any head failure
(FR-003).

Contract (data-model §11, FR-001 through FR-006, SC-002):

* a **required** base check in a terminal-failure conclusion → ``BLOCK`` with
  ``failing_checks`` naming that check (+ its URL);
* base all-green, only **non-required** base failures, or a still-*pending*
  required base check → ``PROCEED`` (L1's job is to refuse merge onto a *red*
  base, not to wait on the base build — pending is the closer gate's concern);
* a ``None`` base rollup (unfetchable / indeterminate) → ``INDETERMINATE`` —
  fail-safe fall-through to head-only behavior, **never** ``BLOCK`` (FR-005);
* an unreadable base branch-protection set → ``PROCEED``: we cannot prove any
  base check is *required*, so we must not hard-block on advisory failures.

The function is pure given its inputs (the network fetch happens upstream in
`get_base_branch_check_rollup`) and re-evaluated each cycle: it holds no state,
so a red base blocks again and a base that turns green proceeds next cycle —
the gate is never latched (FR-006).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinare.services import pr_checks_policy, required_checks_resolver
from coordinare.services.ci_gate import FailedCheck

if TYPE_CHECKING:
    from datetime import datetime

    from coordinare.services.pr_checks_service import CheckRollup

BaseGateVerdict = Literal["BLOCK", "PROCEED", "INDETERMINATE"]


class BaseGateDecision(BaseModel):
    """Typed outcome of the L1 base-precondition gate (data-model §11).

    In-process only — it gates the L1 merge transition and is not persisted; the
    existing hold/observability records carry its content. ``failing_checks`` is
    empty unless ``decision == "BLOCK"``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: BaseGateVerdict
    failing_checks: list[FailedCheck] = Field(default_factory=list)  # base checks only


def evaluate_base_gate(
    base_rollup: CheckRollup | None,
    scope: dict[str, Any] | None = None,
    *,
    persona_check_map: dict[str, Any] | None = None,
    now: datetime | None = None,
    pending_timeout_seconds: int = 900,
) -> BaseGateDecision:
    """Decide whether a base branch's check state should block the merge.

    Pure given its inputs. ``base_rollup`` is the base-origin rollup fetched by
    `get_base_branch_check_rollup` (``None`` when unfetchable). ``scope`` and
    ``persona_check_map`` are threaded into `required_checks_resolver.resolve`
    exactly as the closer gate threads them, so the base required set is resolved
    by the same rules. ``now`` / ``pending_timeout_seconds`` are forwarded to
    `pr_checks_policy.decide` for deterministic tests.
    """
    # FR-005 / SC-002: an unfetchable base is indeterminate — fall through to
    # head-only behavior, never block.
    if base_rollup is None:
        return BaseGateDecision(decision="INDETERMINATE")

    # Fail-safe: without a readable branch-protection set we cannot prove any
    # base check is *required*, so we must not hard-block on advisory failures.
    # (Going through the resolver here would hit its all-head-checks fallback and
    # treat *every* base check as required — exactly the over-block we avoid.)
    if not base_rollup.branch_protection_readable:
        return BaseGateDecision(decision="PROCEED")

    resolved = required_checks_resolver.resolve(
        scope=scope,
        branch_protection_set={c.name for c in base_rollup.checks if c.is_required},
        all_head_checks=[c.name for c in base_rollup.checks],
        persona_check_map=persona_check_map,
    )

    decision = pr_checks_policy.decide(
        base_rollup,
        required_check_names=set(resolved["names"]),
        now=now,
        pending_timeout_seconds=pending_timeout_seconds,
    )

    # Only a RED required base blocks the merge. A pending base HOLDs in the
    # policy, which maps to PROCEED here (L1 does not wait on the base build).
    if decision.action == "BOUNCE" and decision.reason == "required_check_failed":
        failed = set(decision.failed)
        failing_checks = [
            FailedCheck(
                name=c.name,
                conclusion=c.conclusion or "failure",
                html_url=c.details_url,
            )
            for c in base_rollup.checks
            if c.name in failed
        ]
        return BaseGateDecision(decision="BLOCK", failing_checks=failing_checks)

    return BaseGateDecision(decision="PROCEED")


__all__ = ["BaseGateDecision", "BaseGateVerdict", "evaluate_base_gate"]
