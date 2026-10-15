"""Config tools: get, validate, and update the coordinare configuration (US2).

REST mappings per contracts/mcp-tools.md; every handler returns a
:class:`ToolResult` and the agent-facing function serialises it with
``exclude_none`` so a success carries no ``error`` key and vice versa.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from coordinare.mcp.client import ToolResult, get_client
from coordinare.mcp.tools._registry import ToolDefinition

if TYPE_CHECKING:
    from coordinare.mcp.client import DashboardClient

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)

#: GET path template per get_config scope; ``{id}`` requires the id param.
_GET_PATHS: dict[str, str] = {
    "all": "/api/config/all",
    "effective": "/api/config/effective",
    "global": "/api/config/global",
    "section": "/api/config/section/{id}",
    "catalog": "/api/config/catalog/{id}",
    "routing": "/api/config/routing",
}

#: (verb, path template, needs base_hash) per update_config action.
_ACTION_ROUTES: dict[str, tuple[str, str, bool]] = {
    "put_global": ("PUT", "/api/config/global", True),
    "put_section": ("PUT", "/api/config/section/{id}", True),
    "catalog_create": ("POST", "/api/config/catalog/{id}", False),
    "catalog_update": ("PUT", "/api/config/catalog/{id}/{item_id}", True),
    "catalog_delete": ("DELETE", "/api/config/catalog/{id}/{item_id}", True),
    "routing_create": ("POST", "/api/config/routing/entry", False),
    "routing_update": ("PUT", "/api/config/routing/entry/{index}", False),
    "routing_delete": ("DELETE", "/api/config/routing/entry/{index}", False),
    "reload": ("POST", "/api/config/reload", False),
}

UpdateAction = Literal[
    "put_global", "put_section", "catalog_create", "catalog_update", "catalog_delete",
    "routing_create", "routing_update", "routing_delete", "reload",
]

#: The routing actions that name an existing entry by its index (225).
_ROUTING_ENTRY_ACTIONS: frozenset[str] = frozenset({"routing_update", "routing_delete"})


def _fill(template: str, **params: str | int | None) -> str | None:
    path = template
    for key, value in params.items():
        placeholder = f"{{{key}}}"
        if placeholder in path:
            if value in (None, ""):
                return None
            path = path.replace(placeholder, quote(str(value), safe=""))
    return path


class GetConfigInput(BaseModel):
    scope: Literal["all", "effective", "global", "section", "catalog", "routing"]
    id: str | None = None


async def get_config_handler(
    input: GetConfigInput, client: DashboardClient,
) -> ToolResult:
    """Read the redacted configuration at the requested scope (T020)."""
    path = _fill(_GET_PATHS[input.scope], id=input.id)
    if path is None:
        return ToolResult.failure(
            "validation",
            400,
            f"scope '{input.scope}' requires an 'id' (section id or catalog name)",
            "pass the section id or catalog name in the 'id' parameter",
        )
    return await client.get_json(path, timeout_seconds=client.read_budget)


async def get_config(scope: str, id: str | None = None) -> dict[str, Any]:
    """Read the redacted coordinare configuration (all/effective/global/section/catalog/routing)."""
    parsed = GetConfigInput.model_validate({'scope': scope, 'id': id})
    return (await get_config_handler(parsed, get_client())).model_dump(exclude_none=True)


class RoutingIdentity(BaseModel):
    """The routing entry a write means, by its backend and model (225).

    ``get_config`` with scope ``routing`` lists every entry with these two fields, so an agent
    can take them from the same read that gave it ``index`` and ``content_hash``.
    """

    backend: str
    model: str


class UpdateConfigInput(BaseModel):
    action: UpdateAction
    id: str | None = None
    item_id: str | None = None
    index: int | None = None
    payload: dict[str, Any] | None = None
    base_hash: str | None = None
    #: 225: for routing_update / routing_delete, the entry the write means. The dashboard refuses
    #: the write (409) when the entry at ``index`` is a different one. Optional, so a caller that
    #: omits it is still guarded by ``base_hash``.
    expected: RoutingIdentity | None = None


def _update_body(input: UpdateConfigInput) -> dict[str, Any]:
    """Shape the request body per the dashboard's per-route conventions."""
    payload = input.payload or {}
    base = input.base_hash
    body_by_action: dict[str, dict[str, Any]] = {
        "put_global": {**payload, "expected_hash": base},
        "put_section": {"changes": payload, "base_hash": base},
        "catalog_create": {"item": payload, "base_hash": base},
        "catalog_update": {"changes": payload, "base_hash": base},
        "catalog_delete": {"base_hash": base},
        "routing_create": {"entry": payload, "base_hash": base},
        "routing_update": {"changes": payload, "base_hash": base},
        "routing_delete": {"base_hash": base},
    }
    body = body_by_action.get(input.action, {})  # reload → {}
    if input.expected is not None and input.action in _ROUTING_ENTRY_ACTIONS:
        body = {**body, "expected": input.expected.model_dump()}
    return body


async def update_config_handler(
    input: UpdateConfigInput, client: DashboardClient,
) -> ToolResult:
    """Write configuration with optimistic concurrency (T020)."""
    _verb, template, needs_hash = _ACTION_ROUTES[input.action]
    path = _fill(template, id=input.id, item_id=input.item_id, index=input.index)
    if path is None:
        missing = [
            name
            for name in ("id", "item_id", "index")
            if f"{{{name}}}" in template
        ]
        return ToolResult.failure(
            "validation",
            400,
            f"action '{input.action}' requires {missing}",
            "read the latest values via get_config first; its result carries base_hash",
        )
    if needs_hash and not input.base_hash:
        return ToolResult.failure(
            "validation",
            400,
            f"action '{input.action}' requires base_hash",
            "read the latest values via get_config first; its result carries base_hash",
        )
    if input.expected is not None and input.action not in _ROUTING_ENTRY_ACTIONS:
        return ToolResult.failure(
            "validation",
            400,
            "expected names a routing entry, so it applies only to routing_update and routing_delete",
            "remove expected, or use a routing action",
        )
    body = _update_body(input)
    return await client.send_json(
        _verb, path, json=body, timeout_seconds=client.read_budget,
    )


async def update_config(action: str, **kwargs: Any) -> dict[str, Any]:
    """Write configuration with optimistic concurrency (put/section/catalog/routing/reload).

    Keyword arguments mirror ``UpdateConfigInput``: the optional ``id``,
    ``item_id``, ``index``, ``payload``, ``base_hash`` and, for routing_update and
    routing_delete, ``expected`` (``{"backend": ..., "model": ...}``).
    """
    parsed = UpdateConfigInput.model_validate({"action": action, **kwargs})
    return (await update_config_handler(parsed, get_client())).model_dump(exclude_none=True)


class ValidateConfigInput(BaseModel):
    payload: dict[str, Any]


async def validate_config_handler(
    input: ValidateConfigInput, client: DashboardClient,
) -> ToolResult:
    """Validate a config payload without writing (T020)."""
    return await client.send_json(
        "POST", "/api/config/validate", json=input.payload, timeout_seconds=client.read_budget,
    )


async def validate_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a config payload against the daemon's own preflight; writes nothing."""
    parsed = ValidateConfigInput(payload=payload)
    return (await validate_config_handler(parsed, get_client())).model_dump(exclude_none=True)


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="get_config",
        domain="config",
        description="Read the redacted coordinare configuration at one of several scopes.",
        input_model=GetConfigInput,
        annotations=_READ_ONLY,
        rest_calls=(
            ("GET", "/api/config/all"),
            ("GET", "/api/config/effective"),
            ("GET", "/api/config/global"),
            ("GET", "/api/config/section/{section_id}"),
            ("GET", "/api/config/catalog/{catalog}"),
            ("GET", "/api/config/routing"),
        ),
        handler=get_config,
    ),
    ToolDefinition(
        name="update_config",
        domain="config",
        description=(
            "Write configuration with optimistic concurrency (base_hash from get_config)."
        ),
        input_model=UpdateConfigInput,
        annotations=_DESTRUCTIVE,
        rest_calls=(
            ("PUT", "/api/config/global"),
            ("PUT", "/api/config/section/{section_id}"),
            ("POST", "/api/config/catalog/{catalog}"),
            ("PUT", "/api/config/catalog/{catalog}/{item_id}"),
            ("DELETE", "/api/config/catalog/{catalog}/{item_id}"),
            ("POST", "/api/config/routing/entry"),
            ("PUT", "/api/config/routing/entry/{index}"),
            ("DELETE", "/api/config/routing/entry/{index}"),
            ("POST", "/api/config/reload"),
        ),
        handler=update_config,
    ),
    ToolDefinition(
        name="validate_config",
        domain="config",
        description="Validate a config payload without writing.",
        input_model=ValidateConfigInput,
        annotations=_READ_ONLY,
        rest_calls=(("POST", "/api/config/validate"),),
        handler=validate_config,
    ),
)
