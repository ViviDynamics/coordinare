"""Data models for branch-divergence handling (047, 555).

Transient — RebaseRound is rebuilt per divergence sweep, not persisted.

555: the daemon no longer rebases. It only produces ``DELEGATED`` (handed to
the implementer), ``SKIPPED`` and ``DEFERRED``. ``CLEAN``, ``PERFORMER_RESOLVED``,
``BLOCKED`` and ``FAILED`` remain so persisted anti-thrash markers from older
builds still parse.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class RebaseOutcome(StrEnum):
    """Result of a single branch divergence decision."""

    DELEGATED = "delegated"                # Diverged; handed to the implementer (555)
    CLEAN = "clean"                        # Rebased with no conflicts; force-pushed
    PERFORMER_RESOLVED = "performer_resolved"  # Conflicts resolved by performer; force-pushed
    BLOCKED = "blocked"                    # Conflicts unresolvable; card blocked
    SKIPPED = "skipped"                    # Branch up-to-date or performer active
    DEFERRED = "deferred"                  # Lifecycle unavailable or PR closed; retry later
    FAILED = "failed"                      # Unexpected error (git failure, push rejected)


@dataclass
class RebaseJob:
    """One divergence decision for a single in-flight branch."""

    card_id: str
    branch: str
    pr_number: int = 0
    pre_rebase_sha: str = ""
    post_rebase_sha: str = ""
    target_main_sha: str = ""
    outcome: RebaseOutcome = RebaseOutcome.SKIPPED
    conflicted_files: list[str] = field(default_factory=list)
    conflict_preview: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "branch": self.branch,
            "pr_number": self.pr_number,
            "pre_rebase_sha": self.pre_rebase_sha,
            "post_rebase_sha": self.post_rebase_sha,
            "target_main_sha": self.target_main_sha,
            "outcome": self.outcome.value,
            "conflicted_files": self.conflicted_files,
            "conflict_preview": self.conflict_preview,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class RebaseRound:
    """Set of RebaseJobs produced by a single divergence sweep."""

    trigger_pr_number: int = 0
    trigger_sha: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    jobs: list[RebaseJob] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_pr_number": self.trigger_pr_number,
            "trigger_sha": self.trigger_sha,
            "timestamp": self.timestamp.isoformat(),
            "jobs": [j.to_dict() for j in self.jobs],
        }

    @property
    def summary(self) -> str:
        """One-line summary for Slack notification."""
        counts: dict[str, int] = {}
        for job in self.jobs:
            counts[job.outcome.value] = counts.get(job.outcome.value, 0) + 1
        parts = []
        if counts.get("delegated"):
            parts.append(f"{counts['delegated']} handed to implementer")
        if counts.get("clean"):
            parts.append(f"{counts['clean']} rebased cleanly")
        if counts.get("performer_resolved"):
            parts.append(f"{counts['performer_resolved']} conflict(s) resolved by performer")
        if counts.get("blocked"):
            parts.append(f"{counts['blocked']} blocked on conflict")
        if counts.get("skipped"):
            parts.append(f"{counts['skipped']} skipped")
        if counts.get("deferred"):
            parts.append(f"{counts['deferred']} deferred")
        if counts.get("failed"):
            parts.append(f"{counts['failed']} failed")
        return ", ".join(parts) if parts else "no branches to rebase"
