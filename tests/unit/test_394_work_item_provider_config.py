"""Issue #394 — the work item provider is chosen in config, GitHub Projects by default.

Until now ``GitHubProjectsBoardProvider`` was constructed directly, once in
``__main__.py`` and twice in ``daemon.py``, so a second provider had no way in.
A symphony now names its provider in an optional ``work_items`` block, one factory
builds it, and every construction site calls that factory. A config with no block
is GitHub Projects and behaves exactly as before.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from coordinare.config import CoordinareConfiguration, SymphonyConfig, WorkItemsConfig
from coordinare.config_validation import coerce_multi_symphony_raw, validate_raw_config
from coordinare.services.board_provider import GitHubProjectsBoardProvider
from coordinare.services.work_item_providers import (
    DEFAULT_WORK_ITEM_PROVIDER,
    WORK_ITEM_PROVIDERS,
    build_board_provider,
)

_RAW: dict[str, Any] = {
    "github_org": "acme",
    "github_token": "ghp_fixturetoken",
    "human_reviewers": ["alice"],
    "symphonies": [{"name": "demo", "github_project_number": 100}],
}


def _with_work_items(work_items: dict[str, Any]) -> dict[str, Any]:
    return {**_RAW, "symphonies": [{**_RAW["symphonies"][0], "work_items": work_items}]}


# --- the config block ---------------------------------------------------------------------


class TestTheConfigBlock:
    def test_no_block_means_github_projects(self) -> None:
        cfg = SymphonyConfig(name="demo", github_project_number=100)

        assert cfg.work_items is None
        assert cfg.work_item_provider == "github_projects"
        assert DEFAULT_WORK_ITEM_PROVIDER == "github_projects"

    def test_github_projects_can_be_named_explicitly(self) -> None:
        cfg = SymphonyConfig(name="demo", github_project_number=100, work_items={"provider": "github_projects"})

        assert cfg.work_items == WorkItemsConfig(provider="github_projects")
        assert cfg.work_item_provider == "github_projects"

    def test_an_existing_config_is_written_back_without_a_block(self) -> None:
        """A save from the editor must not add a block the operator never wrote."""
        cfg = SymphonyConfig(name="demo", github_project_number=100)
        assert "work_items" not in cfg.model_dump(exclude_none=True)

    def test_an_unknown_provider_is_refused_and_named(self) -> None:
        with pytest.raises(ValueError, match="jira") as excinfo:
            SymphonyConfig(name="demo", github_project_number=100, work_items={"provider": "jira"})
        assert "github_projects" in str(excinfo.value), "the error should say what is accepted"

    def test_an_unknown_key_in_the_block_is_refused(self) -> None:
        with pytest.raises(ValueError, match="extra"):
            SymphonyConfig(name="demo", github_project_number=100, work_items={"provider": "github_projects", "board": 3})


class TestValidate:
    def test_a_config_with_no_block_validates(self) -> None:
        assert validate_raw_config(dict(_RAW)).passed

    def test_an_unknown_provider_fails_validate_and_names_the_field(self) -> None:
        result = validate_raw_config(_with_work_items({"provider": "jira"}))

        assert not result.passed
        paths = [e.field_path for e in result.errors]
        assert "symphonies[0].work_items.provider" in paths, paths

    def test_the_validate_command_shows_each_symphonys_provider(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import argparse

        from coordinare.__main__ import _cmd_config_validate_text

        monkeypatch.delenv("COORDINARE_GITHUB_TOKEN", raising=False)
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(_RAW))
        with pytest.raises(SystemExit):
            _cmd_config_validate_text(argparse.Namespace(config=path, strict=False))

        assert "Work items: demo uses github_projects" in capsys.readouterr().out


# --- one factory ----------------------------------------------------------------------------


class TestTheFactory:
    def test_no_block_builds_the_github_projects_adapter_over_the_given_service(self) -> None:
        github = MagicMock()
        provider = build_board_provider(None, github=github)

        assert isinstance(provider, GitHubProjectsBoardProvider)
        assert provider.github is github

    def test_the_named_provider_is_built(self) -> None:
        github = MagicMock()
        provider = build_board_provider(WorkItemsConfig(provider="github_projects"), github=github)
        assert isinstance(provider, GitHubProjectsBoardProvider)

    def test_the_registry_is_what_config_accepts(self) -> None:
        assert set(WORK_ITEM_PROVIDERS) == {"github_projects"}

    def test_nothing_outside_the_seam_constructs_a_provider(self) -> None:
        """The daemon and the CLI build a provider only through the factory."""
        allowed = {
            Path("src/coordinare/services/work_item_providers.py"),
            # board_of() derives the GitHub board for a state that names none; it is
            # the seam's own fallback, not a construction site.
            Path("src/coordinare/services/board_provider.py"),
        }
        offenders: list[str] = []
        for path in Path("src/coordinare").rglob("*.py"):
            if path in allowed:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("GitHubProjectsBoardProvider"):
                    offenders.append(f"{path}:{node.lineno}")
        assert not offenders, f"providers built outside the factory: {offenders}"


# --- the daemon's per-symphony swap ---------------------------------------------------------


def _daemon() -> Any:
    from coordinare.daemon import CoordinareDaemon

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})

    async def _no_sleep(_: float) -> None:
        return None

    return CoordinareDaemon(graph, poll_interval_seconds=1, max_cycles=1, sleep_func=_no_sleep)


class TestTheSymphonySwap:
    def test_a_symphony_reads_its_own_board_and_the_previous_one_comes_back(self) -> None:
        daemon = _daemon()
        global_github, sym_github = MagicMock(name="global"), MagicMock(name="sym")
        startup_board = build_board_provider(None, github=global_github)
        daemon._state["github_service"] = global_github
        daemon._state["board_provider"] = startup_board
        daemon._state["symphony_github_services"] = {"demo": sym_github}
        daemon._state["symphony_configs"] = {"demo": SymphonyConfig(name="demo", github_project_number=100)}

        swapped = daemon._swap_symphony_state_in("demo", None, {}, None)
        during = daemon._state["board_provider"]
        daemon._restore_symphony_swaps("demo", *swapped)

        assert isinstance(during, GitHubProjectsBoardProvider)
        assert during.github is sym_github
        assert daemon._state["board_provider"] is startup_board


# --- the dashboard and the MCP layer ---------------------------------------------------------


def _client(tmp_path: Path, raw: dict[str, Any]) -> tuple[TestClient, Any, Path]:
    from coordinare.dashboard import DashboardStore, create_dashboard_app
    from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_health, _make_mock_metrics

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
    daemon = _make_mock_daemon()
    daemon.state["coordinare_config"] = cfg
    daemon.state["config_version"] = 1
    daemon.state["symphony_configs"] = {s.name: s for s in cfg.symphonies}
    daemon.state["symphony_states"] = {}
    app = create_dashboard_app(DashboardStore(), daemon, _make_mock_metrics(), _make_mock_health(), config_path=path)
    return TestClient(app, base_url="http://127.0.0.1:8090"), daemon, path


@pytest.fixture
def _no_env_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COORDINARE_GITHUB_TOKEN", "COORDINARE_DASHBOARD_AUTH_TOKEN", "COORDINARE_OIDC_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("_no_env_secrets")
class TestTheDashboardAndMcp:
    def test_the_list_and_the_detail_show_the_provider(self, tmp_path: Path) -> None:
        client, _, _ = _client(tmp_path, _RAW)

        listed = client.get("/api/symphonies").json()["symphonies"][0]
        detail = client.get("/api/symphonies/demo").json()

        assert listed["work_items"] == {"provider": "github_projects"}
        assert detail["work_items"] == {"provider": "github_projects"}

    def test_an_update_keeps_the_block_it_was_not_sent(self, tmp_path: Path) -> None:
        raw = _with_work_items({"provider": "github_projects"})
        client, daemon, path = _client(tmp_path, raw)
        etag = client.get("/api/symphonies/demo").headers["ETag"]

        resp = client.put("/api/symphonies/demo", json={"enabled": True}, headers={"If-Match": etag})

        assert resp.status_code == 200, resp.text
        assert daemon.state["symphony_configs"]["demo"].work_items == WorkItemsConfig(provider="github_projects")
        on_disk = yaml.safe_load(path.read_text())["symphonies"][0]
        assert on_disk["work_items"] == {"provider": "github_projects"}

    def test_an_update_naming_an_unknown_provider_is_refused(self, tmp_path: Path) -> None:
        client, daemon, path = _client(tmp_path, _RAW)
        before = path.read_text()
        etag = client.get("/api/symphonies/demo").headers["ETag"]

        resp = client.put(
            "/api/symphonies/demo", json={"work_items": {"provider": "jira"}}, headers={"If-Match": etag},
        )

        assert resp.status_code == 400
        assert path.read_text() == before
        assert daemon.state["symphony_configs"]["demo"].work_items is None

    def test_validate_names_the_field_for_an_unknown_provider(self, tmp_path: Path) -> None:
        client, _, _ = _client(tmp_path, _RAW)

        resp = client.post("/api/symphonies/demo/validate", json={"work_items": {"provider": "jira"}})

        assert resp.status_code == 400
        body = resp.json()
        assert body["valid"] is False
        assert any(list(e.get("loc", [])) == ["work_items", "provider"] for e in body["errors"]), body

    def test_validate_reports_the_provider_it_would_use(self, tmp_path: Path) -> None:
        client, _, _ = _client(tmp_path, _RAW)

        body = client.post("/api/symphonies/demo/validate").json()

        assert body["valid"] is True
        assert body["work_items"] == {"provider": "github_projects"}

    def test_the_mcp_upsert_tool_describes_the_block(self) -> None:
        """What an agent reads before writing a symphony names the block."""
        from coordinare.mcp.tools import symphony_tools

        (tool,) = [t for t in symphony_tools.TOOLS if t.name == "upsert_symphony"]
        assert "work_items.provider" in tool.description


def test_the_symphony_detail_page_shows_the_provider_after_the_refreshed_rows() -> None:
    """The live refresh rewrites rows 1 to 5 by index, so the new row goes last."""
    source = Path("src/coordinare/dashboard/dashboard_static/app.js").read_text()
    last_error = source.index("['Last error', st.last_error")
    work_items = source.index("['Work items', workItemsLabel]")
    assert last_error < work_items
    assert "workItemsProvider === 'github_projects' ? 'GitHub Projects'" in source
