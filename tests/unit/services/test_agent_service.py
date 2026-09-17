from __future__ import annotations

import pytest

from coordinare.protocol import ProtocolMessage, ProtocolResponse
from coordinare.services.agent_service import AgentService
from coordinare.transport.base import TransportError


class _MockTransport:
    def __init__(self, response: ProtocolResponse | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.last_message: ProtocolMessage | None = None
        self.last_timeout_override: int | None = None

    async def send(
        self,
        message: ProtocolMessage,
        *,
        timeout_override: int | None = None,
    ) -> ProtocolResponse:
        self.last_message = message
        self.last_timeout_override = timeout_override
        if self._error:
            raise self._error
        assert self._response is not None
        return self._response


class TestDispatchCard:
    @pytest.mark.asyncio
    async def test_dispatch_card_happy_path(self) -> None:
        transport = _MockTransport(
            response=ProtocolResponse(status="accepted", session_id="s1"),
        )
        service = AgentService(transport)

        result = await service.dispatch_card({
            "id": "ITEM_1",
            "title": "Fix bug",
            "description": "Details here",
            "acceptance_criteria": ["Test passes"],
            "status": "TODO",
        })

        assert result["status"] == "accepted"
        assert result["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_dispatch_card_sends_fr010_required_fields(self) -> None:
        transport = _MockTransport(
            response=ProtocolResponse(status="accepted", session_id="s1"),
        )
        service = AgentService(transport)

        await service.dispatch_card({
            "id": "ITEM_1",
            "title": "Fix bug",
            "description": "Details here",
            "acceptance_criteria": ["Test passes"],
            "status": "TODO",
        })

        msg = transport.last_message
        assert msg is not None
        assert msg.action == "dispatch"
        payload = msg.payload
        assert payload["title"] == "Fix bug"
        assert payload["description"] == "Details here"
        assert payload["acceptance_criteria"] == ["Test passes"]
        assert payload["id"] == "ITEM_1"
        assert payload["status"] == "TODO"

    @pytest.mark.asyncio
    async def test_dispatch_card_transport_error_returns_error_status(self) -> None:
        transport = _MockTransport(error=TransportError("connection lost"))
        service = AgentService(transport)

        result = await service.dispatch_card({"id": "ITEM_1"})

        assert result["status"] == "error"
        assert "connection lost" in result["reason"]

    @pytest.mark.asyncio
    async def test_dispatch_card_with_missing_fields(self) -> None:
        transport = _MockTransport(
            response=ProtocolResponse(status="accepted", session_id="s1"),
        )
        service = AgentService(transport)

        result = await service.dispatch_card({})

        assert result["status"] == "accepted"
        payload = transport.last_message.payload
        # Empty card_context passed through as-is
        assert payload == {}


class TestCheckHealth:
    @pytest.mark.asyncio
    async def test_check_health_happy_path(self) -> None:
        transport = _MockTransport(
            response=ProtocolResponse(status="accepted"),
        )
        service = AgentService(transport)

        result = await service.check_health()

        assert result["status"] == "accepted"
        assert transport.last_message.action == "health"
        assert transport.last_timeout_override == 10

    @pytest.mark.asyncio
    async def test_check_health_transport_error_returns_unknown(self) -> None:
        transport = _MockTransport(error=TransportError("unreachable"))
        service = AgentService(transport)

        result = await service.check_health()

        assert result["status"] == "unknown"
        assert "unreachable" in result["reason"]


class TestCheckStatus:
    @pytest.mark.asyncio
    async def test_check_status_happy_path(self) -> None:
        transport = _MockTransport(
            response=ProtocolResponse(status="working", session_id="s1", progress="Building"),
        )
        service = AgentService(transport)

        result = await service.check_status("s1")

        assert result["status"] == "working"
        assert result["progress"] == "Building"
        assert transport.last_message.action == "status"
        assert transport.last_message.session_id == "s1"

    @pytest.mark.asyncio
    async def test_check_status_transport_error_propagates(self) -> None:
        """045: TransportError must propagate to the caller (monitor_performer)
        so system_error_count is incremented and the retry budget can fire.
        Previously this was swallowed and returned as {"status": "unknown"},
        making the 044 retry budget dead code."""
        transport = _MockTransport(error=TransportError("timeout"))
        service = AgentService(transport)

        with pytest.raises(TransportError, match="timeout"):
            await service.check_status("s1")


class TestRelayFeedback:
    @pytest.mark.asyncio
    async def test_relay_feedback_happy_path(self) -> None:
        transport = _MockTransport(
            response=ProtocolResponse(status="acknowledged", session_id="s1"),
        )
        service = AgentService(transport)

        result = await service.relay_feedback({
            "session_id": "s1",
            "pr_url": "https://github.com/org/repo/pull/1",
            "comments": [{"reviewer": "alice", "body": "LGTM"}],
        })

        assert result["status"] == "acknowledged"
        msg = transport.last_message
        assert msg.action == "relay_feedback"
        assert msg.session_id == "s1"
        assert msg.payload["pr_url"] == "https://github.com/org/repo/pull/1"

    @pytest.mark.asyncio
    async def test_relay_feedback_transport_error_returns_error(self) -> None:
        transport = _MockTransport(error=TransportError("connection lost"))
        service = AgentService(transport)

        result = await service.relay_feedback({"session_id": "s1"})

        assert result["status"] == "error"
