"""One immutable request-stage decision shared by accounting and dispatch."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from coordinare.services.agent_runtime.errors import refuse_agent

if TYPE_CHECKING:
    from nare import Reply


@dataclass(frozen=True)
class RequestSelection:
    tools: list[dict[str, Any]]
    response_schema: dict[str, Any] | None = None
    response_mode: str | None = None
    tool_choice: str | None = None
    allowed_calls: frozenset[str] | None = None

    def metadata(self) -> dict[str, Any]:
        return {'tools': self.tools, 'response_schema': self.response_schema,
                'response_mode': self.response_mode, 'tool_choice': self.tool_choice}

    def validate_reply(self, reply: Reply) -> None:
        if self.allowed_calls is not None and any(call.name not in self.allowed_calls for call in reply.tool_calls):
            refuse_agent('Provider requested a tool outside the active request stage.',
                         code='agent_request_stage_violation')


class RequestStage(Protocol):
    def select(self, tools: list[dict[str, Any]], schema: dict[str, Any] | None,
               mode: str | None, choice: str | None) -> RequestSelection: ...
