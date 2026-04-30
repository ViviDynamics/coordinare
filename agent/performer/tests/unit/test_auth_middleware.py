"""Unit tests for BearerTokenAuthMiddleware (spec 056, T009)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from performer.server.auth import BearerTokenAuthMiddleware


def _build_app(token: str | None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BearerTokenAuthMiddleware, expected_token=token)

    @app.get("/status")
    async def status() -> dict[str, str]:
        return {"availability": "idle"}

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_disabled_auth_allows_unauthenticated_requests() -> None:
    client = TestClient(_build_app(None))
    r = client.get("/status")
    assert r.status_code == 200


def test_enabled_auth_rejects_missing_header() -> None:
    client = TestClient(_build_app("s3cret"))
    r = client.get("/status")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"
    assert r.json()["error"] == "unauthorized"


def test_enabled_auth_rejects_wrong_scheme() -> None:
    client = TestClient(_build_app("s3cret"))
    r = client.get("/status", headers={"Authorization": "Basic s3cret"})
    assert r.status_code == 401


def test_enabled_auth_rejects_wrong_token() -> None:
    client = TestClient(_build_app("s3cret"))
    r = client.get("/status", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_enabled_auth_accepts_correct_token() -> None:
    client = TestClient(_build_app("s3cret"))
    r = client.get("/status", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_healthz_bypasses_auth_when_enabled() -> None:
    client = TestClient(_build_app("s3cret"))
    r = client.get("/healthz")
    assert r.status_code == 200


@pytest.mark.parametrize("token,expected", [(None, False), ("x", True)])
def test_auth_enabled_property(token: str | None, expected: bool) -> None:
    mw = BearerTokenAuthMiddleware(app=lambda: None, expected_token=token)
    assert mw.auth_enabled is expected
