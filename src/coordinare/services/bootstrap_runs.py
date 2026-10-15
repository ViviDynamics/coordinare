"""Bootstrap run reads, restart recovery, live activity and cancellation."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.nodes.monitor.activity import _feed_fields
from coordinare.lib.redaction import redact_state_value
from coordinare.services.activity_log import EventLink
from coordinare.services.bootstrap_history import (
    ACTIVITY_STAGE,
    HISTORY_LIMIT,
    RUN_SNAPSHOT_KIND,
    BootstrapAttempt,
    failure_detail,
    history_of,
    persist_attempt,
    record_activity,
)
from coordinare.services.bootstrap_status import attempt_view
from coordinare.services.event_capture import performer_feed_capture
from coordinare.services.event_store_query import MAX_LIMIT, EventFilters

if TYPE_CHECKING:
    from collections.abc import Mapping

EVENT_LIMIT = MAX_LIMIT
RECOVERY_PAGE_LIMIT = 20
SEEN_EVENT_LIMIT = 4096
_log = structlog.get_logger(__name__)


async def _query(store: Any, filters: EventFilters, cursor: str | None = None, limit: int = MAX_LIMIT) -> dict[str, Any]:
    """A missing or unreadable event database never changes bootstrap decisions."""
    try:
        page: dict[str, Any] = await store.query_events(filters, cursor=cursor, limit=limit)
    except (sqlite3.Error, OSError) as exc:
        _log.warning("env_bootstrap.history_unavailable", error_type=type(exc).__name__)
        return {"events": [], "cursor": None}
    return page


@dataclass
class BootstrapRuntime:
    """The dispatched owner and poll task, independent of later config changes."""

    service: Any
    job_id: str
    task: asyncio.Task[Any]
    cancelling: bool = False


def _decode(data: dict[str, Any]) -> BootstrapAttempt:
    if not isinstance(data, dict):
        message = "Bootstrap snapshot data is not an object"
        raise TypeError(message)
    allowed = {item.name for item in fields(BootstrapAttempt)}
    values = {key: value for key, value in data.items() if key in allowed}
    for key in ("started_at", "ended_at", "last_activity_at"):
        value = values.get(key)
        values[key] = datetime.fromisoformat(value) if value else None
    if (values["started_at"] is None or values["started_at"].tzinfo is None
            or not isinstance(values.get("run_id"), str) or not values["run_id"]
            or not all(type(values.get(key)) is int for key in ("number", "attempt"))):
        message = "Bootstrap snapshot has no run identity or timestamp"
        raise ValueError(message)
    return BootstrapAttempt(**values)


async def load_bootstrap_history(state: Mapping[str, Any], symphonies: list[str]) -> None:
    """Read bounded pages per symphony, keeping only the latest snapshot of each run."""
    history, store = history_of(state), state.get("event_store")
    if history is None or store is None:
        return
    for symphony in symphonies:
        records: dict[str, BootstrapAttempt] = {}
        cursor = None
        for _ in range(RECOVERY_PAGE_LIMIT):
            page = await _query(store, EventFilters(symphony=symphony, kind=RUN_SNAPSHOT_KIND),
                                            cursor=cursor, limit=MAX_LIMIT)
            for row in page["events"]:
                if row["run_id"] in records:
                    continue
                try:
                    records[row["run_id"]] = _decode(row["data"])
                except (ValueError, TypeError, KeyError):
                    continue
            cursor = page.get("cursor")
            if not cursor or len(records) >= HISTORY_LIMIT:
                break
        history.restore(symphony, sorted(records.values(), key=lambda item: item.started_at, reverse=True))


def record_verification(state: Mapping[str, Any], symphony: str, passed: bool | None, output: str | None) -> None:
    """Keep the authoritative verifier verdict and a bounded, redacted output tail."""
    history = history_of(state)
    record = history.open_attempt(symphony) if history is not None else None
    if record is None:
        return
    record.verify_verdict = "passed" if passed else "unavailable" if passed is None else "failed"
    record.reason_tail = failure_detail(output)[1]
    persist_attempt(state, symphony, record)


def record_bootstrap_events(state: Mapping[str, Any], symphony: str, job_id: str, events: list[Any]) -> None:
    """Attribute live backend events to the bootstrap and persist them once per poll window."""
    history = history_of(state)
    record = history.open_attempt(symphony) if history is not None else None
    if record is None or record.job_id != job_id or not isinstance(events, list):
        return
    seen = record.seen_events
    for event in events or []:
        if not isinstance(event, dict):
            continue
        with contextlib.suppress(Exception):
            identity = hashlib.sha256(json.dumps(event, sort_keys=True, default=str).encode()).hexdigest()
            if identity in seen:
                continue
            seen.add(identity)
            if len(seen) > SEEN_EVENT_LIMIT:
                seen.clear()
                seen.add(identity)
            _record_event(state, record, symphony, event, identity)


def _record_event(state: Mapping[str, Any], record: BootstrapAttempt, symphony: str,
                  event: dict[str, Any], identity: str) -> None:
    safe = redact_state_value(event)
    text = str(safe.get("text") or safe.get("detail") or "")
    stamp = safe.get("timestamp") or datetime.now(UTC).isoformat()
    try:
        at = datetime.fromisoformat(stamp)
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        at = datetime.now(UTC)
    record.last_activity_at = max(record.last_activity_at or at, at)
    safe["timestamp"] = at.isoformat()
    log, store = state.get("activity_log"), state.get("event_store")
    if store is not None:
        store.emit({"ts": at.isoformat(), "symphony": symphony, "run_id": record.run_id,
                    "stage": ACTIVITY_STAGE, "source": "performer", "kind": safe.get("type", "output"),
                    "message": text, "data": safe})
    if log is not None:
        with contextlib.suppress(Exception), performer_feed_capture():
            log.record(**_feed_fields(safe), text=text, symphony=symphony, stage=ACTIVITY_STAGE,
                       session_id=record.run_id, performer_id=record.performer_id,
                       is_delta=safe.get("is_delta") is True, stream_id=str(safe.get("stream_id") or ""),
                       source_event_id=identity,
                       link=EventLink(event_at=at.isoformat(), transcript_seq=safe.get("transcript_seq")))


async def read_bootstrap_run(state: Mapping[str, Any], symphony: str, run_id: str) -> dict[str, Any] | None:
    """One scoped run, with its latest persisted events even after a restart."""
    history, store = history_of(state), state.get("event_store")
    recent = history.recent(symphony, HISTORY_LIMIT) if history is not None else []
    record = next((row for row in recent if row.run_id == run_id), None)
    if record is None and store is not None:
        page = await _query(store, EventFilters(symphony=symphony, run_id=run_id, kind=RUN_SNAPSHOT_KIND), limit=1)
        if page["events"]:
            with contextlib.suppress(ValueError, TypeError, KeyError):
                record = _decode(page["events"][0]["data"])
                record.restored = True
    if record is None:
        return None
    events = []
    if store is not None:
        page = await _query(store, EventFilters(symphony=symphony, run_id=run_id),
                                        limit=EVENT_LIMIT)
        events = [row["data"] for row in reversed(page["events"])
                  if row["kind"] != RUN_SNAPSHOT_KIND and isinstance(row["data"], dict)]
    if not events and state.get("activity_log") is not None:
        events = [entry for entry in state["activity_log"].snapshot()
                  if entry.get("symphony") == symphony and entry.get("session_id") == run_id][-EVENT_LIMIT:]
    now = datetime.now(UTC)
    view = attempt_view(record, now)
    view.update({"run_id": record.run_id, "symphony": symphony, "commit": record.commit,
                 "performer_id": record.performer_id, "manifest": record.manifest,
                 "verify_verdict": record.verify_verdict, "reason_tail": record.reason_tail,
                 "last_activity_at": record.last_activity_at.isoformat() if record.last_activity_at else None,
                 "events": events, "history": attempt_view(record, now)})
    view["state"] = "verifying" if record.verifying else record.outcome
    if events:
        view["last_activity_at"] = events[-1].get("timestamp") or view["last_activity_at"]
    if record.restored and record.ended_at is None:
        view.update({"running": False, "outcome": "interrupted", "state": "interrupted"})
    return dict(redact_state_value(view))


async def cancel_bootstrap(state: Mapping[str, Any], symphony: str, actor: str) -> dict[str, Any]:
    """Quiesce polling, stop through the captured owner, then close through cache completion."""
    runtimes = state.get("bootstrap_runtimes") or {}
    runtime = runtimes.get(symphony)
    cache = (state.get("env_cache") or {}).get(symphony)
    history = history_of(state)
    record = history.open_attempt(symphony) if history is not None else None
    if (runtime is None or runtime.cancelling or cache is None
            or not cache.bootstrap_in_flight or record is None or record.job_id != runtime.job_id):
        message = "No bootstrap is running for this symphony"
        raise ValueError(message)
    service = state.get("env_cache_service")
    if service is None:
        message = "Env cache service not available"
        raise RuntimeError(message)
    runtime.cancelling = True
    runtime.task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await runtime.task
    stopped = False
    try:
        stopped = bool(await runtime.service.stop_session_confirmed(runtime.job_id))
    except Exception:
        stopped = False
    reason = str(redact_state_value(f"cancelled by {actor}"))
    service.on_bootstrap_complete(symphony, False, state, error=reason)
    record_activity(state, symphony, "operator_action", reason)
    runtimes.pop(symphony, None)
    return {"status": "cancelled", "symphony": symphony, "stopped": stopped}


def release_bootstrap_runtime(state: Mapping[str, Any], symphony: str, task: asyncio.Task[Any]) -> None:
    """Release only this poller's owner, without racing a replacement or a cancellation."""
    runtimes = state.get("bootstrap_runtimes") or {}
    runtime = runtimes.get(symphony)
    cache = (state.get("env_cache") or {}).get(symphony)
    if (runtime is not None and runtime.task is task and not runtime.cancelling
            and not getattr(cache, "bootstrap_in_flight", False)):
        runtimes.pop(symphony, None)
