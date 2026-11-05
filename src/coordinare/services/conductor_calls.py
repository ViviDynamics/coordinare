"""Transcripts for the conductor's own model calls (spec 131).

The conductor calls a model for its own decisions: assessing a card, classifying scope,
feedback and comments, the config assistant, env-manifest and env-signature inference, and
the observer. Every such call goes through this module, and each one is written as one
#129-format transcript::

    <transcripts root>/conductor/<card id or "system">/<call id>.jsonl

The first line is the run header (the #130 shape) with a ``conductor`` section that names
the call kind, the symphony, the endpoint, the parameters and the decision. The records
that follow are the reply text, tool calls, the usage (staged ``conductor:<kind>`` so the
#134 ledger counts it under the card), any error, and a closing ``result`` whose text is
the decision.

Two entry points:

* :func:`conductor_call` opens a call around a model call and its parsing. The caller
  passes the decision it made from the output to :meth:`CallRecord.decide`.
* :meth:`ConductorCallLog.invoke` is the one place a backend sends a model request. A send
  made with no open call is recorded as kind ``unattributed``, so nothing escapes the record.

The file is written once, when the call closes, on a worker thread. A failed write is logged
and dropped: neither the call nor its decision waits for, or depends on, the disk. Every
record passes through the run-transcript writer, so redaction, the byte cap, the link and
permission rules and the daemon-only root all apply unchanged. When transcription is off the
log does no I/O, and the model call is made exactly as before.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.lib.path_segments import CONDUCTOR_FOLDER
from coordinare.observability import get_current_symphony

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from coordinare.services.run_transcript import TranscriptCollector

logger = structlog.get_logger(__name__)

#: The card folder for calls that belong to no card.
SYSTEM_CARD = "system"
#: The kind of a model call that no call site opened.
UNATTRIBUTED_KIND = "unattributed"
UNBOUND_SYMPHONY = "__default__"
# The run header is written in the #130 shape. run_header.py owns these values. They are repeated
# here because importing run_header would close an import cycle (config imports the observer,
# and run_header reaches config through persona_service). tests/unit/test_131_conductor_calls.py
# asserts they match.
RUN_HEADER_TYPE = "run_header"
RUN_HEADER_VERSION = 1

#: How long shutdown waits for the conductor's pending transcript writes (131).
SHUTDOWN_DRAIN_SECONDS = 5.0
PROMPT_LIMIT_CHARS = 98_304
DECISION_LIMIT_CHARS = 500
NO_DECISION_TEXT = "no decision recorded"
NO_USAGE_REASON = "the backend reports no token usage for this call"
DECISION_UNAVAILABLE = "decision unavailable"
_HEADER_BACKEND = "conductor"
_HEADER_TRANSPORT = "direct"
_TOOL_TYPE = "tool_call"


@dataclass(frozen=True)
class ModelRequest:
    """What a call sends: the model, the endpoint name, the system and user text, the parameters.

    ``endpoint`` is the configured name (a model endpoint or a backend name), never a URL or
    a credential.
    """

    model: str
    endpoint: str
    system: str
    prompt: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    name: str
    call_id: str
    input: Any = None


@dataclass(frozen=True)
class ModelReply:
    """What came back. ``usage`` keys are the ledger's four kinds; None means not reported.

    ``error`` and ``timed_out`` describe a failed call that returned rather than raised. ``note``
    is the reason a caller may show when there is no text (the CLI backends' rationale).
    """

    text: str = ""
    model: str | None = None
    usage: Mapping[str, int] | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    error: str | None = None
    timed_out: bool = False
    note: str = ""
    finish_reason: str | None = None


@dataclass
class CallRecord:
    """One conductor call, from open to close. Written once, when it closes."""

    kind: str
    card_id: str | None
    symphony: str
    call_id: str
    started_at: datetime
    request: ModelRequest | None = None
    reply: ModelReply | None = None
    error: str | None = None
    timed_out: bool = False
    decision: str | None = None

    def decide(self, decision: str) -> None:
        """The decision the call produced, from its output (e.g. ``assessed: ready``).

        Never raises: recording a decision must not change the decision or the call that made it.
        """
        try:
            self.decision = str(decision)[:DECISION_LIMIT_CHARS]
        except Exception:  # a value that cannot be written as text is recorded as unavailable
            self.decision = DECISION_UNAVAILABLE


_CURRENT: ContextVar[CallRecord | None] = ContextVar("conductor_call", default=None)


class ConductorCallLog:
    """Writes the conductor's call transcripts. One per daemon; a log with no collector is inert."""

    def __init__(self, collector: TranscriptCollector | None) -> None:
        self._collector = collector
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self._collector is not None and self._collector.enabled

    @asynccontextmanager
    async def call(
        self, kind: str, *, card_id: str | None = None, symphony: str | None = None,
    ) -> AsyncIterator[CallRecord]:
        """Open one call. Its model sends made inside the block are recorded on it.

        With no enabled log the call is not set as the current call, so a send made inside it
        is recorded by whatever log that send itself carries, instead of into a record that
        is dropped.
        """
        record = CallRecord(
            kind=kind,
            card_id=card_id or None,
            symphony=symphony or _bound_symphony(),
            call_id=uuid.uuid4().hex,
            started_at=datetime.now(UTC),
        )
        if not self.enabled:
            yield record
            return
        token = _CURRENT.set(record)
        try:
            yield record
        finally:
            try:
                _CURRENT.reset(token)
            except ValueError:  # the call was closed in another context: nothing to restore
                _CURRENT.set(None)
            self._close(record)

    async def invoke(
        self, request: ModelRequest, send: Callable[[], Awaitable[ModelReply]],
    ) -> ModelReply:
        """Send one model request and record it on the open call. Returns the reply unchanged."""
        record = _CURRENT.get()
        if record is None:
            async with self.call(UNATTRIBUTED_KIND) as implicit:
                return await _send_into(implicit, request, send)
        return await _send_into(record, request, send)

    async def drain(self, within: float | None = None) -> None:
        """Wait for the pending writes, for at most ``within`` seconds when one is given.

        Shutdown uses the bound, so a stuck disk cannot hold the daemon. The writes themselves are
        not cancelled: one still pending after the bound finishes on its own, if the process lives
        long enough, and otherwise its call loses its file.
        """
        loop = asyncio.get_running_loop()
        deadline = None if within is None else loop.time() + within
        while self._pending:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return
            await asyncio.wait(list(self._pending), timeout=remaining)

    def _close(self, record: CallRecord) -> None:
        """Schedule the call's write. Never raises: closing a call cannot change its outcome."""
        try:
            # A call that never reached a model send made no model call, so it has nothing to record.
            if not self.enabled or record.request is None:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._persist(record)
                return
            task = loop.create_task(asyncio.to_thread(self._persist, record))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        except Exception as exc:  # best effort: a failed transcript never reaches the call
            logger.warning("conductor_call.close_failed", call_kind=record.kind, exc_type=type(exc).__name__)

    def _persist(self, record: CallRecord) -> None:
        """Write the call's file. Blocking: runs on a worker thread. Never raises."""
        collector = self._collector
        if collector is None:
            return
        try:
            # A one-shot writer: the live-writer map is the poll path's, and this file is never polled.
            writer = collector.conductor_writer(
                run_id=record.call_id,
                card_id=record.card_id or SYSTEM_CARD,
            )
            writer.set_stage(f"conductor:{record.kind}")
            writer.stage_run_header(_header(record))
            rows = _records(record)
            writer.append_batch(rows, last_seq=len(rows))
            # The usage is counted under the card, the stage, and the symphony the call names.
            # A call that names none stays in the conductor's own folder.
            taken = writer.drain_usage()
            if taken:
                collector.usage.observe(
                    record.call_id,
                    symphony=record.symphony or CONDUCTOR_FOLDER,
                    card=writer.parts[1],
                    stage=writer.stage,
                    records=taken,
                )
        except Exception as exc:  # best effort: a failed transcript never reaches the call
            logger.warning(
                "conductor_call.transcript_failed",
                call_kind=record.kind,
                exc_type=type(exc).__name__,
            )


async def _send_into(
    record: CallRecord, request: ModelRequest, send: Callable[[], Awaitable[ModelReply]],
) -> ModelReply:
    record.request = request
    try:
        reply = await send()
    except BaseException as exc:
        text = str(exc)
        record.error = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
        record.timed_out = isinstance(exc, TimeoutError)
        raise
    record.reply = reply
    if reply.error is not None:
        record.error = reply.error
        record.timed_out = reply.timed_out
    return reply


#: The log used when no transcript log is wired (tests, and backends built without one).
NULL_CALL_LOG = ConductorCallLog(None)


def conductor_call(
    source: object, *, kind: str, card_id: str | None = None, symphony: str | None = None,
) -> AbstractAsyncContextManager[CallRecord]:
    """Open a conductor call through the log a backend (or a log) carries.

    ``source`` is a :class:`ConductorCallLog` or any object with a ``calls`` log attribute.
    Anything else records nothing, so a test double behaves as it always did.
    """
    log = source if isinstance(source, ConductorCallLog) else getattr(source, "calls", None)
    if not isinstance(log, ConductorCallLog):
        log = NULL_CALL_LOG
    return log.call(kind, card_id=card_id, symphony=symphony)


def assessment_decision(assessment: Any) -> str:
    """The decision an assessment (``sufficient`` / ``questions`` / ``dependencies``) reached.

    Never raises, whatever the assessment holds: the caller records it inside the card's
    assessment handling, and a formatting error there must not block the card.
    """
    try:
        if not isinstance(assessment, Mapping):
            return DECISION_UNAVAILABLE
        deps = assessment.get("dependencies") or []
        if isinstance(deps, list) and deps:
            numbers = ", ".join(f"#{d}" for d in deps)
            return f"assessed: blocked by {numbers}"
        if assessment.get("sufficient", True):
            return "assessed: ready"
        count = len(assessment.get("questions") or [])
        return f"assessed: needs input ({count} question{'' if count == 1 else 's'})"
    except Exception:
        return DECISION_UNAVAILABLE


def _bound_symphony() -> str:
    """The symphony the calling task is bound to, or empty when none is bound."""
    bound = get_current_symphony()
    return "" if bound == UNBOUND_SYMPHONY else bound


def _bounded(text: str) -> tuple[str, bool]:
    if len(text) <= PROMPT_LIMIT_CHARS:
        return text, False
    marker = f"\n[prompt truncated: {len(text)} characters, first {PROMPT_LIMIT_CHARS} kept]"
    return text[:PROMPT_LIMIT_CHARS] + marker, True


def _header(record: CallRecord) -> dict[str, Any]:
    request = record.request
    prompt, truncated = _bounded(request.prompt if request is not None else "")
    reply = record.reply
    usage_note: str | None = None
    if reply is not None and reply.usage is None:
        usage_note = reply.note or NO_USAGE_REASON
    return {
        "type": RUN_HEADER_TYPE,
        "seq": 0,
        "header_version": RUN_HEADER_VERSION,
        "ts": record.started_at.isoformat(),
        "run_id": record.call_id,
        "prompt": {
            "rendered": prompt,
            "system": request.system if request is not None else "",
            "truncated": truncated,
            "reason": None,
        },
        "performer": {
            "id": None,
            "backend": _HEADER_BACKEND,
            "transport": _HEADER_TRANSPORT,
            "endpoint": request.endpoint if request is not None else None,
        },
        "model": {"model": request.model if request is not None else None},
        "card": {"id": record.card_id, "issue_number": None, "title": None},
        "conductor": {
            "kind": record.kind,
            "symphony": record.symphony or None,
            "card_id": record.card_id,
            "endpoint": request.endpoint if request is not None else None,
            "parameters": dict(request.parameters) if request is not None else {},
            "decision": record.decision,
            "usage_note": usage_note,
        },
    }


def _records(record: CallRecord) -> list[dict[str, Any]]:
    """The records after the header, numbered from 1, in the order a reader expects."""
    ts = datetime.now(UTC).isoformat()
    rows: list[dict[str, Any]] = []
    reply = record.reply
    if reply is not None:
        if reply.text:
            rows.append({"type": "assistant_text", "text": reply.text})
        rows.extend(
            {"type": _TOOL_TYPE, "tool_name": tool.name, "call_id": tool.call_id, "input": tool.input}
            for tool in reply.tool_calls
        )
        if reply.usage is not None:
            usage: dict[str, Any] = {**dict(reply.usage), "model": reply.model}
            rows.append({"type": "usage", "scope": "message", "usage": usage})
    if record.error is not None:
        rows.append({"type": "error", "text": record.error, "input": {"timed_out": record.timed_out}})
    rows.append({
        "type": "result",
        "is_error": record.error is not None,
        "text": record.decision or NO_DECISION_TEXT,
    })
    return [{"ts": ts, "seq": number, **row} for number, row in enumerate(rows, start=1)]


def split_messages(messages: list[dict[str, str]]) -> tuple[str, str]:
    """(system, user) text of a chat message list. Used for the header's prompt fields."""
    system = "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    user = "\n\n".join(m.get("content", "") for m in messages if m.get("role") != "system")
    return system, user

