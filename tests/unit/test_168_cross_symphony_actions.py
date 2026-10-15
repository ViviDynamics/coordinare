"""#168: a card action reaches the symphony that holds the card.

One daemon process holds every symphony's runtime. Skip and restart find the
owning session across all runtimes by card id, so they reach the holder. Cancel
acts only on the card the flat state names as active, so it is offered only for
that card, and the server says which one that is (cancel_available).
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

from pydantic import SecretStr

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.graph.state import SymphonyRuntimeState
from coordinare.localhost_guard import build_permitted
from tests.unit.test_dashboard import (
    _make_app,
    _make_mock_daemon,
    _make_mock_health,
    _make_mock_metrics,
)

HELD = "PVTI_HELD"  # held by the symphony "owner", not the flat active card
ACTIVE = "PVTI_ACTIVE"
TOKEN = "a-secret-operator-token-of-at-least-32-characters"
BASE = "http://127.0.0.1:8090"


def _session(card_id: str, number: int) -> dict:
    return {
        "phase": "monitoring_performer",
        "performer_stage": "implementing",
        "board_paused": False,
        "current_card": {"id": card_id, "issue_number": number, "title": f"Card {number}"},
    }


def _two_symphony_daemon():
    """The flat state is the active card's; HELD sits in the owner's runtime."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    held = _session(HELD, 68)
    active = _session(ACTIVE, 67)
    daemon.state.update(
        performer_stage="implementing",
        current_card=active["current_card"],
        active_card_id=ACTIVE,
        active_sessions={ACTIVE: active, HELD: held},
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
        symphony_states={
            "owner": SymphonyRuntimeState(name="owner", active_sessions={HELD: held}),
            "peer": SymphonyRuntimeState(name="peer", active_sessions={ACTIVE: active}),
        },
        symphony_configs={},
    )
    return daemon, held


def _snapshot_sessions(daemon) -> list[dict]:
    snapshot = DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    return list(snapshot["active_sessions"])


# --- skip and restart reach the holding symphony ------------------------------ #


def test_skip_on_a_card_another_symphony_holds_queues_it_there() -> None:
    daemon, held = _two_symphony_daemon()
    response = _make_app(daemon=daemon).post("/api/skip-role", params={"card_id": HELD})
    assert response.status_code == 200 and response.json() == {
        "status": "override_queued",
        "action": "skip",
    }
    assert held["pending_override"]["action"] == "skip"
    assert daemon.state["active_sessions"][ACTIVE].get("pending_override") is None


def test_restart_on_a_card_another_symphony_holds_queues_it_there() -> None:
    daemon, held = _two_symphony_daemon()
    response = _make_app(daemon=daemon).post("/api/restart-from/assessor", params={"card_id": HELD})
    assert response.status_code == 200 and response.json()["target_stage"] == "assessing"
    assert held["pending_override"] == {
        "action": "restart",
        "target_stage": "assessing",
        "control_id": held["pending_override"]["control_id"],
    }


def test_a_second_skip_on_the_held_card_is_refused_as_pending() -> None:
    daemon, _held = _two_symphony_daemon()
    client = _make_app(daemon=daemon)
    assert client.post("/api/skip-role", params={"card_id": HELD}).status_code == 200
    assert client.post("/api/skip-role", params={"card_id": HELD}).status_code == 409


# --- cancel acts only on the card it names, and says so ----------------------- #


def test_cancel_on_a_card_another_symphony_holds_is_refused_and_cancels_nothing(
    monkeypatch,
) -> None:
    daemon, held = _two_symphony_daemon()
    cancel = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr("coordinare.cancel.cancel_active_card", cancel)
    response = _make_app(daemon=daemon).post("/api/cancel", params={"card_id": HELD})
    assert response.status_code == 409 and response.json()["status"] == "not_active_card"
    cancel.assert_not_awaited()
    assert "pending_override" not in held


def test_cancel_on_the_active_card_still_cancels_it(monkeypatch) -> None:
    daemon, _held = _two_symphony_daemon()
    cancel = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr("coordinare.cancel.cancel_active_card", cancel)
    response = _make_app(daemon=daemon).post("/api/cancel", params={"card_id": ACTIVE})
    assert response.json() == {"status": "cancelled"}
    cancel.assert_awaited_once()


def test_snapshot_offers_cancel_only_for_the_card_cancel_would_act_on() -> None:
    daemon, _held = _two_symphony_daemon()
    flags = {row["card_id"]: row["cancel_available"] for row in _snapshot_sessions(daemon)}
    assert flags == {ACTIVE: True, HELD: False}


def test_snapshot_offers_no_cancel_when_nothing_is_active() -> None:
    daemon, _held = _two_symphony_daemon()
    daemon.state.update(active_card_id=None, current_card=None, active_sessions={})
    for row in _snapshot_sessions(daemon):
        assert row["cancel_available"] is False


# --- unknown cards are a clean error ------------------------------------------ #


def test_skip_and_restart_on_an_unknown_card_are_a_clean_400() -> None:
    daemon, _held = _two_symphony_daemon()
    client = _make_app(daemon=daemon)
    skip = client.post("/api/skip-role", params={"card_id": "PVTI_NOPE"})
    restart = client.post("/api/restart-from/assessor", params={"card_id": "PVTI_NOPE"})
    assert skip.status_code == 400 and skip.json() == {
        "error": "No eligible active card for this target",
    }
    assert restart.status_code == 400 and restart.json() == {
        "error": "No eligible active card for this target",
    }


def test_cancel_on_an_unknown_card_is_a_clean_refusal(monkeypatch) -> None:
    daemon, _held = _two_symphony_daemon()
    cancel = AsyncMock()
    monkeypatch.setattr("coordinare.cancel.cancel_active_card", cancel)
    response = _make_app(daemon=daemon).post("/api/cancel", params={"card_id": "PVTI_NOPE"})
    assert response.status_code == 409 and response.json()["status"] == "not_active_card"
    cancel.assert_not_awaited()


# --- authentication still applies to every card action ------------------------ #


def _authed_app(daemon):
    return create_dashboard_app(
        DashboardStore(),
        daemon,
        MagicMock(),
        MagicMock(),
        auth_token=SecretStr(TOKEN),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )


def test_card_actions_on_a_held_card_require_the_token_and_same_origin() -> None:
    from fastapi.testclient import TestClient

    daemon, held = _two_symphony_daemon()
    with TestClient(_authed_app(daemon), base_url=BASE) as client:
        for path in ("/api/cancel", "/api/skip-role", "/api/restart-from/assessor"):
            params = {"card_id": HELD}
            assert client.post(path, params=params).status_code == 401
            assert (
                client.post(
                    path,
                    params=params,
                    headers={"Authorization": "Bearer wrong"},
                ).status_code
                == 401
            )
            assert (
                client.post(
                    path,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Origin": "https://evil.example",
                    },
                ).status_code
                == 403
            )
        assert "pending_override" not in held

        basic = base64.b64encode(("operator:" + TOKEN).encode()).decode()
        ok = client.post(
            "/api/skip-role",
            params={"card_id": HELD},
            headers={"Authorization": f"Basic {basic}"},
        )
        assert ok.status_code == 200
        assert held["pending_override"]["action"] == "skip"
