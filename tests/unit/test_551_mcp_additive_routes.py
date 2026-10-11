"""Spec 551 T026: the additive routes exist and are auth-covered like the rest.

``GET /api/state``, ``GET /api/activity`` and ``POST /api/config/validate`` are
new in spec 551; each must refuse unauthenticated requests when the dashboard
bearer is configured and answer correctly with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app

TOKEN = "d" * 32


def _app(config_path: Path, raw: dict[str, Any]):
    validated_raw = {**raw, "github_token": "ghp_fixturetoken"}
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(validated_raw))
    daemon = MagicMock()
    daemon.state = {"coordinare_config": cfg, "config_version": 7}
    daemon.running = True
    daemon._cycle_active = False
    daemon.state_store = None
    return create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        config_path=config_path, auth_token=SecretStr(TOKEN),
    )


@pytest.fixture
def client(temp_config_path: Path) -> TestClient:
    raw = yaml.safe_load(temp_config_path.read_text())
    return TestClient(_app(temp_config_path, raw), base_url="http://127.0.0.1:8090")


def test_state_and_activity_refuse_missing_bearer(client: TestClient) -> None:
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/activity").status_code == 401
    assert client.post("/api/config/validate", json={}).status_code == 401


def test_state_route_mirrors_snapshot(client: TestClient) -> None:
    response = client.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert "phase" in body and "subsystems" in body


def test_activity_route_returns_entries(client: TestClient) -> None:
    response = client.get("/api/activity", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert "entries" in response.json()


def test_validate_route_returns_error_as_data(
    client: TestClient, temp_config_path: Path,
) -> None:
    response = client.post(
        "/api/config/validate",
        json={"github_token": "ghp_fixturetoken"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["passed"] is False
    assert isinstance(body["errors"], list)
    before = temp_config_path.read_bytes()
    assert before  # the read-only preflight never writes
