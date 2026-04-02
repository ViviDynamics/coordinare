"""Dry-run mode — preview lifecycle without side effects (038).

Provides mock service implementations that record intended actions instead of
executing them, and an ``execute_dry_run`` orchestrator that builds a lifecycle
preview for a given card ID.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel

if TYPE_CHECKING:
    from coordinare.config import ProjectConfiguration

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Mock GitHub service
# ---------------------------------------------------------------------------


class DryRunGitHubService:
    """Drop-in replacement for ``GitHubService`` that records calls.

    Implements ``GitHubServiceProtocol`` — every method appends the call
    to ``recorded_actions`` and returns synthetic data.
    """

    def __init__(self) -> None:
        self.recorded_actions: list[dict[str, Any]] = []

    async def poll_board(self) -> dict[str, Any]:
        self.recorded_actions.append({"method": "poll_board"})
        return {
            "snapshot": {
                "TODO": [], "IN_PROGRESS": [], "IN_REVIEW": [],
                "BLOCKED": [], "DONE": [],
            },
            "titles": {},
            "descriptions": {},
            "field_values": {},
            "issue_numbers": {},
            "issue_urls": {},
            "item_labels": {},
            "content_node_ids": {},
        }

    async def get_issue_details(self, issue_id: str) -> dict[str, Any]:
        self.recorded_actions.append({"method": "get_issue_details", "issue_id": issue_id})
        return {
            "id": issue_id,
            "title": f"[dry-run] Synthetic issue {issue_id}",
            "body": "Dry-run synthetic issue body.",
            "labels": [],
            "state": "OPEN",
        }

    async def move_card(self, item_id: str, status: str) -> None:
        self.recorded_actions.append({"method": "move_card", "item_id": item_id, "status": status})

    async def get_pr_reviews(self, pr_id: str) -> list[dict[str, Any]]:
        self.recorded_actions.append({"method": "get_pr_reviews", "pr_id": pr_id})
        return []

    async def check_mergeability(self, pr_id: str) -> dict[str, Any]:
        self.recorded_actions.append({"method": "check_mergeability", "pr_id": pr_id})
        return {
            "mergeable": True, "mergeable_raw": "MERGEABLE",
            "merge_state_status": "CLEAN", "review_decision": "APPROVED",
        }

    async def squash_merge(self, pr_id: str) -> dict[str, Any]:
        self.recorded_actions.append({"method": "squash_merge", "pr_id": pr_id})
        return {"merged": True}

    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]:
        self.recorded_actions.append({"method": "add_comment", "subject_id": subject_id, "body": body})
        return {"id": "dry-run-comment-id"}


# ---------------------------------------------------------------------------
# Mock Agent service
# ---------------------------------------------------------------------------


class DryRunAgentService:
    """Drop-in replacement for ``AgentService`` / ``ResilientAgentService``.

    Implements ``AgentServiceProtocol`` — records dispatch parameters and
    returns synthetic success responses.
    """

    def __init__(self) -> None:
        self.recorded_actions: list[dict[str, Any]] = []

    async def dispatch_card(
        self,
        card_context: dict[str, Any],
        workspace_info: Any = None,
    ) -> dict[str, Any]:
        self.recorded_actions.append({
            "method": "dispatch_card",
            "card_context": card_context,
            "workspace_info": workspace_info if workspace_info is None else {
                "path": str(getattr(workspace_info, "path", None)),
                "branch": getattr(workspace_info, "branch", None),
            },
        })
        return {"status": "accepted", "session_id": "dry-run-session"}

    async def check_status(self, session_id: str) -> dict[str, Any]:
        self.recorded_actions.append({"method": "check_status", "session_id": session_id})
        return {
            "status": "pr_opened",
            "pr_url": "https://github.com/dry-run/repo/pull/0",
            "pr_node_id": "dry-run-pr-node-id",
        }

    async def check_health(self) -> dict[str, Any]:
        self.recorded_actions.append({"method": "check_health"})
        return {"status": "healthy"}

    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]:
        self.recorded_actions.append({"method": "relay_feedback", "review_payload": review_payload})
        return {"status": "accepted"}


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class DryRunResult(BaseModel):
    """Structured output of a dry-run preview."""

    planned_actions: list[str]
    lifecycle_stages: list[str]
    assessment_result: str
    board_transitions: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Lifecycle sequence builder (mirrors __main__._build_lifecycle_sequence)
# ---------------------------------------------------------------------------


def _build_lifecycle_sequence(config: ProjectConfiguration) -> list[str]:
    """Derive the ordered lifecycle from configured performer roles.

    Uses shared constants from ``coordinare.lifecycle`` to build the sequence,
    falling back to ``["implementing"]`` when no roles are configured.
    """
    from coordinare.lifecycle import CANONICAL_ORDER, ROLE_TO_STAGE
    sequence: list[str] = []
    for role in CANONICAL_ORDER:
        role_config = getattr(config.performers, role, None)
        if role_config is not None:
            sequence.append(ROLE_TO_STAGE[role])
    return sequence or ["implementing"]


# ---------------------------------------------------------------------------
# Dry-run orchestrator
# ---------------------------------------------------------------------------


def _describe_role(role: str, config: ProjectConfiguration) -> str:
    """Return a human-readable description of a role's backend configuration."""
    role_config = getattr(config.performers, role, None)
    if role_config is None:
        return f"{role} (default transport)"
    backend = role_config.backend or "opencode"
    model = role_config.model or "default"
    transport = role_config.transport or config.agent_transport
    return f"{role} (backend={backend}, model={model}, transport={transport})"


async def execute_dry_run(
    card_id: str,
    config: ProjectConfiguration,
) -> DryRunResult:
    """Build a lifecycle preview for *card_id* without side effects.

    Constructs the lifecycle sequence from *config*, simulates the assessment
    phase (always "sufficient" in dry-run), and returns a ``DryRunResult``
    showing the planned actions, stages, and board transitions.

    No GitHub API calls are made, no performers are spawned, and no state
    is persisted.
    """
    _log.info("dry_run.start", card_id=card_id)

    lifecycle = _build_lifecycle_sequence(config)

    # Synthetic assessment — always sufficient to show the full lifecycle
    assessment_result = "sufficient"

    # Build planned actions list
    planned_actions: list[str] = [
        f"Fetch issue details for card {card_id}",
        f"Assess card {card_id} -> {assessment_result}",
    ]

    board_transitions: list[dict[str, Any]] = []

    from coordinare.lifecycle import ROLE_TO_STAGE
    for stage in lifecycle:
        # Find the role name for this stage
        role = next((r for r, s in ROLE_TO_STAGE.items() if s == stage), stage)
        role_desc = _describe_role(role, config)
        planned_actions.append(f"Dispatch {role_desc} for stage '{stage}'")
        planned_actions.append(f"Monitor {stage} until completion")
        board_transitions.append({
            "stage": stage,
            "column": "IN_PROGRESS",
        })

    # Final board transition after all stages complete
    planned_actions.append("Move card to IN_REVIEW then DONE after all stages complete")
    board_transitions.append({"stage": "monitoring_pr", "column": "IN_REVIEW"})
    board_transitions.append({"stage": "complete", "column": "DONE"})

    result = DryRunResult(
        planned_actions=planned_actions,
        lifecycle_stages=lifecycle,
        assessment_result=assessment_result,
        board_transitions=board_transitions,
    )

    _log.info(
        "dry_run.complete",
        card_id=card_id,
        stages=lifecycle,
        actions_count=len(planned_actions),
    )

    return result
