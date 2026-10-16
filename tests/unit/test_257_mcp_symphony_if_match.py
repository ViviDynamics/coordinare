"""#257: MCP symphony writes name the config version they replace.

The dashboard's symphony writes (PUT, POST, DELETE) answer 428 without an ``If-Match``
header on a file-backed config, so the MCP tools must read the version first and send
it on every write. These tests run the real dashboard ASGI app over a real config file;
nothing in the write path is stubbed.
"""

from __future__ import annotations

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
from coordinare.mcp.tools.symphony_tools import (
    UpsertSymphonyInput,
    _NameInput,
    delete_symphony_handler,
    upsert_symphony_handler,
)

TOKEN = "d" * 32
_CONCURRENT_EDIT = "\n# edited by another editor\n"
_STARTING_VERSION = 7


def _build(config_path: Path) -> tuple[Any, MagicMock]:
    """The dashboard app and its daemon double over ``config_path``: two symphonies, file-backed."""
    raw = yaml.safe_load(config_path.read_text())
    raw["github_token"] = "ghp_fixturetoken"
    symphonies = raw.get("symphonies") or []
    if len(symphonies) == 1:
        spare = {**symphonies[0], "name": "spare", "github_project_number": 99}
        raw["symphonies"] = [*symphonies, spare]
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
    daemon = MagicMock()
    daemon.state = {
        "startup_complete": True,
        "coordinare_config": cfg,
        "config_version": _STARTING_VERSION,
        "symphony_configs": {s.name: s for s in cfg.symphonies},
        "symphony_states": {s.name: {} for s in cfg.symphonies},
    }
    daemon.running = True
    daemon._cycle_active = False
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path,
    )
    return app, daemon


def _client(transport: httpx.AsyncBaseTransport) -> DashboardClient:
    config = McpServerConfig(agent_token=SecretStr("a" * 32), dashboard_token=SecretStr(TOKEN))
    return DashboardClient(config, transport=transport)


class _ConcurrentEdit(httpx.AsyncBaseTransport):
    """Forwards to the dashboard, and edits the config file once, before the first symphony write.

    Stands in for another editor saving between this tool's version read and its write.
    Every symphony write is recorded, so a test can see whether the tool retried.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, config_path: Path) -> None:
        self._inner = inner
        self._config_path = config_path
        self.writes: list[str] = []
        self.edited_text: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method in ("PUT", "POST", "DELETE") and request.url.path.startswith(
            "/api/symphonies",
        ):
            self.writes.append(f"{request.method} {request.url.path}")
            if self.edited_text is None:
                self.edited_text = self._config_path.read_text() + _CONCURRENT_EDIT
                self._config_path.write_text(self.edited_text)
        return await self._inner.handle_async_request(request)


@pytest.mark.asyncio
async def test_create_update_and_delete_write_a_file_backed_config(
    temp_config_path: Path,
) -> None:
    app, _daemon = _build(temp_config_path)
    client = _client(httpx.ASGITransport(app=app))

    created = await upsert_symphony_handler(
        UpsertSymphonyInput(name="fresh", spec={"github_project_number": 31}), client,
    )
    assert created.ok, created.error
    assert "fresh" in _names(temp_config_path)

    updated = await upsert_symphony_handler(
        UpsertSymphonyInput(name="fresh", spec={"github_project_number": 31, "enabled": False}),
        client,
    )
    assert updated.ok, updated.error
    assert _symphony(temp_config_path, "fresh")["enabled"] is False

    deleted = await delete_symphony_handler(_NameInput(name="fresh"), client)
    assert deleted.ok, deleted.error
    assert "fresh" not in _names(temp_config_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_a_stale_version_is_a_conflict_and_writes_nothing(
    temp_config_path: Path, operation: str,
) -> None:
    app, daemon = _build(temp_config_path)
    edits = _ConcurrentEdit(httpx.ASGITransport(app=app), temp_config_path)
    client = _client(edits)

    if operation == "update":
        result = await upsert_symphony_handler(
            UpsertSymphonyInput(name="demo", spec={"enabled": False}), client,
        )
    else:
        result = await delete_symphony_handler(_NameInput(name="spare"), client)

    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "conflict"
    assert result.error.status == 409
    assert "re-read" in result.error.recovery_hint
    assert result.error.detail.strip() != ""
    assert not result.error.detail.startswith("{")
    # One write attempt, no automatic retry: the agent must re-read first.
    assert len(edits.writes) == 1
    # The file is exactly what the other editor left, and memory was not touched.
    assert temp_config_path.read_text() == edits.edited_text
    assert daemon.state["config_version"] == _STARTING_VERSION
    assert daemon.state["symphony_configs"]["demo"].enabled is True
    assert "spare" in daemon.state["symphony_configs"]


@pytest.mark.asyncio
async def test_a_422_refusal_surfaces_the_servers_reason(temp_config_path: Path) -> None:
    # The start-time check reads every symphony in the candidate, not only the one written, so
    # the refusal is provoked by the spare symphony: a placeholder its override cannot resolve.
    app, daemon = _build(temp_config_path)
    spare = daemon.state["symphony_configs"]["spare"]
    daemon.state["symphony_configs"]["spare"] = spare.model_copy(
        update={"overrides": {"github_token": "${COORDINARE_SURELY_UNSET_257}"}},
    )
    client = _client(httpx.ASGITransport(app=app))
    before = temp_config_path.read_text()

    result = await upsert_symphony_handler(
        UpsertSymphonyInput(name="demo", spec={"enabled": False}), client,
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "validation"
    assert result.error.status == 422
    assert result.error.detail.startswith("Validation failed")
    assert "COORDINARE_SURELY_UNSET_257" in result.error.detail
    assert not result.error.detail.startswith("{")
    assert temp_config_path.read_text() == before
    assert daemon.state["config_version"] == _STARTING_VERSION


def _names(config_path: Path) -> list[str]:
    return [s["name"] for s in yaml.safe_load(config_path.read_text())["symphonies"]]


def _symphony(config_path: Path, name: str) -> dict[str, Any]:
    entries = yaml.safe_load(config_path.read_text())["symphonies"]
    return next(entry for entry in entries if entry["name"] == name)
