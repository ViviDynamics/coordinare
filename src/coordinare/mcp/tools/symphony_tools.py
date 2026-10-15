"""Symphony tools: list, read, upsert, validate, and delete symphonies (T032)."""

from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import quote

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from coordinare.mcp.client import DashboardClient, ToolResult, get_client
from coordinare.mcp.tools._registry import ToolDefinition

_HTTP_NOT_FOUND = 404
_HTTP_UNPROCESSABLE = 422
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
_WRITE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True,
)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


class _NoParams(BaseModel):
    """Input shape for tools that take no parameters."""


def _symphony_path(name: str, suffix: str = "") -> str:
    return f"/api/symphonies/{quote(name, safe='')}{suffix}"


#: 257: every symphony write names the config version it replaces. The list GET carries that
#: version as its ETag, and it exists even when the symphony does not, so one read serves a
#: create, an update and a delete alike.
_VERSION_PATH = "/api/symphonies"

_CONFLICT_HINT = (
    "the config changed, or a cycle is running, since this read; re-read with list_symphonies "
    "or get_symphony and retry the write. Nothing was written."
)
_REFUSED_HINT = (
    "the daemon refuses this symphony as written; fix the reason it names and retry. "
    "Nothing was written."
)


def _server_reason(text: str) -> str:
    """The reason the daemon gave, from its JSON ``error`` field, or the body as sent."""
    try:
        body = json.loads(text)
    except ValueError:
        return text
    reason = body.get("error") if isinstance(body, dict) else None
    return reason if isinstance(reason, str) else text


def _write_outcome(result: ToolResult) -> ToolResult:
    """A refused symphony write, in the terms the agent acts on (257).

    A 409 is a version conflict: nothing was written, and the agent must re-read before it
    tries again, so there is no automatic retry. A 422 is the daemon's start-time or
    validation check: its reason is what the agent needs, so it is lifted out of the body.
    """
    error = result.error
    if result.ok or error is None:
        return result
    if error.kind == "conflict":
        return ToolResult.failure(
            "conflict", error.status, _server_reason(error.detail), _CONFLICT_HINT,
        )
    if error.status == _HTTP_UNPROCESSABLE:
        return ToolResult.failure(
            "validation", error.status, _server_reason(error.detail), _REFUSED_HINT,
        )
    return result


async def _current_version(client: DashboardClient) -> tuple[ToolResult | None, str | None]:
    """The config version to send as ``If-Match``, or the failure that stopped the read.

    ``None`` as the version means the dashboard has no config file to guard, and the write
    goes without a header: the daemon asks for one only where there is a file to overwrite.
    """
    read, version = await client.get_with_version(_VERSION_PATH, timeout_seconds=client.read_budget)
    if not read.ok:
        return read, None
    return None, version


class _NameInput(BaseModel):
    name: str


class UpsertSymphonyInput(BaseModel):
    name: str
    spec: dict[str, Any] | None = None
    bootstrap: Literal["env", "wiki"] | None = None


class ValidateSymphonyInput(BaseModel):
    name: str
    spec: dict[str, Any] = {}


async def list_symphonies_handler(client: DashboardClient) -> ToolResult:
    """List configured symphonies from the dashboard config (T031)."""
    return await client.get_json("/api/symphonies", timeout_seconds=client.read_budget)


async def list_symphonies() -> dict[str, Any]:
    """List every configured symphony."""
    return (await list_symphonies_handler(get_client())).model_dump(exclude_none=True)


async def get_symphony_handler(input: _NameInput, client: DashboardClient) -> ToolResult:
    """Fetch one symphony's full configuration (T031)."""
    return await client.get_json(
        _symphony_path(input.name), timeout_seconds=client.read_budget,
    )


async def get_symphony(name: str) -> dict[str, Any]:
    """Read one symphony's full configuration by name."""
    return (await get_symphony_handler(_NameInput(name=name), get_client())).model_dump(
        exclude_none=True,
    )


async def upsert_symphony_handler(
    input: UpsertSymphonyInput, client: DashboardClient,
) -> ToolResult:
    """Write a symphony, then chain the requested bootstrap action (T032).

    The write (PUT with POST-create fallback) runs first; the bootstrap POST
    runs only after a successful write, and the first failure aborts the chain
    and is surfaced verbatim (contracts/mcp-tools.md).
    """
    if input.spec is None:
        return ToolResult.failure(
            "validation", 400, "upsert requires spec", "supply the symphony spec object",
        )
    version_failure, version = await _current_version(client)
    if version_failure is not None:
        return version_failure
    spec = dict(input.spec)
    write = await client.send_json(
        "PUT", _symphony_path(input.name), json=spec, timeout_seconds=client.read_budget,
        if_match=version,
    )
    if not write.ok and write.error is not None and write.error.status == _HTTP_NOT_FOUND:
        spec.setdefault("name", input.name)
        write = await client.send_json(
            "POST", _VERSION_PATH, json=spec, timeout_seconds=client.read_budget,
            if_match=version,
        )
    write = _write_outcome(write)
    if not write.ok or input.bootstrap is None:
        return write
    suffix = "/env-bootstrap" if input.bootstrap == "env" else "/wiki-init"
    bootstrap = await client.send_json(
        "POST", _symphony_path(input.name, suffix), timeout_seconds=client.lifecycle_budget,
    )
    if not bootstrap.ok:
        return bootstrap
    return ToolResult(ok=True, data={"write": write.data, "bootstrap": bootstrap.data})


async def upsert_symphony(
    name: str, spec: dict[str, Any] | None = None, bootstrap: str | None = None,
) -> dict[str, Any]:
    """Create or replace one symphony; optionally chain env or wiki bootstrap after it."""
    parsed = UpsertSymphonyInput.model_validate(
        {"name": name, "spec": spec, "bootstrap": bootstrap},
    )
    return (await upsert_symphony_handler(parsed, get_client())).model_dump(exclude_none=True)


async def validate_symphony_handler(
    input: ValidateSymphonyInput, client: DashboardClient,
) -> ToolResult:
    """Validate a symphony spec against the loaded config without saving (T032)."""
    return await client.send_json(
        "POST", _symphony_path(input.name, "/validate"),
        json=dict(input.spec), timeout_seconds=client.read_budget,
    )


async def validate_symphony(name: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a symphony spec without saving; returns errors as data."""
    parsed = ValidateSymphonyInput(name=name, spec=spec or {})
    return (await validate_symphony_handler(parsed, get_client())).model_dump(exclude_none=True)


async def delete_symphony_handler(input: _NameInput, client: DashboardClient) -> ToolResult:
    """Delete a symphony from the persisted config (T032, 257: guarded by If-Match)."""
    version_failure, version = await _current_version(client)
    if version_failure is not None:
        return version_failure
    deleted = await client.send_json(
        "DELETE", _symphony_path(input.name), timeout_seconds=client.read_budget,
        if_match=version,
    )
    return _write_outcome(deleted)


async def delete_symphony(name: str) -> dict[str, Any]:
    """Delete a symphony by name (destructive)."""
    return (await delete_symphony_handler(_NameInput(name=name), get_client())).model_dump(
        exclude_none=True,
    )


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="list_symphonies",
        domain="symphonies",
        description="List every configured symphony.",
        input_model=_NoParams,
        annotations=_READ_ONLY,
        rest_calls=(("GET", "/api/symphonies"),),
        handler=list_symphonies,
    ),
    ToolDefinition(
        name="get_symphony",
        domain="symphonies",
        description="Read one symphony's full configuration by name.",
        input_model=_NameInput,
        annotations=_READ_ONLY,
        rest_calls=(("GET", "/api/symphonies/{name}"),),
        handler=get_symphony,
    ),
    ToolDefinition(
        name="upsert_symphony",
        domain="symphonies",
        description=(
            "Create or replace one symphony spec (PUT with POST-create fallback); "
            "optionally chain env or wiki bootstrap after the write."
        ),
        input_model=UpsertSymphonyInput,
        annotations=_WRITE_IDEMPOTENT,
        rest_calls=(
            ("PUT", "/api/symphonies/{name}"),
            ("POST", "/api/symphonies"),
            ("POST", "/api/symphonies/{name}/env-bootstrap"),
            ("POST", "/api/symphonies/{name}/wiki-init"),
        ),
        handler=upsert_symphony,
    ),
    ToolDefinition(
        name="validate_symphony",
        domain="symphonies",
        description="Validate a symphony spec against the loaded config without saving.",
        input_model=ValidateSymphonyInput,
        annotations=_READ_ONLY,
        rest_calls=(("POST", "/api/symphonies/{name}/validate"),),
        handler=validate_symphony,
    ),
    ToolDefinition(
        name="delete_symphony",
        domain="symphonies",
        description="Delete a symphony by name (destructive).",
        input_model=_NameInput,
        annotations=_DESTRUCTIVE,
        rest_calls=(("DELETE", "/api/symphonies/{name}"),),
        handler=delete_symphony,
    ),
)
