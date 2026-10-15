"""#221: the symphony editor's server side, against a real config file.

The symphony PUT and DELETE already take a version (#158 / #241). These tests pin what a stale or
concurrent write does to the file and to the daemon's memory, and the two things #221 added: a
secret override is served masked, and a masked secret sent back keeps the value the server holds.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from coordinare.config import CoordinareConfiguration
from coordinare.config_descriptors import SECRET_MASK
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.services.config_write_service import compute_content_hash
from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_health, _make_mock_metrics

_BASE: dict[str, Any] = {
    "github_org": "acme",
    "github_token": "ghp_fixturetoken",
    "human_reviewers": ["alice"],
    "symphonies": [
        {"name": "demo", "github_project_number": 100, "overrides": {"max_concurrent_cards": 2}},
        {"name": "other", "github_project_number": 101},
    ],
}
_SECRET = "ghp_symphonysecret0000000000000000000"
_TOKEN = "a-dashboard-token-with-more-than-thirty-two-characters"


def _write(path: Path, raw: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def _client(config_path: Path, raw: dict[str, Any]) -> tuple[TestClient, Any]:
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw({**raw, "github_token": "ghp_fixturetoken"}))
    daemon = _make_mock_daemon()
    daemon.state["coordinare_config"] = cfg
    daemon.state["config_version"] = 1
    daemon.state["symphony_configs"] = {s.name: s for s in cfg.symphonies}
    daemon.state["symphony_states"] = {}
    app = create_dashboard_app(
        DashboardStore(), daemon, _make_mock_metrics(), _make_mock_health(), config_path=config_path,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090"), daemon


@pytest.fixture(autouse=True)
def _daemon_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COORDINARE_GITHUB_TOKEN", "COORDINARE_DASHBOARD_AUTH_TOKEN", "COORDINARE_OIDC_SECRET"):
        monkeypatch.delenv(name, raising=False)


def _etag(client: TestClient, name: str = "demo") -> str:
    resp = client.get(f"/api/symphonies/{name}")
    assert resp.status_code == 200, resp.text
    return resp.headers["ETag"]


def _symphony_on_disk(path: Path, name: str) -> dict[str, Any]:
    return next(s for s in yaml.safe_load(path.read_text())["symphonies"] if s["name"] == name)


# --- the GET serves a secret override masked ------------------------------------------------


def test_a_secret_override_is_served_masked_and_its_key_is_listed(tmp_path: Path) -> None:
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100,
                                    "overrides": {"github_token": _SECRET, "max_concurrent_cards": 2}}]}
    client, _ = _client(_write(tmp_path / "config.yaml", raw), raw)

    resp = client.get("/api/symphonies/demo")

    assert resp.status_code == 200
    body = resp.json()
    assert body["overrides"] == {"github_token": SECRET_MASK, "max_concurrent_cards": 2}
    # The global token is required, so the override is not offered a Clear (#225's required rule).
    assert body["secret_overrides"] == [{"key": "github_token", "clearable": False}]
    assert _SECRET not in resp.text


_NESTED = "whsec_NESTED_SECRET_123456789"


def test_no_secret_at_any_depth_appears_in_the_get_body(tmp_path: Path) -> None:
    """A secret inside a dict-valued override (webhooks.secret) is masked like a top-level one."""
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100, "overrides": {
        "webhooks": {"secret": _NESTED}, "github_token": _SECRET, "max_concurrent_cards": 2}}]}
    client, _ = _client(_write(tmp_path / "config.yaml", raw), raw)

    resp = client.get("/api/symphonies/demo")

    assert resp.status_code == 200
    assert resp.json()["overrides"]["webhooks"] == {"secret": SECRET_MASK}
    assert _NESTED not in resp.text
    assert _SECRET not in resp.text


def test_a_nested_masked_secret_keeps_its_stored_value_on_save(tmp_path: Path) -> None:
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100, "overrides": {
        "webhooks": {"secret": _NESTED}}}]}
    path = _write(tmp_path / "config.yaml", raw)
    client, _ = _client(path, raw)

    resp = client.put("/api/symphonies/demo", json={"overrides": {"webhooks": {"secret": SECRET_MASK}, "max_concurrent_cards": 4}},
                      headers={"If-Match": _etag(client)})

    assert resp.status_code == 200, resp.text
    assert _symphony_on_disk(path, "demo")["overrides"] == {"webhooks": {"secret": _NESTED}, "max_concurrent_cards": 4}
    assert _NESTED not in resp.text


def test_a_nested_mask_with_nothing_stored_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    before = path.read_text()
    client, _ = _client(path, _BASE)

    resp = client.put("/api/symphonies/demo", json={"overrides": {"webhooks": {"secret": SECRET_MASK}}},
                      headers={"If-Match": _etag(client)})

    assert resp.status_code == 422, resp.text
    assert path.read_text() == before


def test_an_optional_secret_override_is_clearable_and_clearing_it_writes_it_absent(tmp_path: Path) -> None:
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100,
                                    "overrides": {"dashboard_auth_token": _TOKEN, "max_concurrent_cards": 2}}]}
    path = _write(tmp_path / "config.yaml", raw)
    client, _ = _client(path, raw)

    body = client.get("/api/symphonies/demo").json()
    assert body["secret_overrides"] == [{"key": "dashboard_auth_token", "clearable": True}]

    resp = client.put("/api/symphonies/demo", json={"overrides": {"max_concurrent_cards": 2}},
                      headers={"If-Match": _etag(client)})

    assert resp.status_code == 200, resp.text
    assert _symphony_on_disk(path, "demo")["overrides"] == {"max_concurrent_cards": 2}


def test_an_update_response_serves_a_secret_override_masked_too(tmp_path: Path) -> None:
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100,
                                    "overrides": {"github_token": _SECRET}}]}
    path = _write(tmp_path / "config.yaml", raw)
    client, _ = _client(path, raw)

    resp = client.put("/api/symphonies/demo", json={"overrides": {"github_token": SECRET_MASK, "max_concurrent_cards": 3}},
                      headers={"If-Match": _etag(client)})

    assert resp.status_code == 200, resp.text
    assert resp.json()["overrides"]["github_token"] == SECRET_MASK
    assert _SECRET not in resp.text


# --- a masked secret sent back keeps the stored value ----------------------------------------


def test_a_masked_secret_keeps_the_stored_value_on_save(tmp_path: Path) -> None:
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100,
                                    "overrides": {"github_token": _SECRET}}]}
    path = _write(tmp_path / "config.yaml", raw)
    client, daemon = _client(path, raw)

    resp = client.put("/api/symphonies/demo", json={"overrides": {"github_token": SECRET_MASK, "max_concurrent_cards": 4}},
                      headers={"If-Match": _etag(client)})

    assert resp.status_code == 200, resp.text
    on_disk = _symphony_on_disk(path, "demo")["overrides"]
    assert on_disk == {"github_token": _SECRET, "max_concurrent_cards": 4}
    assert daemon.state["symphony_configs"]["demo"].overrides == on_disk


def test_a_typed_secret_replaces_the_stored_one(tmp_path: Path) -> None:
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100,
                                    "overrides": {"github_token": _SECRET}}]}
    path = _write(tmp_path / "config.yaml", raw)
    client, _ = _client(path, raw)
    typed = "ghp_typedanewvalue00000000000000000000"

    resp = client.put("/api/symphonies/demo", json={"overrides": {"github_token": typed}},
                      headers={"If-Match": _etag(client)})

    assert resp.status_code == 200, resp.text
    assert _symphony_on_disk(path, "demo")["overrides"] == {"github_token": typed}


def test_a_mask_with_nothing_stored_to_keep_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    before = path.read_text()
    client, daemon = _client(path, _BASE)

    resp = client.put("/api/symphonies/demo", json={"overrides": {"github_token": SECRET_MASK}},
                      headers={"If-Match": _etag(client)})

    assert resp.status_code == 422, resp.text
    assert "github_token" in resp.json()["error"]
    assert path.read_text() == before
    assert daemon.state["config_version"] == 1


# --- a stale save is refused with the conflict flag, and nothing is written -------------------


def test_a_stale_save_is_refused_as_a_conflict_and_writes_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    client, daemon = _client(path, _BASE)
    stale = _etag(client)
    path.write_text(path.read_text() + "\n# someone else changed this\n")
    after_other_edit = path.read_text()
    version_before = daemon.state["config_version"]

    resp = client.put("/api/symphonies/demo", json={"overrides": {"max_concurrent_cards": 9}},
                      headers={"If-Match": stale})

    assert resp.status_code == 409, resp.text
    assert resp.json()["conflict"] is True
    assert path.read_text() == after_other_edit
    assert daemon.state["symphony_configs"]["demo"].overrides == {"max_concurrent_cards": 2}
    assert daemon.state["config_version"] == version_before


def test_a_stale_delete_is_refused_as_a_conflict_and_removes_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    client, daemon = _client(path, _BASE)
    stale = _etag(client, "other")
    path.write_text(path.read_text() + "\n# someone else changed this\n")
    after_other_edit = path.read_text()

    resp = client.delete("/api/symphonies/other", headers={"If-Match": stale})

    assert resp.status_code == 409, resp.text
    assert resp.json()["conflict"] is True
    assert path.read_text() == after_other_edit
    assert "other" in daemon.state["symphony_configs"]


def test_a_save_with_no_version_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    before = path.read_text()
    client, _ = _client(path, _BASE)

    resp = client.put("/api/symphonies/demo", json={"overrides": {"max_concurrent_cards": 9}})

    assert resp.status_code == 428, resp.text
    assert path.read_text() == before


# --- a concurrent delete: the symphony is gone, so the save says so and writes nothing --------


def test_a_save_after_a_concurrent_delete_says_the_symphony_is_gone(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    client, daemon = _client(path, _BASE)
    stale = _etag(client)
    # The other session deletes it: the file and the daemon both lose the symphony.
    remaining = [s for s in _BASE["symphonies"] if s["name"] != "demo"]
    path.write_text(yaml.safe_dump({**_BASE, "symphonies": remaining}, sort_keys=False))
    del daemon.state["symphony_configs"]["demo"]
    after_delete = path.read_text()

    resp = client.put("/api/symphonies/demo", json={"overrides": {"max_concurrent_cards": 9}},
                      headers={"If-Match": stale})

    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["error"]
    assert path.read_text() == after_delete


# --- a save the daemon could not start with is refused with its reason ------------------------


def test_a_save_the_daemon_could_not_start_with_is_refused_with_the_reason(tmp_path: Path) -> None:
    raw = {**_BASE, "dashboard_host": "0.0.0.0", "dashboard_auth_token": _TOKEN}
    path = _write(tmp_path / "config.yaml", raw)
    # The token is removed on disk: a remote bind then cannot start, and the next save must say why.
    path.write_text(yaml.safe_dump({k: v for k, v in raw.items() if k != "dashboard_auth_token"}, sort_keys=False))
    before = path.read_text()
    client, _ = _client(path, raw)

    resp = client.put("/api/symphonies/demo", json={"overrides": {"max_concurrent_cards": 9}},
                      headers={"If-Match": _etag(client)})

    assert resp.status_code == 422, resp.text
    assert "Non-loopback dashboard_host requires dashboard_auth_token" in resp.json()["error"]
    assert path.read_text() == before


def test_the_etag_names_the_file_the_page_loaded(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    client, _ = _client(path, _BASE)

    assert _etag(client) == f'"{compute_content_hash(path)}"'
