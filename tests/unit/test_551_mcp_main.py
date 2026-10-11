"""Spec 551 — the ``python -m coordinare.mcp`` entry point (T014) and the
audit wrapper that sits between the registry and every tool handler."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

import coordinare.mcp.__main__ as mcp_main
from coordinare.mcp.client import ToolResult
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.server import _audit_wrapped, _extract_action

# --- argument parsing ---------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = mcp_main._parse_args([])
    assert args.transport == "http"
    assert args.listen_host is None and args.listen_port is None


def test_parse_args_overrides() -> None:
    args = mcp_main._parse_args(["--listen-host", "127.0.0.1", "--listen-port", "8092"])
    assert args.listen_host == "127.0.0.1" and args.listen_port == 8092


# --- main(): env-driven boot ----------------------------------------------------


def _env(monkeypatch: pytest.MonkeyPatch, **tokens: str) -> None:
    monkeypatch.setenv("COORDINARE_MCP_TOKEN", "a" * 32)
    monkeypatch.setenv("COORDINARE_MCP_DASHBOARD_TOKEN", "d" * 32)
    for name, value in tokens.items():
        monkeypatch.setenv(name, value)


def test_main_boots_uvicorn_with_the_env_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, COORDINARE_MCP_HOST="127.0.0.1", COORDINARE_MCP_PORT="8093")
    monkeypatch.setattr("sys.argv", ["coordinare.mcp"])
    seen: dict[str, Any] = {}

    def fake_run(app, *, host, port, log_level):
        seen.update(host=host, port=port, log_level=log_level, app=app)

    monkeypatch.setattr(mcp_main.uvicorn, "run", fake_run)
    assert mcp_main.main() == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8093
    assert seen["app"] is not None


def test_main_misconfigured_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["coordinare.mcp"])
    monkeypatch.setenv("COORDINARE_MCP_TOKEN", "short")
    assert mcp_main.main() == 2


# --- the audit wrapper -----------------------------------------------------------


class _FakeTool:
    name = "control"


@pytest.fixture
def _fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare.mcp.server import _caller_fingerprint

    _caller_fingerprint.set("fp1234")


async def test_wrapper_emits_ok_with_action_from_kwargs(
    monkeypatch: pytest.MonkeyPatch, _fingerprint: None,
) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "coordinare.mcp.server.emit_tool_invoked", lambda **e: events.append(e),
    )

    async def handler(input: Any, client: Any) -> ToolResult:
        return ToolResult(ok=True, data={})

    result = await _audit_wrapped(_FakeTool(), handler)(
        ControlLike(action="force_poll"), object(),
    )
    assert result.ok
    assert len(events) == 1
    assert events[0]["tool"] == "control"
    assert events[0]["action"] == "force_poll"
    assert events[0]["outcome"] == "ok"
    assert events[0]["credential_fingerprint"] == "fp1234"


async def test_wrapper_emits_error_from_result_dict(
    monkeypatch: pytest.MonkeyPatch, _fingerprint: None,
) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "coordinare.mcp.server.emit_tool_invoked", lambda **e: events.append(e),
    )

    async def handler() -> dict[str, Any]:
        return {"ok": False, "error": {"kind": "conflict"}}

    await _audit_wrapped(_FakeTool(), handler)()
    assert events[0]["outcome"] == "error" and events[0]["error_kind"] == "conflict"


async def test_wrapper_emits_error_and_reraises_on_exception(
    monkeypatch: pytest.MonkeyPatch, _fingerprint: None,
) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "coordinare.mcp.server.emit_tool_invoked", lambda **e: events.append(e),
    )

    async def handler() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await _audit_wrapped(_FakeTool(), handler)()
    assert events[0]["outcome"] == "error" and events[0]["error_kind"] == "ValueError"


class ControlLike:
    def __init__(self, action: str) -> None:
        self.action = action


def test_extract_action_reads_positional_input_first() -> None:
    assert _extract_action((ControlLike(action="veto"),), {}) == "veto"
    assert _extract_action((), {"action": "dry_run"}) == "dry_run"
    assert _extract_action((), {}) is None


async def test_wrapper_wraps_a_plain_dict_result(
    monkeypatch: pytest.MonkeyPatch, _fingerprint: None,
) -> None:
    """Non-ToolResult dicts pass through unchanged and are still audited."""
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "coordinare.mcp.server.emit_tool_invoked", lambda **e: events.append(e),
    )

    async def handler() -> dict[str, Any]:
        return {"ok": True, "data": {"x": 1}}

    result = await _audit_wrapped(_FakeTool(), handler)()
    assert isinstance(result, dict)
    assert len(events) == 1


def test_config_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        McpServerConfig(
            agent_token=SecretStr("a" * 32), dashboard_token=SecretStr("d" * 32),
            read_timeout_seconds=0.0,
        )
    with pytest.raises(ValidationError):
        McpServerConfig(
            agent_token=SecretStr("a" * 32), dashboard_token=SecretStr("d" * 32),
            listen_port=8090,
        )
