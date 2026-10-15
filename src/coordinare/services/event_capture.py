from __future__ import annotations

import contextlib
import hashlib
import json
from typing import TYPE_CHECKING, Any

from structlog.contextvars import get_contextvars

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableMapping

    from coordinare.services.activity_log import ActivityEntry
    from coordinare.services.event_store import EventStore

_STORE: EventStore | None = None
_SYMPHONY: Callable[[str], str] | None = None
LOG_LEVELS = frozenset({"info", "warning", "warn", "error", "exception", "critical", "fatal"})
ERROR_KINDS = frozenset({"error", "failed", "failure", "blocked"})


def install_event_store(
    store: EventStore | None, resolver: Callable[[str], str] | None = None,
) -> None:
    global _STORE, _SYMPHONY
    _STORE, _SYMPHONY = store, resolver


def _symphony(card_id: str) -> str:
    context = get_contextvars()
    return str(context.get("symphony") or (_SYMPHONY(card_id) if _SYMPHONY else ""))


def _source(kind: str) -> str:
    if kind == "operator_action":
        return "operator"
    if "bootstrap" in kind or kind.startswith("env_cache."):
        return "bootstrap"
    return "daemon"


def capture_activity(entry: ActivityEntry) -> None:
    store = _STORE
    if store is None:
        return
    data = entry.to_dict()
    store.emit(
        {
            "ts": entry.timestamp.isoformat(),
            "symphony": _symphony(entry.card_id),
            "card_id": entry.card_id,
            "run_id": entry.session_id,
            "stage": entry.stage,
            "source": _source(entry.activity_type),
            "kind": entry.activity_type,
            "level": "error" if entry.activity_type in ERROR_KINDS else "info",
            "message": entry.text,
            "data": data,
        },
    )


def symphony_for_card(state: Any, card_id: str) -> str:
    session = (state.get("active_sessions") or {}).get(card_id) or {}
    card = session.get("current_card") or {}
    return str(
        session.get("current_symphony")
        or card.get("symphony")
        or state.get("current_symphony")
        or "",
    )


def event_store_processor(
    _: Any, method: str, event: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Never logs, never mutates the renderer's event, and ignores internal records."""
    with contextlib.suppress(Exception):
        _capture_log(method, event)
    return event


def _capture_log(method: str, event: MutableMapping[str, Any]) -> None:
    store = _STORE
    kind = str(event.get("event") or "")
    if (
        store is None
        or method not in LOG_LEVELS
        or kind.startswith(("event_store.", "coordinare.services.event_store"))
    ):
        return
    store.emit(
        {
            "ts": event.get("timestamp"),
            "symphony": event.get("symphony") or "",
            "card_id": event.get("card_id") or "",
            "run_id": event.get("run_id") or event.get("session_id") or "",
            "stage": event.get("stage") or event.get("performer_stage") or "",
            "source": _source(kind),
            "kind": kind,
            "level": method,
            "message": str(event.get("message") or event.get("error") or kind),
            "data": dict(event),
        },
    )


def capture_performer_events(
    state: Mapping[str, Any], events: list[Any], attribution: dict[str, Any],
) -> None:
    store = state.get("event_store")
    if store is None:
        return
    run_id = str((state.get("agent_dispatch") or {}).get("session_id") or "")
    for event in events:
        if not isinstance(event, dict):
            continue
        identity = json.dumps([run_id, attribution, event], sort_keys=True, default=str)
        if not store.remember(hashlib.sha256(identity.encode()).hexdigest()):
            continue
        store.emit(
            {
                "symphony": state.get("current_symphony") or _symphony(attribution["card_id"]),
                **attribution,
                "run_id": run_id,
                "source": "performer",
                "kind": str(event.get("type") or "output"),
                "level": "error" if event.get("type") in ERROR_KINDS else "info",
                "message": event.get("text") or event.get("detail") or "",
                "data": {**event, "run_id": run_id, "transcript_seq": event.get("transcript_seq")},
            },
        )
