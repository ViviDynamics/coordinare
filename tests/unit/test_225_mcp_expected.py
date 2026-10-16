"""#225: MCP routing writes name their entry, and the dashboard checks it.

``update_config`` on a routing entry sends ``expected`` (the entry's backend and model) so the
write gets the same identity check as the Settings page. An old caller that omits it is still
guarded by ``base_hash``. ``expected`` on any other action is refused before any request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from pydantic import SecretStr

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.mcp.client import DashboardClient
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.tools.config_tools import (
    GetConfigInput,
    RoutingIdentity,
    UpdateConfigInput,
    get_config_handler,
    update_config,
    update_config_handler,
)

TOKEN = "d" * 32
_VLLM = RoutingIdentity(backend="vllm", model="qwen2.5-coder")


def _config() -> McpServerConfig:
    return McpServerConfig(agent_token=SecretStr("a" * 32), dashboard_token=SecretStr(TOKEN))


def _recording_client(seen: list[dict[str, Any]]) -> DashboardClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append({"method": request.method, "path": request.url.path, "body": body})
        return httpx.Response(200, json={"ok": True})

    return DashboardClient(_config(), transport=httpx.MockTransport(handler))


def _routing_input(action: str, **kwargs: Any) -> UpdateConfigInput:
    return UpdateConfigInput.model_validate({"action": action, "index": 0, "base_hash": "h", **kwargs})


@pytest.mark.asyncio
async def test_routing_update_sends_the_entry_it_means() -> None:
    seen: list[dict[str, Any]] = []
    result = await update_config_handler(
        _routing_input("routing_update", payload={"model": "qwen-32b"}, expected=_VLLM),
        _recording_client(seen),
    )
    assert result.ok, result.error
    assert seen == [{
        "method": "PUT", "path": "/api/config/routing/entry/0",
        "body": {"changes": {"model": "qwen-32b"}, "base_hash": "h",
                 "expected": {"backend": "vllm", "model": "qwen2.5-coder"}},
    }]


@pytest.mark.asyncio
async def test_routing_delete_sends_the_entry_it_means() -> None:
    seen: list[dict[str, Any]] = []
    result = await update_config_handler(
        _routing_input("routing_delete", expected=_VLLM), _recording_client(seen),
    )
    assert result.ok, result.error
    assert seen == [{
        "method": "DELETE", "path": "/api/config/routing/entry/0",
        "body": {"base_hash": "h", "expected": {"backend": "vllm", "model": "qwen2.5-coder"}},
    }]


@pytest.mark.asyncio
async def test_an_old_caller_without_expected_sends_the_old_body() -> None:
    """Omitting expected keeps the request exactly as it was: the hash guard still covers it."""
    seen: list[dict[str, Any]] = []
    client = _recording_client(seen)
    await update_config_handler(_routing_input("routing_update", payload={"model": "m"}), client)
    await update_config_handler(_routing_input("routing_delete"), client)
    assert seen[0]["body"] == {"changes": {"model": "m"}, "base_hash": "h"}
    assert seen[1]["body"] == {"base_hash": "h"}
    assert all("expected" not in entry["body"] for entry in seen)


@pytest.mark.asyncio
async def test_expected_on_a_non_routing_action_is_refused_before_any_request() -> None:
    seen: list[dict[str, Any]] = []
    result = await update_config_handler(
        _routing_input("put_global", id=None, payload={"poll_interval_seconds": 30}, expected=_VLLM),
        _recording_client(seen),
    )
    assert not result.ok
    assert result.error is not None and result.error.kind == "validation"
    assert seen == []


def test_expected_must_name_backend_and_model() -> None:
    with pytest.raises(ValueError):
        UpdateConfigInput.model_validate({"action": "routing_update", "index": 0, "expected": {"backend": "vllm"}})


def test_the_mcp_schema_exposes_expected() -> None:
    properties = UpdateConfigInput.model_json_schema()["properties"]
    assert "expected" in properties


def _app(config_path: Path, raw: dict[str, Any]) -> Any:
    validated_raw = {**raw, "github_token": "ghp_fixturetoken"}
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(validated_raw))
    daemon = MagicMock()
    daemon.state = {"startup_complete": True, "coordinare_config": cfg, "config_version": 7}
    daemon.running = True
    return create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path,
    )


@pytest.mark.asyncio
async def test_the_dashboard_refuses_an_mcp_write_that_names_another_entry(
    temp_config_with_routing: tuple[Path, Path],
) -> None:
    config_path, routing_path = temp_config_with_routing
    raw = yaml.safe_load(config_path.read_text())
    client = DashboardClient(_config(), transport=httpx.ASGITransport(app=_app(config_path, raw)))
    before = routing_path.read_text()

    read = await get_config_handler(GetConfigInput(scope="routing"), client)
    assert read.ok and read.data is not None
    base_hash = read.data["content_hash"]

    refused = await update_config_handler(
        UpdateConfigInput(
            action="routing_update", index=0, base_hash=base_hash,
            payload={"model": "overwritten"},
            expected=RoutingIdentity(backend="not-vllm", model="gone-model"),
        ),
        client,
    )
    assert not refused.ok
    assert refused.error is not None and refused.error.kind == "conflict"
    assert routing_path.read_text() == before

    refused_delete = await update_config_handler(
        UpdateConfigInput(
            action="routing_delete", index=0, base_hash=base_hash,
            expected=RoutingIdentity(backend="not-vllm", model="gone-model"),
        ),
        client,
    )
    assert not refused_delete.ok
    assert refused_delete.error is not None and refused_delete.error.kind == "conflict"
    assert routing_path.read_text() == before


@pytest.mark.asyncio
async def test_the_dashboard_applies_an_mcp_write_that_names_the_entry_at_its_index(
    temp_config_with_routing: tuple[Path, Path],
) -> None:
    config_path, routing_path = temp_config_with_routing
    raw = yaml.safe_load(config_path.read_text())
    client = DashboardClient(_config(), transport=httpx.ASGITransport(app=_app(config_path, raw)))

    read = await get_config_handler(GetConfigInput(scope="routing"), client)
    assert read.ok and read.data is not None
    entry = read.data["entries"][0]

    applied = await update_config_handler(
        UpdateConfigInput(
            action="routing_update", index=0, base_hash=read.data["content_hash"],
            payload={"model": "qwen2.5-coder-32b"},
            expected=RoutingIdentity(backend=entry["backend"], model=entry["model"]),
        ),
        client,
    )
    assert applied.ok, applied.error
    assert yaml.safe_load(routing_path.read_text())["entries"][0]["model"] == "qwen2.5-coder-32b"


def test_routing_identity_refuses_an_extra_key() -> None:
    with pytest.raises(ValueError):
        RoutingIdentity.model_validate({"backend": "vllm", "model": "m", "index": 9})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected",
    [
        {"backend": "vllm", "model": "m", "index": 9},  # an extra key
        "vllm/m",  # not an object
        {"backend": "vllm"},  # missing model
        {"backend": 1, "model": "m"},  # wrong type
    ],
)
async def test_a_malformed_expected_is_a_tool_error_not_an_exception(expected: Any) -> None:
    """The agent-facing update_config returns the error as a result; it never raises ValidationError."""
    result = await update_config(
        "routing_update", index=0, base_hash="h", payload={"model": "m"}, expected=expected,
    )

    assert result["ok"] is False
    assert result["error"]["kind"] == "validation"
    assert "update_config argument 'expected" in result["error"]["detail"]


@pytest.mark.asyncio
async def test_a_whole_routing_entry_in_expected_names_the_allowed_keys() -> None:
    """An agent that copies a whole entry gets a 400-class error that says exactly what is allowed."""
    from coordinare.mcp.tools.config_tools import update_config

    result = await update_config(
        "routing_update", index=0, base_hash="h", payload={"model": "m"},
        expected={"backend": "vllm", "model": "m", "target": {"base_url": "http://x"}},
    )

    assert result["ok"] is False
    assert result["error"]["kind"] == "validation"
    assert result["error"]["status"] == 400
    detail = result["error"]["detail"]
    assert "backend and model" in detail
    assert "remove target" in detail
