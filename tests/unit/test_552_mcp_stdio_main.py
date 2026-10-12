"""Spec 552: stdio dispatch in the ``python -m coordinare.mcp`` entry point (FR2, FR4)."""

from __future__ import annotations

from typing import Any

import pytest

import coordinare.mcp.__main__ as mcp_main
from coordinare.mcp.config import McpServerConfig


def _env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setenv("COORDINARE_MCP_TOKEN", "a" * 32)
    monkeypatch.setenv("COORDINARE_MCP_DASHBOARD_TOKEN", "d" * 32)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def test_stdio_flag_runs_the_stdio_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr("sys.argv", ["coordinare.mcp", "--transport", "stdio"])
    seen: dict[str, McpServerConfig] = {}

    def fake_run_stdio(config: McpServerConfig) -> None:
        seen["config"] = config

    monkeypatch.setattr(mcp_main, "run_stdio", fake_run_stdio)
    assert mcp_main.main() == 0
    assert seen["config"].transport == "stdio"


def test_env_transport_stdio_boots_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, COORDINARE_MCP_TRANSPORT="stdio")
    monkeypatch.setattr("sys.argv", ["coordinare.mcp"])
    monkeypatch.setattr(mcp_main, "run_stdio", lambda config: None)
    assert mcp_main.main() == 0


def test_stdio_rejects_listener_flags_with_exit_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listener flags are HTTP-only; stdio inherits the shell's streams (FR4)."""
    _env(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["coordinare.mcp", "--transport", "stdio", "--listen-port", "8091"],
    )
    seen: dict[str, Any] = {}
    monkeypatch.setattr(mcp_main, "run_stdio", lambda config: seen.update(ran=True))
    assert mcp_main.main() == 2
    assert not seen


def test_http_flag_overrides_env_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit --transport http wins over COORDINARE_MCP_TRANSPORT=stdio."""
    _env(monkeypatch, COORDINARE_MCP_TRANSPORT="stdio")
    monkeypatch.setattr("sys.argv", ["coordinare.mcp", "--transport", "http"])
    seen: dict[str, Any] = {}

    def fake_run(app: Any, *, host: str, port: int, log_level: str) -> None:
        seen["booted"] = True

    monkeypatch.setattr(mcp_main.uvicorn, "run", fake_run)
    assert mcp_main.main() == 0
    assert seen["booted"]
