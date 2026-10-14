"""#151: the signals are recorded where their events already happen, and nothing else is called."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coordinare.dashboard.webhook import register_webhook_route
from coordinare.graph.nodes import monitor_performer as monitor_node
from coordinare.services import http_performer_service as hps_mod
from coordinare.services import performer_lifecycle as lifecycle
from coordinare.services.github import GitHubService, TransientGitHubError
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.system_health import (
    DEGRADED,
    HEALTHY,
    SYSTEM_HEALTH,
    SystemHealthInputs,
    build_system_health,
)
from tests.unit.services.test_http_performer_service import _card, _ephemeral_config, _workspace

_SECRET = "ghp_thisIsASecretTokenValue0123456789"
_NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_recorder() -> None:
    SYSTEM_HEALTH.reset()


def _inputs(**overrides: Any) -> SystemHealthInputs:
    base: dict[str, Any] = {
        "now": datetime.now(UTC),
        "health": {},
        "poll_interval_seconds": 30,
        "webhooks_enabled": True,
        "performer_modes": ("ephemeral",),
        "active_performers": 0,
        "disk_free_fraction": 0.5,
        "env_symphonies": 0,
        "env_failed": (),
        "env_ready": 0,
    }
    base.update(overrides)
    return SystemHealthInputs(**base)


def _row(key: str) -> dict[str, Any]:
    result = build_system_health(SYSTEM_HEALTH, _inputs())
    return next(row for row in result["rows"] if row["key"] == key)


class _Transport:
    response_headers: ClassVar[dict[str, str]] = {
        "X-RateLimit-Remaining": "4321",
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Reset": str(int(_NOW.timestamp())),
    }


class _Client:
    transport = _Transport()

    async def execute_async(self, document: Any, variable_values: Any = None) -> dict[str, Any]:
        return {"viewer": {"login": "bot"}}


def _service() -> GitHubService:
    return GitHubService(org="acme", project_number=1, token="t")


# --- GitHub API: the GraphQL response headers and the call outcome ---------------------


@pytest.mark.asyncio
async def test_a_graphql_response_records_the_quota_its_headers_carry(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()

    async def _through_the_request(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return await svc._execute_request(_Client(), query, variables)

    monkeypatch.setattr(svc, "_retried_execute", _through_the_request)
    await svc._guarded_execute("query { viewer { login } }", {})
    row = _row("github_api")
    assert row["status"] == HEALTHY
    assert row["summary"].startswith("4,321 of 5,000 requests left")


@pytest.mark.asyncio
async def test_a_transient_failure_is_recorded_by_class_name_and_the_message_stays_out(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    monkeypatch.setattr(svc, "_retried_execute", AsyncMock(side_effect=TransientGitHubError(f"503 Bearer {_SECRET}")))
    with pytest.raises(TransientGitHubError):
        await svc._guarded_execute("query { viewer { login } }", {})
    row = _row("github_api")
    assert row["status"] == DEGRADED
    assert "TransientGitHubError" in row["reason"]
    assert _SECRET not in json.dumps(row)
    assert "Bearer" not in json.dumps(row)


@pytest.mark.asyncio
async def test_a_successful_call_is_recorded_as_the_last_success(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    monkeypatch.setattr(svc, "_retried_execute", AsyncMock(return_value={"ok": True}))
    await svc._guarded_execute("query { viewer { login } }", {})
    row = _row("github_api")
    assert row["status"] == HEALTHY
    assert row["last_success_at"] is not None


# --- Board check: the board read itself ------------------------------------------------


@pytest.mark.asyncio
async def test_a_board_read_that_succeeds_is_recorded_as_a_successful_check(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    monkeypatch.setattr(svc, "_read_board", AsyncMock(return_value={"snapshot": {}}))
    await svc.poll_board()
    row = _row("board_check")
    assert row["status"] == HEALTHY
    assert row["last_success_at"] is not None


@pytest.mark.asyncio
async def test_a_board_read_that_fails_is_recorded_and_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    monkeypatch.setattr(svc, "_read_board", AsyncMock(side_effect=TransientGitHubError(f"boom {_SECRET}")))
    with pytest.raises(TransientGitHubError):
        await svc.poll_board()
    row = _row("board_check")
    assert row["status"] == DEGRADED
    assert row["last_success_at"] is None
    assert _SECRET not in json.dumps(row)


# --- Webhooks: every delivery, accepted or rejected ------------------------------------


def _signed(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_deliveries_are_recorded_as_accepted_or_rejected() -> None:
    app = FastAPI()
    register_webhook_route(app, "/webhook/github", "s3cret", asyncio.Event())
    client = TestClient(app)
    body = b'{"action":"opened"}'

    good = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": _signed(body, "s3cret")})
    assert good.status_code == 200
    assert _row("github_webhooks")["status"] == HEALTHY

    bad = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": "sha256=nope"})
    assert bad.status_code == 401
    assert _row("github_webhooks")["status"] == DEGRADED


# --- Model backends: the outcome of a performer turn ------------------------------------


@pytest.mark.asyncio
async def test_a_turn_that_ends_in_a_system_error_is_recorded_as_a_model_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _body(state: dict[str, Any]) -> dict[str, Any]:
        state["phase"] = "system_error"
        return state

    monkeypatch.setattr(monitor_node, "_monitor_performer_body", _body)
    state: Any = {"phase": "monitoring_performer"}
    await monitor_node.monitor_performer(state)
    row = _row("model_backends")
    assert row["status"] == DEGRADED
    assert row["summary"].startswith("1 of last")


@pytest.mark.asyncio
async def test_a_turn_that_leaves_monitoring_without_an_error_is_a_model_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _body(state: dict[str, Any]) -> dict[str, Any]:
        state["phase"] = "monitoring_pr"
        return state

    monkeypatch.setattr(monitor_node, "_monitor_performer_body", _body)
    state: Any = {"phase": "monitoring_performer"}
    await monitor_node.monitor_performer(state)
    row = _row("model_backends")
    assert row["status"] == HEALTHY
    assert row["last_success_at"] is not None


@pytest.mark.asyncio
async def test_a_poll_that_is_still_running_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _body(state: dict[str, Any]) -> dict[str, Any]:
        state["phase"] = "monitoring_performer"
        return state

    monkeypatch.setattr(monitor_node, "_monitor_performer_body", _body)
    state: Any = {"phase": "monitoring_performer"}
    await monitor_node.monitor_performer(state)
    assert _row("model_backends")["status"] == "unknown"


# --- Performer runtime: the start the dispatch path makes -------------------------------


@pytest.mark.asyncio
async def test_a_failed_container_start_is_recorded_by_its_exception_class_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail_start(config: Any, **kwargs: Any) -> None:
        raise lifecycle.LifecycleError(f"no image {_SECRET}")

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", _fail_start)
    await HTTPPerformerService(_ephemeral_config()).dispatch_card(_card(), _workspace())
    row = _row("performer_runtime")
    assert row["status"] == DEGRADED
    assert "LifecycleError" in row["reason"]
    assert row["summary"].startswith("Docker")
    assert _SECRET not in json.dumps(row)
