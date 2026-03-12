from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from coordinare.protocol import ProtocolMessage, ProtocolResponse
from coordinare.transport.base import TransportError

if TYPE_CHECKING:
    from coordinare.workspace import WorkspaceInfo

logger = structlog.get_logger(__name__)


class AgentService:
    def __init__(self, transport: Any) -> None:
        self._transport = transport

    async def dispatch_card(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": card_context.get("title", ""),
            "description": card_context.get("description", ""),
            "acceptance_criteria": card_context.get("acceptance_criteria", []),
            "board_card_id": str(card_context.get("id", "")),
            "column": card_context.get("status", ""),
        }
        issue_url = card_context.get("issue_url", "")
        if issue_url:
            payload["issue_url"] = issue_url
        issue_number = card_context.get("issue_number")
        if issue_number:
            payload["issue_number"] = issue_number
        clarifications = card_context.get("clarifications")
        if clarifications:
            payload["clarifications"] = [
                {"questions": c.get("questions", []), "answer": c.get("answer", "")}
                for c in clarifications
                if isinstance(c, dict)
            ]
        if workspace_info is not None:
            payload["repo_url"] = workspace_info.repo_url
            payload["branch"] = workspace_info.branch
            if workspace_info.github_token:
                payload["github_token"] = workspace_info.github_token
            if workspace_info.path is not None:
                payload["workspace_path"] = str(workspace_info.path)
        message = ProtocolMessage(
            action="dispatch",
            payload=payload,
        )
        try:
            response: ProtocolResponse = await self._transport.send(message)
            result: dict[str, Any] = response.model_dump()
            return result
        except TransportError as exc:
            logger.warning("dispatch_card_transport_error", error=str(exc))
            return {"status": "error", "reason": str(exc)}

    async def check_health(self) -> dict[str, Any]:
        message = ProtocolMessage(action="health")
        try:
            response: ProtocolResponse = await self._transport.send(
                message, timeout_override=10
            )
            return response.model_dump()
        except TransportError as exc:
            logger.warning("check_health_transport_error", error=str(exc))
            return {"status": "unknown", "reason": str(exc)}

    async def check_status(self, session_id: str) -> dict[str, Any]:
        message = ProtocolMessage(action="status", session_id=session_id)
        try:
            response: ProtocolResponse = await self._transport.send(message)
            return response.model_dump()
        except TransportError as exc:
            logger.warning("check_status_transport_error", error=str(exc))
            return {"status": "unknown", "reason": str(exc)}

    def get_agent_logs(self) -> list[str]:
        """Return buffered stderr lines from the active performer process.

        Falls back to an empty list when the transport does not support log
        buffering (e.g. SSH or Kubernetes transports).
        """
        getter = getattr(self._transport, "agent_logs", None)
        if getter is None:
            return []
        return list(getter) if not callable(getter) else getter()

    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]:
        session_id = review_payload.get("session_id", "")
        message = ProtocolMessage(
            action="relay_feedback",
            session_id=session_id,
            payload={
                "pr_url": review_payload.get("pr_url", ""),
                "comments": review_payload.get("comments", review_payload.get("reviews", [])),
            },
        )
        try:
            response: ProtocolResponse = await self._transport.send(message)
            return response.model_dump()
        except TransportError as exc:
            logger.warning("relay_feedback_transport_error", error=str(exc))
            return {"status": "error", "reason": str(exc)}
