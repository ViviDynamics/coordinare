"""Dispatcher-dedup data model (spec 076).

Plain value objects + enums used across `dispatch_guard`, `reconciliation`,
`retry_counter`, and the modified graph nodes.  All models forbid extra
fields to catch typos at construction time.

See `specs/076-qa-cycle/data-model.md` for the canonical definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 — runtime use by Pydantic field validators
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReconciliationDecision(StrEnum):
    """Per-card outcome emitted by the reconciliation pass (data-model §2)."""

    ADOPTED = "adopted"
    REAPED_AND_REPLACED = "reaped_and_replaced"
    FRESH_DISPATCHED = "fresh_dispatched"
    SKIPPED_PERSISTENT = "skipped_persistent"
    ORPHAN_SWEPT = "orphan_swept"


class WedgeResolution(StrEnum):
    """Action taken when ``detect_wedged_state`` finds the forbidden combination (data-model §3)."""

    RELEASED = "released"      # default per FR-020 + clarification Q1
    BLOCKED = "blocked"        # promoted after N wedges in trailing window
    OP_OVERRIDE = "op_override"


class IdleTimeoutRetryRecord(BaseModel):
    """Per-(card_id, performer_stage) rolling-window counter for idle-timeout retries.

    Persisted on ``PersistedSession.idle_timeout_retries[<card_id>:<stage>]``.
    Implements FR-019 / clarification Q3 (2 retries per rolling 24h window).
    """

    model_config = ConfigDict(extra="forbid")

    card_id: str
    performer_stage: str
    window_start_at: datetime
    attempt_count: int = Field(default=0, ge=0)
    last_at: datetime | None = None


class MultiPRDivergence(BaseModel):
    """Surfaced when FR-024 multi-PR detection finds >1 open PR for a card."""

    model_config = ConfigDict(extra="forbid")

    card_id: str
    canonical_branch_prefix: str
    pr_numbers: list[int]
    detected_at: datetime
    detected_at_trigger: Literal["dispatch", "restart", "webhook"]
    response: Literal["dispatch_refused", "card_blocked"]


@dataclass(frozen=True)
class CanonicalBranchName:
    """Value object for a card's canonical branch name (data-model §6).

    Construct via ``dispatch_guard.canonical_branch_name(card)``.  Two
    instances built from the same ``(card_node_id, title)`` are equal.
    """

    card_node_id: str
    title_slug: str

    @property
    def full_name(self) -> str:
        return f"coordinare/{self.card_node_id}/{self.title_slug}"


class ReconciliationReport(BaseModel):
    """Summary object returned by ``run_startup_reconciliation`` (data-model §7)."""

    model_config = ConfigDict(extra="forbid")

    started_at: datetime
    completed_at: datetime
    wall_clock_seconds: float
    cards_considered: int
    decisions: dict[str, ReconciliationDecision] = Field(default_factory=dict)
    orphans_swept: list[str] = Field(default_factory=list)
    docker_unreachable: bool = False


__all__ = [
    "CanonicalBranchName",
    "IdleTimeoutRetryRecord",
    "MultiPRDivergence",
    "ReconciliationDecision",
    "ReconciliationReport",
    "WedgeResolution",
]
