from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
import yaml

from coordinare import __main__ as cli
from coordinare.config import ProjectConfiguration
from coordinare.doctor import CheckResult, DoctorReport, Status
from coordinare.services.config_write_service import compute_content_hash
from coordinare.services.event_capture import event_store_processor, install_event_store
from tests.unit.dashboard_js_slices import shipped_function
from tests.unit.test_dashboard_config_api import _make_client


class _Select(HTMLParser):
    """Read the selected value from shipped markup without running a browser."""

    def __init__(self) -> None:
        super().__init__()
        self.attrs: dict[str, Any] = {}
        self.options: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "select":
            self.attrs = dict(attrs)
        elif tag == "option":
            self.options.append(dict(attrs))

    @property
    def value(self) -> str:
        selected = next((option for option in self.options if "selected" in option), self.options[0])
        return selected["value"]


def _node(body: str, payload: Any) -> Any:
    node = shutil.which("node")
    assert node is not None
    functions = ["cfgInput", "cfgCoerce", "cfgCollectChanges", "cfgSameValue", "cfgUnset",
                 "cfgParseSaved", "cfgSecretChanged"]
    script = "\n".join([
        shipped_function("helpers", "esc"),
        *(shipped_function("config", name) for name in functions),
        "var _CFG_REFS = {}, CFG_INPUT = '', CFG_SECRET_MASK = '••••••';",
        "const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));",
        body,
    ])
    result = subprocess.run([node, "-e", script], input=json.dumps(payload), capture_output=True,
                            text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("clear,stale", [(False, True), (True, True), (True, False)])
def test_200_full_config_settings_preserve_unset_preset(
    temp_config_path: Path, monkeypatch: pytest.MonkeyPatch, clear: bool, stale: bool,
) -> None:
    """Exercise the shared cause of the three real-server #143 browser failures."""
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "fixture-token")
    raw = yaml.safe_load(temp_config_path.read_text())
    raw["dashboard_auth_token"] = "stored-dashboard-token" * 2
    temp_config_path.write_text(yaml.safe_dump(raw))
    client = _make_client(temp_config_path, raw)
    snapshot = client.get("/api/config/all").json()
    settings = next(section for section in snapshot["sections"] if section["id"] == "global")["settings"]
    preset = next(setting for setting in settings if setting["key"] == "global.preset")
    assert preset["current_value"] is None
    assert preset["nullable"] is True
    markup = _node("console.log(JSON.stringify(cfgInput(payload)));", preset)
    select = _Select()
    select.feed(markup)
    assert select.value == ""
    changes = _node("""
const select = {value: payload.value, getAttribute: key => payload.attrs[key] ?? null,
                closest: () => null};
const changes = cfgCollectChanges({querySelectorAll: () => [select]});
changes.poll_interval_seconds = 45;
if (payload.clear) changes.dashboard_auth_token = '';
console.log(JSON.stringify(changes));
""", {"value": select.value, "attrs": select.attrs, "clear": clear})
    assert changes == {"poll_interval_seconds": 45, **({"dashboard_auth_token": ""} if clear else {})}
    request = {"section": "global", "changes": changes, "base_hash": snapshot["content_hashes"]["config_yaml"]}
    if stale:
        temp_config_path.write_text(temp_config_path.read_text() + "\n# edited elsewhere\n")
        response = client.put("/api/config/section/global", json=request)
        assert response.status_code == 409, response.text
        assert response.json()["conflict"] is True
        assert yaml.safe_load(temp_config_path.read_text())["dashboard_auth_token"] == raw["dashboard_auth_token"]
        request["base_hash"] = compute_content_hash(temp_config_path)
    response = client.put("/api/config/section/global", json=request)
    assert response.status_code == 200, response.text
    saved = yaml.safe_load(temp_config_path.read_text())
    assert saved["poll_interval_seconds"] == 45
    assert "preset" not in saved
    if clear:
        assert "dashboard_auth_token" not in saved
    else:
        assert saved["dashboard_auth_token"] == raw["dashboard_auth_token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_200_preflight_failure_is_captured_and_flushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool,
) -> None:
    install_event_store(None)
    monkeypatch.chdir(tmp_path)
    config = ProjectConfiguration(github_org="example", github_token="test-token",
                                  human_reviewers=["operator"], state_file_path=tmp_path / "state.json")
    runtime = AsyncMock()
    monkeypatch.setattr(cli, "_run_with_events", runtime)

    async def preflight(*args: Any) -> DoctorReport:
        structlog.get_logger().error("test.preflight_failed")
        if raises:
            raise RuntimeError("preflight crashed")
        return DoctorReport([CheckResult("port", Status.FAIL, "busy", "change dashboard_port")])

    monkeypatch.setattr(cli, "run_preflight", preflight)
    with pytest.raises(RuntimeError if raises else SystemExit):
        await cli._run(config)
    runtime.assert_not_called()
    with sqlite3.connect(tmp_path / "events.db") as db:
        kinds = {record[0] for record in db.execute("SELECT kind FROM events")}
        assert "test.preflight_failed" in kinds
        if not raises:
            assert "startup.preflight_failed" in kinds
    event_store_processor(None, "error", {"event": "after.close"})
    with sqlite3.connect(tmp_path / "events.db") as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind = 'after.close'").fetchone()[0] == 0
