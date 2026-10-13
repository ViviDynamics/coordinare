"""Spec 129: the daemon and performer redaction modules stay identical, and the stdio
status path tolerates the transcript target key (LOW 13)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from performer import transcript_redaction as performer_copy
from performer.protocol import PerformerMessage

from coordinare.lib import transcript_redaction as daemon_copy
from coordinare.protocol import ProtocolResponse
from coordinare.services.agent_service import AgentService


def test_daemon_and_performer_redaction_modules_are_byte_identical() -> None:
    daemon_bytes = Path(daemon_copy.__file__).read_bytes()
    performer_bytes = Path(performer_copy.__file__).read_bytes()
    assert daemon_bytes == performer_bytes, (
        "transcript_redaction.py must be copied, not edited: "
        "copy src/coordinare/lib/transcript_redaction.py over the performer copy"
    )


def test_both_sides_share_the_same_value_patterns_and_stems() -> None:
    assert daemon_copy.VALUE_PATTERNS == performer_copy.VALUE_PATTERNS
    assert daemon_copy.NAME_STEMS == performer_copy.NAME_STEMS


class _CapturingTransport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> ProtocolResponse:
        self.sent.append(message)
        return ProtocolResponse(status="working", session_id="sess-stdio")


async def test_stdio_status_carries_the_transcript_target_to_the_performer() -> None:
    """LOW 13: the observer probe's extra payload key reaches a stdio receiver intact."""
    transport = _CapturingTransport()
    service = AgentService(transport)
    payload = {"transcript_target": {"symphony": "alpha", "card_id": "PVTI_s"}}
    await service.check_status("sess-stdio", payload=payload)
    [message] = transport.sent
    received = PerformerMessage.model_validate(message.model_dump())
    assert received.action == "status"
    assert received.payload["transcript_target"] == payload["transcript_target"]
