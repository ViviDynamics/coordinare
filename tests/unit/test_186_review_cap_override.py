"""#186 E: a symphony can set max_cards_in_review to no cap, and clear it.

The browser editor sends ``null`` to mean "no cap" and omits the key to mean
"use the global default". The server must take both through the same version-guarded
PUT, persist them, and resolve the effective config the way the daemon reads it.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml
from fastapi.testclient import TestClient

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.services.pipeline_budget import configured_review_limit


def _client(config_path: Path) -> TestClient:
    raw = yaml.safe_load(config_path.read_text())
    raw["github_token"] = "ghp_fixturetoken"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
    daemon = MagicMock()
    daemon.state = {
        "coordinare_config": cfg,
        "config_version": 7,
        "symphony_configs": {s.name: s for s in cfg.symphonies},
        "symphony_states": {s.name: {} for s in cfg.symphonies},
    }
    daemon.running = True
    daemon._cycle_active = False
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path)
    client = TestClient(app, base_url="http://127.0.0.1:8090")
    client.daemon = daemon
    return client


def _put(client: TestClient, overrides: dict | None) -> object:
    version = client.get("/api/symphonies/demo").headers["ETag"]
    return client.put(
        "/api/symphonies/demo",
        json={"overrides": overrides, "personas": None, "enabled": True, "env_spec_files": ["README.md"]},
        headers={"If-Match": version},
    )


def _persisted_overrides(config_path: Path) -> dict | None:
    raw = yaml.safe_load(config_path.read_text())
    (demo,) = [s for s in raw["symphonies"] if s["name"] == "demo"]
    return demo.get("overrides")


def _effective_review_limit(client: TestClient) -> int | None:
    symphony = client.daemon.state["symphony_configs"]["demo"]
    global_config = client.daemon.state["coordinare_config"].global_config
    return configured_review_limit(symphony.effective_config(global_config))


def test_a_symphony_can_set_no_cap_explicitly(temp_config_path: Path) -> None:
    client = _client(temp_config_path)
    response = _put(client, {"max_cards_in_review": None})
    assert response.status_code == 200, response.text
    assert response.json()["overrides"] == {"max_cards_in_review": None}
    assert _persisted_overrides(temp_config_path) == {"max_cards_in_review": None}
    assert _effective_review_limit(client) is None


def test_clearing_the_override_inherits_the_global_cap(temp_config_path: Path) -> None:
    client = _client(temp_config_path)
    assert _put(client, {"max_cards_in_review": None}).status_code == 200
    response = _put(client, None)
    assert response.status_code == 200, response.text
    assert not _persisted_overrides(temp_config_path)
    global_config = client.daemon.state["coordinare_config"].global_config
    assert _effective_review_limit(client) == configured_review_limit(global_config)


def test_a_number_is_still_saved_and_a_non_number_is_refused(temp_config_path: Path) -> None:
    client = _client(temp_config_path)
    # The fixture runs max_concurrent_cards 3, so 5 is above the floor and is taken as written.
    assert _put(client, {"max_cards_in_review": 5}).status_code == 200
    assert _effective_review_limit(client) == 5
    assert _put(client, {"max_cards_in_review": "lots"}).status_code == 400
    assert _persisted_overrides(temp_config_path) == {"max_cards_in_review": 5}
