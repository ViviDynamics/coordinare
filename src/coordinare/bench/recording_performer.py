"""Spec 134 — recording wrapper around the performer service.

The artifact's per-dispatch data (job/session/container ids, status, tokens,
timing, raw summary) is not visible to the fake GitHub service — it lives on the
performer service's ``dispatch_card`` / ``check_status`` returns. This thin wrapper
delegates to the real service (or a stub) and records every dispatch + terminal
status so the runner can assemble ``PersonaDispatch`` rows without parsing
structlog. Any method not wrapped passes through unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast


class RecordingPerformer:
    """Delegating recorder for an ``AgentServiceProtocol``-shaped service."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        # Raw per-call records the runner maps into artifact PersonaDispatch rows.
        self.dispatch_records: list[dict[str, Any]] = []
        self.status_records: list[dict[str, Any]] = []

    async def dispatch_card(self, card_context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        started = datetime.now(UTC)
        result = await self._delegate.dispatch_card(card_context, **kwargs)
        self.dispatch_records.append({
            "stage": card_context.get("stage") or card_context.get("role", ""),
            "role": card_context.get("role", ""),
            "card_id": card_context.get("item_id") or card_context.get("card_id", ""),
            "started_at": started.isoformat(),
            "session_id": (result or {}).get("session_id"),
            "job_id": (result or {}).get("job_id"),
            "container_id": (result or {}).get("container_id"),
            "status": (result or {}).get("status"),
        })
        return cast("dict[str, Any]", result)

    async def check_status(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        result = await self._delegate.check_status(session_id, **kwargs)
        status = (result or {}).get("status")
        # Terminal statuses carry the full PerformerResponse JSON in `summary`;
        # pull best-effort tokens_processed from its metrics (may be absent).
        if status in {"succeeded", "failed", "cancelled", "error", "done"}:
            self.status_records.append({
                "session_id": session_id,
                "status": status,
                "at": datetime.now(UTC).isoformat(),
                "tokens_processed": _tokens_from_summary((result or {}).get("summary")),
                "summary": (result or {}).get("summary"),
            })
        return cast("dict[str, Any]", result)

    def __getattr__(self, name: str) -> Any:
        # Pass through anything the graph reads that we don't wrap
        # (check_health, relay_feedback, attributes, etc.).
        return getattr(self._delegate, name)


def _tokens_from_summary(summary: Any) -> int | None:
    if not isinstance(summary, str):
        return None
    try:
        data = json.loads(summary)
    except (ValueError, TypeError):
        return None
    metrics = data.get("metrics") if isinstance(data, dict) else None
    if isinstance(metrics, dict):
        tokens = metrics.get("tokens_processed")
        if isinstance(tokens, int):
            return tokens
    return None
