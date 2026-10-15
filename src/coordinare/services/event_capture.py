from __future__ import annotations

import contextlib
import json
import threading
from collections import deque
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from structlog.contextvars import get_contextvars

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, MutableMapping

    from coordinare.services.activity_log import ActivityEntry
    from coordinare.services.event_store import EventStore

_STORE: EventStore | None = None
_SYMPHONY: Callable[[str], str] | None = None
LOG_LEVELS = frozenset({"info", "warning", "warn", "error", "exception", "critical", "fatal"})
EARLY_RECORD_LIMIT = 128
EARLY_BYTE_LIMIT = 1024 * 1024
_EARLY_RECORDS: deque[str] = deque()
_CAPTURE_LOCK = threading.Lock()
_ACTIVITY_CAPTURE: ContextVar[bool] = ContextVar("event_store_activity_capture", default=True)
ERROR_KINDS = frozenset({"error", "failed", "failure", "blocked"})


def install_event_store(
    store: EventStore | None, resolver: Callable[[str], str] | None = None,
) -> None:
    global _STORE, _SYMPHONY
    with _CAPTURE_LOCK:
        _STORE, _SYMPHONY = store, resolver
        if store is not None:
            for raw in _EARLY_RECORDS:
                store.emit(json.loads(raw))
        _EARLY_RECORDS.clear()


@contextlib.contextmanager
def performer_feed_capture() -> Iterator[None]:
    """The raw performer row owns persistence while its live feed is recorded."""
    token = _ACTIVITY_CAPTURE.set(False)
    try:
        yield
    finally:
        _ACTIVITY_CAPTURE.reset(token)


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
    if store is None or not _ACTIVITY_CAPTURE.get():
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
    kind = str(event.get("event") or "")
    if (
        method not in LOG_LEVELS
        or kind.startswith(("event_store.", "coordinare.services.event_store"))
    ):
        return
    row = {
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
    }
    with _CAPTURE_LOCK:
        if _STORE is not None:
            _STORE.emit(row)
        else:
            raw = json.dumps(row, default=str, ensure_ascii=True)
            if len(raw) > EARLY_BYTE_LIMIT:
                return
            _EARLY_RECORDS.append(raw)
            while len(_EARLY_RECORDS) > EARLY_RECORD_LIMIT or sum(map(len, _EARLY_RECORDS)) > EARLY_BYTE_LIMIT:
                _EARLY_RECORDS.popleft()


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
