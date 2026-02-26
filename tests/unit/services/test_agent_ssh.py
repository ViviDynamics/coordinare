from __future__ import annotations

import pytest

from coordinare.services.agent_ssh import AgentSSHService


@pytest.mark.asyncio
async def test_dispatch_card_delegates_to_remote_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgentSSHService(host="host", user="user", command="agent")

    async def _fake_run(payload: dict[str, object]) -> dict[str, object]:
        assert payload["action"] == "dispatch"
        return {"status": "accepted"}

    monkeypatch.setattr(service, "_run_remote", _fake_run)

    result = await service.dispatch_card({"id": "card-1"})

    assert result["status"] == "accepted"


@pytest.mark.asyncio
async def test_check_status_uses_status_action(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgentSSHService(host="host", user="user", command="agent")

    async def _fake_run(payload: dict[str, object]) -> dict[str, object]:
        assert payload == {"action": "status", "card_id": "card-1"}
        return {"status": "working"}

    monkeypatch.setattr(service, "_run_remote", _fake_run)

    result = await service.check_status("card-1")

    assert result["status"] == "working"


@pytest.mark.asyncio
async def test_check_health_uses_health_action(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgentSSHService(host="host", user="user", command="agent")

    async def _fake_run(payload: dict[str, object]) -> dict[str, object]:
        assert payload == {"action": "health"}
        return {"status": "healthy"}

    monkeypatch.setattr(service, "_run_remote", _fake_run)

    result = await service.check_health()

    assert result["status"] == "healthy"


@pytest.mark.asyncio
async def test_relay_feedback_uses_relay_action(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgentSSHService(host="host", user="user", command="agent")

    async def _fake_run(payload: dict[str, object]) -> dict[str, object]:
        assert payload["action"] == "relay_feedback"
        assert "review" in payload
        return {"status": "acknowledged"}

    monkeypatch.setattr(service, "_run_remote", _fake_run)

    result = await service.relay_feedback({"reviews": [{"id": "R1"}]})

    assert result["status"] == "acknowledged"
