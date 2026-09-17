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
        # Pass through ALL card_context fields to the performer.
        # Previous code selectively allowlisted fields, which silently dropped
        # role, relay_feedback, pr_url, pr_node_id, backend, model, github_api_url,
        # and architecture_plan_path — breaking feedback loops, role routing,
        # and features 036/037.
        # Ensure all values are JSON-serializable (card_context may contain
        # datetime, Path, or other non-serializable types from state)
        import json
        payload: dict[str, Any] = json.loads(json.dumps(dict(card_context), default=str))
        if workspace_info is not None:
            payload["repo_url"] = workspace_info.repo_url
            payload["branch"] = workspace_info.branch
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
                message, timeout_override=10,
            )
            return response.model_dump()
        except TransportError as exc:
            logger.warning("check_health_transport_error", error=str(exc))
            return {"status": "unknown", "reason": str(exc)}

    async def check_status(self, session_id: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        message = ProtocolMessage(action="status", session_id=session_id, payload=payload or {})
        # 045: Don't swallow TransportError here.  Returning {"status": "unknown"}
        # hid transport failures from monitor_performer's error-counting logic,
        # making the retry budget (044) dead code.  Let the error propagate so
        # monitor_performer can increment system_error_count and eventually
        # escalate to blocked.  Also log here so this service records its own
        # method-specific context (``check_status_transport_error``); the
        # downstream ``monitor_performer.transport_error`` log captures the
        # same error=str(exc) under a different event key, which is useful
        # for filtering by which surface surfaced the failure.
        try:
            response: ProtocolResponse = await self._transport.send(message)
        except TransportError as exc:
            logger.warning("check_status_transport_error", error=str(exc))
            raise
        return response.model_dump()

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
