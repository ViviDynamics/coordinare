"""Regression test for create_app_from_env constructor wiring (spec 056)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from performer.server import create_app_from_env


def test_create_app_from_env_no_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERFORMER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PERFORMER_CREDS_FILE", raising=False)
    app = create_app_from_env()
    client = TestClient(app)
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["auth_enabled"] is False


def test_create_app_from_env_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERFORMER_AUTH_TOKEN", "testtoken")
    monkeypatch.delenv("PERFORMER_CREDS_FILE", raising=False)
    app = create_app_from_env()
    client = TestClient(app)
    response = client.get("/status", headers={"Authorization": "Bearer testtoken"})
    assert response.status_code == 200
    assert response.json()["auth_enabled"] is True
