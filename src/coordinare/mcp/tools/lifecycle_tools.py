"""Lifecycle tools: card and role control actions (US4, T028)."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from coordinare.mcp.client import DashboardClient, ToolResult, get_client
from coordinare.mcp.tools import ToolDefinition

#: FR5: control actions get the lifecycle budget, not the read budget.
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


class ControlInput(BaseModel):
    action: Literal["force_poll", "cancel", "skip_role", "restart_from", "veto", "dry_run"]
    card_id: str | None = None
    role: str | None = None
    reason: str | None = None


def _query(**params: str | None) -> str:
    parts = [
        f"{key}={quote(value, safe='')}"
        for key, value in params.items()
        if value is not None
    ]
    return f"?{'&'.join(parts)}" if parts else ""


async def control_handler(input: ControlInput, client: DashboardClient) -> ToolResult:
    """Dispatch one lifecycle control action against the dashboard API."""
    action = input.action
    if action == "force_poll":
        path = "/api/force-poll"
    elif action == "cancel":
        path = "/api/cancel"
    elif action == "skip_role":
        if not input.card_id:
            return _missing("card_id")
        path = f"/api/skip-role{_query(card_id=input.card_id, reason=input.reason)}"
    elif action == "restart_from":
        if not input.card_id:
            return _missing("card_id")
        if not input.role:
            return _missing("role")
        path = f"/api/restart-from/{quote(input.role, safe='')}{_query(card_id=input.card_id, reason=input.reason)}"
    elif action == "veto":
        if not input.card_id:
            return _missing("card_id")
        path = f"/api/veto{_query(card_id=input.card_id, reason=input.reason)}"
    else:
        if not input.card_id:
            return _missing("card_id")
        path = f"/api/dry-run/{quote(input.card_id, safe='')}"
    return await client.send_json("POST", path, timeout_seconds=client.lifecycle_budget)


def _missing(param: str) -> ToolResult:
    return ToolResult.failure(
        "validation", 400, f"action requires {param}", f"supply {param} for this action",
    )


async def control(
    action: str, card_id: str | None = None, role: str | None = None, reason: str | None = None,
) -> dict[str, Any]:
    """Run a lifecycle control action (force_poll, cancel, skip_role, restart_from, veto, dry_run)."""
    parsed = ControlInput.model_validate(
        {"action": action, "card_id": card_id, "role": role, "reason": reason},
    )
    return (await control_handler(parsed, get_client())).model_dump(exclude_none=True)


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="control",
        domain="lifecycle",
        description=(
            "Lifecycle control: force_poll, cancel, skip_role (card_id), "
            "restart_from (card_id + role), veto (card_id), dry_run (card_id)."
        ),
        input_model=ControlInput,
        annotations=_DESTRUCTIVE,
        rest_calls=(
            ("POST", "/api/force-poll"),
            ("POST", "/api/cancel"),
            ("POST", "/api/skip-role"),
            ("POST", "/api/restart-from/{role}"),
            ("POST", "/api/veto"),
            ("POST", "/api/dry-run/{card_id}"),
        ),
        handler=control,
    ),
)
