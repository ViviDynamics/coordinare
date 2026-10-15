from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coordinare.env_placeholders import is_secret_env_key
from coordinare.lib.redaction import redact_mapping, redact_state_value
from coordinare.lifecycle import CANONICAL_ORDER
from coordinare.services.github import BOARD_COLUMN_TO_STATUS
from coordinare.services.pipeline_budget import has_live_work

if TYPE_CHECKING:
    from coordinare.config import CoordinareConfiguration, ProjectConfiguration


def sample_issue() -> dict[str, str]:
    """A reversible first card, shared with the future init command."""
    return {
        "title": "Document our first Coordinare run",
        "body": (
            "Create FIRST_CARD.md at the repository root with one sentence: "
            "Coordinare can open a pull request for this repository. "
            "Change no other files. Open a pull request for a human to review."
        ),
    }


def expanded_config_view(value: dict[str, Any], *, environment: bool = False) -> dict[str, Any]:
    """Keep effective settings inspectable while hiding credentials in arbitrary env mappings."""
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            safe[key] = expanded_config_view(item, environment=key in {"env", "workflow_env"})
        elif isinstance(item, list):
            safe[key] = [expanded_config_view(v) if isinstance(v, dict) else v for v in item]
        else:
            safe[key] = item
        if environment and is_secret_env_key(key):
            safe[key] = "[REDACTED]"
    redacted: dict[str, Any] = redact_state_value(redact_mapping(safe))
    return redacted


def resolved_config_view(config: ProjectConfiguration) -> dict[str, Any]:
    """Render every role through the runtime inheritance resolver."""
    expanded = config.model_dump(mode="json")
    for role in (*CANONICAL_ORDER, "env_bootstrap"):
        resolved = config.performers.resolved_role(role)
        expanded["performers"][role] = resolved.model_dump(mode="json") if resolved else None
    return expanded


def running_summary(
    config: ProjectConfiguration,
    coordinare_config: CoordinareConfiguration | None = None,
    *,
    board_columns: dict[str, list[str]] | None = None,
) -> str:
    """Describe the effective configuration without credentials."""
    configs = [(config.project_name or "default", config)]
    if coordinare_config is not None:
        configs = [
            (s.name, s.effective_config(config)) for s in coordinare_config.symphonies if s.enabled
        ]
    lines = ["", "Coordinare is running"]
    for name, effective in configs:
        columns = (board_columns or {}).get(name)
        labels = columns if columns is not None else list(BOARD_COLUMN_TO_STATUS)
        watched = [label for label in labels if BOARD_COLUMN_TO_STATUS.get(label.lower()) == "TODO"]
        watching = ", ".join(watched) if watched else "TODO (board columns unavailable)"
        lines.append(
            f"  {name}: https://github.com/orgs/{effective.github_org}/projects/{effective.github_project_number}; "
            f"watches {watching}.",
        )
        enabled = []
        skipped = []
        for role in CANONICAL_ORDER:
            resolved = effective.performers.resolved_role(role)
            if resolved is None:
                skipped.append(f"{role} (not configured)")
            elif resolved.max_concurrency == 0:
                skipped.append(f"{role} (max_concurrency: 0)")
            else:
                enabled.append(f"{role} ({resolved.backend})")
        if not enabled and not effective.performers.implementer and effective.agent_executable:
            enabled.append("implementer (legacy agent_executable)")
            skipped.remove("implementer (not configured)")
        lines.append(f"  Enabled roles: {', '.join(enabled) or 'none'}")
        lines.append(f"  Skipped roles: {', '.join(skipped) or 'none'}")
        lines.append(f"  Concurrency: {effective.max_concurrent_cards} card(s)")
    host = config.dashboard_host
    if ":" in host:
        host = f"[{host}]"
    lines.append(f"  Dashboard: http://{host}:{config.dashboard_port}")
    return "\n".join(lines)


def idle_explanation(counts: dict[str, int], skips: dict[str, Any]) -> str:
    """Explain a successful poll with no performer work, without exposing card text."""
    if any(
        (s.get("reason") or s.get("kind"))
        in {"assignee_filter", "not_owned", "ownership", "assigned_elsewhere", "unassigned"}
        for s in skips.values()
        if isinstance(s, dict)
    ):
        return "Idle: To Do cards are filtered by assignee. Check assignee_filter and include_unassigned."
    if counts.get("BLOCKED", 0):
        return "Idle: cards are blocked and need attention. Open a blocked card for its question or failure."
    if counts.get("TODO", 0):
        return "Idle: To Do cards are waiting on dependencies, filters or capacity. Open a card for its wait reason."
    if counts.get("IN_REVIEW", 0):
        return "Idle: pull requests are waiting for review or checks."
    return "Idle: No cards in To Do. Add an issue to the board and set Status to To Do or Ready."


def board_idle_explanation(
    board: dict[str, list[str]] | None,
    waits: dict[str, Any],
    sessions: dict[str, Any],
) -> str:
    """Only explain a board after a poll, and when no card can start or is running."""
    if board is None or any(has_live_work(session) for session in sessions.values()):
        return ""
    todo = board.get("TODO", [])
    if any(card not in waits for card in todo):
        return ""
    return idle_explanation({status: len(cards) for status, cards in board.items()}, waits)
