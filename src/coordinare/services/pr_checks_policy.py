"""Pure decision policy for the closer PR-checks gate (spec 064).

All inputs come from `pr_checks_service.CheckRollup` (and config); all outputs
are `GateDecision` values. No I/O — unit-testable without mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from coordinare.services.pr_checks_service import CheckEntry, CheckRollup

Action = Literal["FORWARD", "BOUNCE", "HOLD"]


class GateDecision(BaseModel):
    """Outcome of evaluating a `CheckRollup` against gate config."""

    model_config = ConfigDict(frozen=True)

    action: Action
    reason: str
    failed: list[str] = []
    pending: list[str] = []
    elapsed_seconds: float = 0.0


# Conclusions that count as a pass per FR-002.
_PASS_CONCLUSIONS = {"success", "neutral", "skipped"}
# Conclusions that count as a fail per FR-002.
_FAIL_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
}


def _is_pending(entry: CheckEntry) -> bool:
    if entry.status != "completed":
        return True
    return entry.conclusion is None


def _is_failure(entry: CheckEntry) -> bool:
    return entry.status == "completed" and entry.conclusion in _FAIL_CONCLUSIONS


def _is_pass(entry: CheckEntry) -> bool:
    return entry.status == "completed" and entry.conclusion in _PASS_CONCLUSIONS


def decide(
    rollup: CheckRollup,
    *,
    pending_timeout_seconds: int = 900,
    treat_unknown_required_as: Literal["pass", "block"] = "pass",
    now: datetime | None = None,
    required_check_names: set[str] | None = None,
) -> GateDecision:
    """Evaluate a check rollup and produce a gate decision.

    Pure function. `now` is injectable for deterministic tests.

    When ``required_check_names`` is provided (spec 075), it overrides the
    ``is_required`` flag from the rollup: only checks whose name appears in
    the set are treated as required, and the branch-protection-readable
    gating is bypassed (the caller resolved the required set explicitly).
    Names in the set that are not present in the rollup are treated as
    pending (the expected check has not yet reported).  When ``None``
    (the default), 064's existing behavior is preserved byte-identically.
    """
    now = now or datetime.now(UTC)
    elapsed = (now - rollup.head_pushed_at).total_seconds()

    if required_check_names is not None:
        present = {c.name for c in rollup.checks}
        required = [c for c in rollup.checks if c.name in required_check_names]
        missing = sorted(required_check_names - present)

        failed = [c.name for c in required if _is_failure(c)]
        if failed:
            return GateDecision(
                action="BOUNCE",
                reason="required_check_failed",
                failed=failed,
                elapsed_seconds=elapsed,
            )

        pending = [c.name for c in required if _is_pending(c)] + missing
        if pending:
            if elapsed > pending_timeout_seconds:
                return GateDecision(
                    action="BOUNCE",
                    reason="pending_timeout",
                    pending=pending,
                    elapsed_seconds=elapsed,
                )
            return GateDecision(
                action="HOLD",
                reason="checks_pending",
                pending=pending,
                elapsed_seconds=elapsed,
            )

        return GateDecision(
            action="FORWARD",
            reason="all required checks passing",
            elapsed_seconds=elapsed,
        )

    required = [c for c in rollup.checks if c.is_required]

    bp_unreadable_pass = (
        not rollup.branch_protection_readable
        and treat_unknown_required_as == "pass"
    )

    if not rollup.branch_protection_readable:
        if treat_unknown_required_as == "block":
            return GateDecision(
                action="BOUNCE",
                reason="branch_protection_unreadable",
                elapsed_seconds=elapsed,
            )
        # "pass" → required-set unknown; treat visible PENDING checks as HOLD
        # (subject to pending_timeout) so we don't FORWARD onto a still-running
        # build. Visible advisory FAILURES remain advisory — they don't bounce.
        required = []

    failed = [c.name for c in required if _is_failure(c)]
    if failed:
        return GateDecision(
            action="BOUNCE",
            reason="required_check_failed",
            failed=failed,
            elapsed_seconds=elapsed,
        )

    pending = [c.name for c in required if _is_pending(c)]
    if not pending and bp_unreadable_pass:
        # Required-set unknown — surface visible pending checks so we don't
        # FORWARD onto a still-running build. Failures stay advisory above.
        pending = [c.name for c in rollup.checks if _is_pending(c)]
    if pending:
        if elapsed > pending_timeout_seconds:
            return GateDecision(
                action="BOUNCE",
                reason="pending_timeout",
                pending=pending,
                elapsed_seconds=elapsed,
            )
        return GateDecision(
            action="HOLD",
            reason="checks_pending",
            pending=pending,
            elapsed_seconds=elapsed,
        )

    return GateDecision(
        action="FORWARD",
        reason="all required checks passing",
        elapsed_seconds=elapsed,
    )
