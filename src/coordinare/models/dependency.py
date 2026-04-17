"""Data models for card dependency detection (046).

Stateless — the dependency graph is rebuilt from board state + issue
descriptions on every poll cycle rather than persisted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class DependencyStatus(StrEnum):
    """Satisfaction state of a single card dependency."""

    PENDING = "pending"          # Blocker is on the board and not yet in DONE
    SATISFIED = "satisfied"      # Blocker is in DONE or the issue is closed/merged
    UNRESOLVABLE = "unresolvable"  # Blocker not on the board and not closed


class DependencySource(StrEnum):
    """How the dependency was detected."""

    EXPLICIT = "explicit"   # Parsed from card description ("Depends on #N", etc.)
    ASSESSOR = "assessor"   # Flagged by the assessor role's semantic analysis


@dataclass(frozen=True)
class CardDependency:
    """One dependency edge: a card that depends on a blocker issue."""

    dependent_item_id: str        # Project item ID (PVTI_...) of the dependent card
    blocker_issue_number: int     # GitHub issue number the card depends on
    source: DependencySource = DependencySource.EXPLICIT
    status: DependencyStatus = DependencyStatus.PENDING


@dataclass
class DependencyGraph:
    """Transient in-memory dependency graph rebuilt on each poll cycle.

    Not persisted — derived from the current board snapshot + issue bodies.
    """

    dependencies: list[CardDependency] = field(default_factory=list)
    # Index: dependent_item_id → its dependencies
    by_dependent: dict[str, list[CardDependency]] = field(default_factory=dict)
    # Index: blocker_issue_number → cards depending on it
    by_blocker: dict[int, list[CardDependency]] = field(default_factory=dict)
    # Reverse lookup: issue_number → item_id on the board
    issue_to_item: dict[int, str] = field(default_factory=dict)
    # Reverse lookup: issue_number → board column name
    issue_to_column: dict[int, str] = field(default_factory=dict)
    # Detected cycles (each is a list of item_ids forming a cycle)
    cycles: list[list[str]] = field(default_factory=list)
