"""Persona tools: read, upsert, and delete performer personas (T033)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from coordinare.mcp.client import DashboardClient, ToolResult, get_client
from coordinare.mcp.tools import ToolDefinition

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
_WRITE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True,
)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


def _persona_path(role: str) -> str:
    return f"/api/personas/{quote(role, safe='')}"


class GetPersonasInput(BaseModel):
    role: str | None = None


class UpsertPersonaInput(BaseModel):
    role: str
    instructions: str


async def get_personas_handler(input: GetPersonasInput, client: DashboardClient) -> ToolResult:
    """List personas, optionally narrowed to one role (T033)."""
    return await client.get_json("/api/personas", timeout_seconds=client.read_budget)


async def get_personas(role: str | None = None) -> dict[str, Any]:
    """Read configured personas (all roles, or one role's instructions)."""
    parsed = GetPersonasInput(role=role)
    return (await get_personas_handler(parsed, get_client())).model_dump(exclude_none=True)


async def upsert_persona_handler(
    input: UpsertPersonaInput, client: DashboardClient,
) -> ToolResult:
    """Replace one role's persona instructions (T033)."""
    return await client.send_json(
        "PUT",
        _persona_path(input.role),
        json={"instructions": input.instructions},
        timeout_seconds=client.read_budget,
    )


async def upsert_persona(role: str, instructions: str) -> dict[str, Any]:
    """Set a role's persona instructions (creates or replaces)."""
    parsed = UpsertPersonaInput(role=role, instructions=instructions)
    return (await upsert_persona_handler(parsed, get_client())).model_dump(exclude_none=True)


async def delete_persona_handler(input: GetPersonasInput, client: DashboardClient) -> ToolResult:
    """Delete one role's persona instructions (T033)."""
    if not input.role:
        return ToolResult.failure(
            "validation", 400, "role is required", "supply the role whose persona to delete",
        )
    return await client.send_json(
        "DELETE", _persona_path(input.role), timeout_seconds=client.read_budget,
    )


async def delete_persona(role: str) -> dict[str, Any]:
    """Delete a role's persona instructions (destructive)."""
    return (await delete_persona_handler(GetPersonasInput(role=role), get_client())).model_dump(
        exclude_none=True,
    )


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="get_personas",
        domain="personas",
        description="Read configured personas; pass role to narrow to one role.",
        input_model=GetPersonasInput,
        annotations=_READ_ONLY,
        rest_calls=(("GET", "/api/personas"),),
        handler=get_personas,
    ),
    ToolDefinition(
        name="upsert_persona",
        domain="personas",
        description="Set a role's persona instructions (creates or replaces).",
        input_model=UpsertPersonaInput,
        annotations=_WRITE_IDEMPOTENT,
        rest_calls=(("PUT", "/api/personas/{role}"),),
        handler=upsert_persona,
    ),
    ToolDefinition(
        name="delete_persona",
        domain="personas",
        description="Delete a role's persona instructions (destructive).",
        input_model=GetPersonasInput,
        annotations=_DESTRUCTIVE,
        rest_calls=(("DELETE", "/api/personas/{role}"),),
        handler=delete_persona,
    ),
)
