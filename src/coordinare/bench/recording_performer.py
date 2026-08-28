"""Spec 134 — recording wrapper around the performer service.

The artifact's per-dispatch data (job/session/container ids, status, tokens,
timing) is not visible to the fake GitHub service — it lives on the
performer service's ``dispatch_card`` / ``check_status`` returns. This thin wrapper
delegates to the real service (or a stub) and records every dispatch + terminal
status so the runner can assemble ``PersonaDispatch`` rows without parsing
structlog. Any method not wrapped passes through unchanged.
"""

from __future__ import annotations

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
        # 151 review fix: record the dispatch even when it RAISES. coordinare catches
        # dispatch exceptions (dispatch_performer.py:1685), so appending only on
        # success made a run where every container failed to start emit an artifact
        # with ZERO dispatches — "nothing happened" instead of "8 dispatches, all
        # failed", which is the datum this bench exists to collect.
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            result = await self._delegate.dispatch_card(card_context, **kwargs)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._record_dispatch(card_context, result, started, error)
        return cast("dict[str, Any]", result)

    def _record_dispatch(
        self,
        card_context: dict[str, Any],
        result: dict[str, Any] | None,
        started: datetime,
        error: str | None,
    ) -> None:
        self.dispatch_records.append({
            "stage": card_context.get("stage") or card_context.get("role", ""),
            "role": card_context.get("role", ""),
            "card_id": (
                card_context.get("item_id")
                or card_context.get("card_id")
                or card_context.get("id", "")
            ),
            # 151 (SC-002): the real per-dispatch model/backend for the artifact.
            "model": card_context.get("model", ""),
            "backend": card_context.get("backend", ""),
            "started_at": started,
            "session_id": (result or {}).get("session_id"),
            "job_id": (result or {}).get("job_id"),
            "container_id": (result or {}).get("container_id"),
            "status": "error" if error else (result or {}).get("status"),
            "error": error,
        })

    async def check_status(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        result = await self._delegate.check_status(session_id, **kwargs)
        status = (result or {}).get("status")
        # Only the non-terminal branch reports "working" — every other return is the
        # parsed PerformerResponse, whose `status` IS the terminal marker
        # (succeeded / pr_opened / changes_requested / qa_failed / …) and whose
        # `metrics` carries tokens_processed. This is the dispatch's true finish time.
        if status and status != "working":
            self.status_records.append({
                "session_id": session_id,
                "status": status,
                "at": datetime.now(UTC),
                "tokens_processed": _tokens(result),
            })
        return cast("dict[str, Any]", result)

    def __getattr__(self, name: str) -> Any:
        # Pass through anything the graph reads that we don't wrap
        # (check_health, relay_feedback, attributes, etc.).
        return getattr(self._delegate, name)

    @property  # type: ignore[misc]  # spoofing __class__ is the point (see below)
    def __class__(self) -> Any:
        # 151 review fix: `isinstance` consults __class__, so spoofing it (the same
        # idiom unittest.mock uses for spec'd mocks) makes this wrapper transparent
        # to the `isinstance(service, HTTPPerformerService)` gates in the graph.
        # Without it, dispatch_performer silently drops the env-cache extra_volumes
        # (:1683) and falls back to DEFAULT_DEVENV_ROOT (:1595), so the bench would
        # exercise a different dispatch path than production.
        # Detect this wrapper with `is_recorder()` below — never with isinstance.
        return self._delegate.__class__


def _tokens(result: Any) -> int | None:
    metrics = result.get("metrics") if isinstance(result, dict) else None
    tokens = metrics.get("tokens_processed") if isinstance(metrics, dict) else None
    return tokens if isinstance(tokens, int) else None


def is_recorder(svc: Any) -> bool:
    """True for a RecordingPerformer. `isinstance` cannot be used — the wrapper
    spoofs __class__ so the graph's HTTPPerformerService gates keep working."""
    return hasattr(svc, "dispatch_records") and hasattr(svc, "status_records")
