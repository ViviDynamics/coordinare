"""Spec 551 T031: persona tool contract — get/upsert/delete mappings."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from coordinare.mcp.client import DashboardClient
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.tools.persona_tools import (
    GetPersonasInput,
    UpsertPersonaInput,
    delete_persona_handler,
    get_personas_handler,
    upsert_persona_handler,
)

EXPECTED = {
    "get_personas": ("GET", "/api/personas"),
    "upsert_persona": ("PUT", "/api/personas/implementer"),
    "delete_persona": ("DELETE", "/api/personas/implementer"),
}


def make_client(handler) -> DashboardClient:
    config = McpServerConfig(
        agent_token=SecretStr("a" * 32), dashboard_token=SecretStr("d" * 32),
    )
    return DashboardClient(config, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_get_personas_mapping() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"implementer": {"instructions": "do it"}})

    result = await get_personas_handler(GetPersonasInput(), make_client(handler))
    assert result.ok and (result.data or {})
    assert seen == [EXPECTED["get_personas"]]


@pytest.mark.asyncio
async def test_upsert_persona_sends_instructions_body() -> None:
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"role": "implementer"})

    result = await upsert_persona_handler(
        UpsertPersonaInput(role="implementer", instructions="do it right"),
        make_client(handler),
    )
    assert result.ok
    method, path, body = seen[0]
    assert (method, path) == EXPECTED["upsert_persona"]
    assert body == {"instructions": "do it right"}


@pytest.mark.asyncio
async def test_delete_persona_is_empty_success() -> None:
    """204 no-content maps to an ok result with no error and no payload."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    result = await delete_persona_handler(
        GetPersonasInput(role="implementer"), make_client(handler),
    )
    assert result.ok and result.error is None


@pytest.mark.asyncio
async def test_delete_persona_requires_role() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected")

    result = await delete_persona_handler(GetPersonasInput(), make_client(handler))
    assert not result.ok
    assert result.error is not None and result.error.kind == "validation"
