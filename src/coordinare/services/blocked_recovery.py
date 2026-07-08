"""129 (US1): pure evaluator deciding whether a BLOCKED card may auto-recover.

No I/O — the caller (check_board) gathers the cleared-signals and the anti-thrash
state, this decides STILL_BLOCKED vs RECOVER(target_stage). Encodes FR-001..006:
ALL active block reasons must clear (FR-005), a genuine unresolved human verdict
or unanswered clarification NEVER clears (FR-004), and an anti-thrash cooldown
gates repeated attempts (FR-006).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BlockReason(StrEnum):
    STALE_REVIEW = "stale_review"          # gated by a human CHANGES_REQUESTED verdict
    ENV_BLOCKED = "env_blocked"            # spec-095 environment failure
    CI_RED = "ci_red"                      # a required CI check was red
    OPEN_CLARIFICATION = "open_clarification"  # an unanswered human question


@dataclass(frozen=True)
class RecoverySignals:
    """Current cleared-signals gathered by the caller (all fail-safe defaults)."""
    review_decision: str = ""            # GitHub reviewDecision (e.g. CHANGES_REQUESTED/APPROVED/"")
    review_stale_addressed: bool = False  # spec-128 evaluator returned STALE_ADDRESSED
    env_recovered: bool = False
    ci_green: bool = False
    clarification_answered: bool = False
    prior_stage: str = ""                 # stage to resume env/clarification/ci recoveries to
    cooldown_active: bool = False         # anti-thrash: a recovery was attempted too recently


@dataclass(frozen=True)
class RecoveryConfig:
    cooldown_cycles: int = 3  # min cycles between recovery attempts (caller enforces via marker)


@dataclass(frozen=True)
class RecoveryDecision:
    recover: bool
    target_stage: str  # board column to move to when recover=True; "" otherwise
    reason: str
    reasons_evaluated: list[BlockReason] = field(default_factory=list)


# Stage precedence when several cleared reasons imply different resumption points:
# a review/CI block resumes at IN_REVIEW (PR exists); env/clarification resume at
# the card's prior stage (default IN_PROGRESS). Later > earlier.
_STAGE_RANK = {"TODO": 0, "IN_PROGRESS": 1, "IN_REVIEW": 2}


def _reason_cleared(reason: BlockReason, s: RecoverySignals) -> tuple[bool, str]:
    """Return (cleared, target_stage_if_sole_reason)."""
    if reason is BlockReason.STALE_REVIEW:
        # Safety (FR-004): a CHANGES_REQUESTED verdict clears ONLY when spec-128
        # judged it stale+addressed; a genuine/fresh verdict never auto-clears.
        if s.review_decision.upper() == "CHANGES_REQUESTED":
            return (s.review_stale_addressed, "IN_REVIEW")
        # verdict no longer requesting changes (approved/dismissed/none) → cleared
        return (True, "IN_REVIEW")
    if reason is BlockReason.ENV_BLOCKED:
        return (s.env_recovered, s.prior_stage or "IN_PROGRESS")
    if reason is BlockReason.CI_RED:
        return (s.ci_green, "IN_REVIEW")
    if reason is BlockReason.OPEN_CLARIFICATION:
        # Safety (FR-004): only a human answer clears this.
        return (s.clarification_answered, s.prior_stage or "IN_PROGRESS")
    return (False, "")  # unknown reason → never auto-recover


def evaluate_recovery(
    reasons: list[BlockReason],
    signals: RecoverySignals,
    config: RecoveryConfig | None = None,
) -> RecoveryDecision:
    """Decide whether a BLOCKED card with the given active ``reasons`` may recover."""
    _ = config or RecoveryConfig()
    if not reasons:
        # No recorded recoverable reason → leave it to the operator (safe).
        return RecoveryDecision(False, "", "no recoverable block reason recorded", [])

    if signals.cooldown_active:
        return RecoveryDecision(False, "", "recovery cooldown active", list(reasons))

    targets: list[str] = []
    for r in reasons:
        cleared, target = _reason_cleared(r, signals)
        if not cleared:
            return RecoveryDecision(
                False, "", f"block reason still holds: {r.value}", list(reasons)
            )
        targets.append(target)

    # All reasons cleared (FR-005) → resume at the latest implied stage.
    target_stage = max(targets, key=lambda t: _STAGE_RANK.get(t, 1))
    return RecoveryDecision(
        True,
        target_stage,
        "all block reasons cleared: " + ", ".join(r.value for r in reasons),
        list(reasons),
    )
