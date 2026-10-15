"""#225 narrow check: every write path refuses a config the daemon cannot start with.

Covers the global save (the HTTP route, the config assistant's Apply body, and MCP put_global),
the app-mode key file at save time, the symphony CRUD writes, and the unexpanded ${VAR} secrets
that would otherwise become a publicly known token at boot and at save.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import CoordinareConfiguration, ProjectConfiguration, SymphonyConfig
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.mcp.client import DashboardClient
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.tools.config_tools import UpdateConfigInput, update_config_handler
from coordinare.services.config_write_service import (
    SaveRequest,
    _validate_candidate,
    compute_content_hash,
    save_section,
)
from coordinare.start_checks import validate_start_time
from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_health, _make_mock_metrics

_TOKEN = "a-dashboard-token-with-more-than-thirty-two-characters"
_OIDC = {
    "discovery_url": "https://idp.example/.well-known/openid-configuration",
    "client_id": "cid",
    "client_secret": "csec",
    "redirect_url": "https://coordinare.example/oidc/callback",
}
_BASE: dict[str, Any] = {
    "github_org": "acme",
    "github_token": "ghp_fixturetoken",
    "human_reviewers": ["alice"],
    "symphonies": [{"name": "demo", "github_project_number": 100}],
}


@pytest.fixture(autouse=True)
def _daemon_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decide from the file alone: nothing from the developer's environment supplies a secret."""
    for name in ("COORDINARE_GITHUB_TOKEN", "COORDINARE_DASHBOARD_AUTH_TOKEN", "COORDINARE_OIDC_SECRET"):
        monkeypatch.delenv(name, raising=False)


def _write(path: Path, raw: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def _client(config_path: Path, raw: dict[str, Any]) -> TestClient:
    validated = {**raw, "github_token": "ghp_fixturetoken"}
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(validated))
    daemon = _make_mock_daemon()
    daemon.state["coordinare_config"] = cfg
    daemon.state["config_version"] = 7
    app = create_dashboard_app(
        DashboardStore(), daemon, _make_mock_metrics(), _make_mock_health(), config_path=config_path,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090")


# A dashboard token that is set on disk but unbootable: a remote bind needs a token, and a token
# must be 32 characters. Each variant is refused by the daemon at boot.
_UNBOOTABLE_TOKENS: dict[str, dict[str, Any]] = {
    "remote bind, no token": {"dashboard_host": "0.0.0.0"},
    "remote bind, 5-character token": {"dashboard_host": "0.0.0.0", "dashboard_auth_token": "short"},
    "remote bind, empty token": {"dashboard_host": "0.0.0.0", "dashboard_auth_token": ""},
    "loopback, 5-character token": {"dashboard_host": "127.0.0.1", "dashboard_auth_token": "short"},
}


# --- BLOCK 1: the global save, on each caller path ------------------------------------


@pytest.mark.parametrize("variant", sorted(_UNBOOTABLE_TOKENS))
def test_http_global_put_refuses_a_candidate_the_daemon_cannot_start_with(
    tmp_path: Path, variant: str,
) -> None:
    config_path = _write(tmp_path / "config.yaml", {**_BASE, **_UNBOOTABLE_TOKENS[variant]})
    before = config_path.read_text()
    client = _client(config_path, yaml.safe_load(before))

    resp = client.put(
        "/api/config/global",
        json={"poll_interval_seconds": 45, "expected_hash": compute_content_hash(config_path)},
    )

    assert resp.status_code == 422, resp.text
    assert "dashboard_auth_token" in resp.json()["error"]
    assert config_path.read_text() == before


@pytest.mark.parametrize("variant", sorted(_UNBOOTABLE_TOKENS))
def test_assistant_apply_refuses_a_candidate_the_daemon_cannot_start_with(
    tmp_path: Path, variant: str,
) -> None:
    """The config assistant's Apply sends the proposed values with expected_hash to the same route."""
    config_path = _write(tmp_path / "config.yaml", {**_BASE, **_UNBOOTABLE_TOKENS[variant]})
    before = config_path.read_text()
    client = _client(config_path, yaml.safe_load(before))
    proposal_values = {"poll_interval_seconds": 45}

    resp = client.put(
        "/api/config/global",
        json={**proposal_values, "expected_hash": compute_content_hash(config_path)},
    )

    assert resp.status_code == 422, resp.text
    assert "Validation failed" in resp.json()["error"]
    assert config_path.read_text() == before


@pytest.mark.parametrize("variant", sorted(_UNBOOTABLE_TOKENS))
@pytest.mark.asyncio
async def test_mcp_put_global_refuses_a_candidate_the_daemon_cannot_start_with(
    tmp_path: Path, variant: str,
) -> None:
    config_path = _write(tmp_path / "config.yaml", {**_BASE, **_UNBOOTABLE_TOKENS[variant]})
    before = config_path.read_text()
    raw = yaml.safe_load(before)
    validated = {**raw, "github_token": "ghp_fixturetoken"}
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(validated))
    daemon = _make_mock_daemon()
    daemon.state["coordinare_config"] = cfg
    daemon.state["config_version"] = 7
    app = create_dashboard_app(
        DashboardStore(), daemon, _make_mock_metrics(), _make_mock_health(), config_path=config_path,
    )
    mcp_config = McpServerConfig(agent_token=SecretStr("a" * 32), dashboard_token=SecretStr(_TOKEN))
    client = DashboardClient(mcp_config, transport=httpx.ASGITransport(app=app))

    result = await update_config_handler(
        UpdateConfigInput(
            action="put_global", base_hash=compute_content_hash(config_path),
            payload={"poll_interval_seconds": 45},
        ),
        client,
    )

    assert not result.ok
    assert result.error is not None and result.error.kind == "validation"
    assert "dashboard_auth_token" in result.error.detail
    assert config_path.read_text() == before


def test_a_bootable_global_save_still_goes_through(tmp_path: Path) -> None:
    config_path = _write(tmp_path / "config.yaml", {**_BASE, "dashboard_host": "127.0.0.1"})
    client = _client(config_path, yaml.safe_load(config_path.read_text()))

    resp = client.put(
        "/api/config/global",
        json={"poll_interval_seconds": 45, "expected_hash": compute_content_hash(config_path)},
    )

    assert resp.status_code == 200, resp.text


# --- BLOCK 2: the app-mode key file is a start-time check, at boot and at save ---------


def _app_raw(key_path: Path) -> dict[str, Any]:
    raw = {k: v for k, v in _BASE.items() if k != "github_token"}
    return {
        **raw, "github_auth": "app", "github_app_id": 1, "github_installation_id": 2,
        "github_private_key_path": str(key_path),
    }


def test_start_time_refuses_a_missing_app_key_file(tmp_path: Path) -> None:
    pc = ProjectConfiguration.from_yaml(_write(tmp_path / "config.yaml", _app_raw(tmp_path / "missing.pem")))
    with pytest.raises(ValueError, match="does not exist"):
        validate_start_time(pc)


def test_start_time_refuses_a_key_path_that_is_a_directory(tmp_path: Path) -> None:
    pc = ProjectConfiguration.from_yaml(_write(tmp_path / "config.yaml", _app_raw(tmp_path)))
    with pytest.raises(ValueError, match="not a regular file"):
        validate_start_time(pc)


def test_a_save_that_points_app_mode_at_a_missing_key_file_is_refused(tmp_path: Path) -> None:
    key = tmp_path / "key.pem"
    key.write_text("placeholder")
    config_path = _write(tmp_path / "config.yaml", _app_raw(key))

    result = save_section(
        SaveRequest(
            section="global",
            changes={"github_private_key_path": str(tmp_path / "missing.pem")},
            base_hash=compute_content_hash(config_path),
        ),
        config_path,
    )

    assert not result.ok
    assert any("does not exist" in e.message for e in result.errors)
    assert str(key) in config_path.read_text()


# --- the symphony CRUD writes go through the same gate ----------------------------------


def _symphony_client(tmp_path: Path) -> tuple[TestClient, Any, Path]:
    raw = {**_BASE, "dashboard_host": "0.0.0.0"}
    config_path = _write(tmp_path / "config.yaml", raw)
    daemon = _make_mock_daemon()
    daemon.state["symphony_configs"] = {
        "alpha": SymphonyConfig(name="alpha", github_project_number=10),
        "beta": SymphonyConfig(name="beta", github_project_number=20),
    }
    app = create_dashboard_app(
        DashboardStore(), daemon, _make_mock_metrics(), _make_mock_health(), config_path=config_path,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090"), daemon, config_path


def test_create_symphony_is_refused_and_changes_nothing_when_the_config_would_not_boot(tmp_path: Path) -> None:
    client, daemon, config_path = _symphony_client(tmp_path)
    before = config_path.read_text()

    resp = client.post(
        "/api/symphonies",
        json={"name": "gamma", "github_project_number": 30},
        headers={"If-Match": compute_content_hash(config_path)},
    )

    assert resp.status_code == 422, resp.text
    assert "dashboard_auth_token" in resp.json()["error"]
    assert "gamma" not in daemon.state["symphony_configs"]
    assert config_path.read_text() == before


def test_delete_symphony_is_refused_and_changes_nothing_when_the_config_would_not_boot(tmp_path: Path) -> None:
    client, daemon, config_path = _symphony_client(tmp_path)
    before = config_path.read_text()

    resp = client.delete(
        "/api/symphonies/alpha", headers={"If-Match": compute_content_hash(config_path)},
    )

    assert resp.status_code == 422, resp.text
    assert "alpha" in daemon.state["symphony_configs"]
    assert config_path.read_text() == before


# --- unexpanded ${VAR} secrets: refused at boot and at save ----------------------------


def test_boot_refuses_an_unset_dashboard_token_reference_even_when_it_is_long_enough(
    tmp_path: Path,
) -> None:
    """The literal "${COORDINARE_DASHBOARD_AUTH_TOKEN}" is 34 characters: it passes the length check."""
    raw = {**_BASE, "dashboard_host": "127.0.0.1", "dashboard_auth_token": "${COORDINARE_DASHBOARD_AUTH_TOKEN}"}
    pc = ProjectConfiguration.from_yaml(_write(tmp_path / "config.yaml", raw))
    with pytest.raises(ValueError, match="environment variable COORDINARE_DASHBOARD_AUTH_TOKEN"):
        validate_start_time(pc)


def test_boot_accepts_a_dashboard_token_reference_once_it_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORDINARE_DASHBOARD_AUTH_TOKEN", _TOKEN)
    raw = {**_BASE, "dashboard_host": "0.0.0.0", "dashboard_auth_token": "${COORDINARE_DASHBOARD_AUTH_TOKEN}"}
    pc = ProjectConfiguration.from_yaml(_write(tmp_path / "config.yaml", raw))
    validate_start_time(pc)


def test_boot_names_the_unset_github_token_variable(tmp_path: Path) -> None:
    raw = {**_BASE, "github_token": "${GITHUB_TOKEN}"}
    with pytest.raises(Exception, match="environment variable GITHUB_TOKEN"):
        ProjectConfiguration.from_yaml(_write(tmp_path / "config.yaml", raw))


def test_boot_names_the_unset_oidc_client_secret_variable(tmp_path: Path) -> None:
    oidc = {**_OIDC, "client_secret": "${COORDINARE_OIDC_CLIENT_SECRET}"}
    raw = {**_BASE, "dashboard_host": "127.0.0.1", "dashboard_oidc": oidc,
           "trusted_dashboard_hosts": ["coordinare.example"]}
    pc = ProjectConfiguration.from_yaml(_write(tmp_path / "config.yaml", raw))
    with pytest.raises(ValueError, match="environment variable COORDINARE_OIDC_CLIENT_SECRET"):
        validate_start_time(pc)


def test_boot_names_the_unset_webhook_secret_variable(tmp_path: Path) -> None:
    raw = {**_BASE, "webhooks": {"enabled": True, "secret": "${COORDINARE_WEBHOOK_SECRET}"}}
    pc = ProjectConfiguration.from_yaml(_write(tmp_path / "config.yaml", raw))
    with pytest.raises(ValueError, match="environment variable COORDINARE_WEBHOOK_SECRET"):
        validate_start_time(pc)


def test_save_refuses_an_unset_dashboard_token_reference_and_names_the_variable() -> None:
    raw = {**_BASE, "dashboard_host": "127.0.0.1", "dashboard_auth_token": "${COORDINARE_DASHBOARD_AUTH_TOKEN}"}
    errors = _validate_candidate(raw)
    assert errors
    assert "environment variable COORDINARE_DASHBOARD_AUTH_TOKEN" in errors[0].message
    assert errors[0].key == "dashboard_auth_token"


def test_the_global_put_refuses_an_unset_dashboard_token_reference(tmp_path: Path) -> None:
    raw = {**_BASE, "dashboard_host": "127.0.0.1", "dashboard_auth_token": "${COORDINARE_DASHBOARD_AUTH_TOKEN}"}
    config_path = _write(tmp_path / "config.yaml", raw)
    before = config_path.read_text()
    client = _client(config_path, {**raw, "dashboard_auth_token": _TOKEN})

    resp = client.put(
        "/api/config/global",
        json={"poll_interval_seconds": 45, "expected_hash": compute_content_hash(config_path)},
    )

    assert resp.status_code == 422, resp.text
    assert "environment variable COORDINARE_DASHBOARD_AUTH_TOKEN" in resp.json()["error"]
    assert config_path.read_text() == before


def test_a_set_reference_saves_and_boots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINARE_DASHBOARD_AUTH_TOKEN", _TOKEN)
    raw = {**_BASE, "dashboard_host": "0.0.0.0", "dashboard_auth_token": "${COORDINARE_DASHBOARD_AUTH_TOKEN}"}
    config_path = _write(tmp_path / "config.yaml", raw)

    result = save_section(
        SaveRequest(section="global", changes={"poll_interval_seconds": 45},
                    base_hash=compute_content_hash(config_path)),
        config_path,
    )

    assert result.ok, result.errors
    validate_start_time(ProjectConfiguration.from_yaml(config_path))
