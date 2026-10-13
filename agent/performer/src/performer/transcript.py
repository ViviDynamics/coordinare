"""Full-fidelity run transcript records (spec 129).

Every backend records what its model and tools actually produced into an
in-memory, seq-numbered buffer owned by the job runner. The daemon pulls the
buffer incrementally over ``GET /jobs/{id}?transcript_after=N`` and writes the
records to disk, so the transcript outlives the performer pod.

This module is the performer-side half of the record schema. The daemon owns
the mirror in ``coordinare.services.run_transcript`` and the file. The two
packages do not import each other; the field names are the contract.

Design rules:

* Nothing is truncated here. ``text``, ``input`` and ``output`` carry the full
  value. The 200-character ``BackendEvent.text`` summary is a separate channel
  and must never be used as a transcript source.
* Redaction runs before a record is stored (``_redact_secrets`` on every
  string, sensitive keys masked). The daemon redacts again at write time.
* When no buffer is active (transcripts disabled, or a unit test that never
  starts a job) :func:`emit` is a no-op. Disabled runs pay one ContextVar read.
* The buffer enforces ``max_bytes``. The first record that would exceed the cap
  is replaced by exactly one ``truncated`` marker; later content records are
  counted and dropped.
"""
from __future__ import annotations

import contextvars
import json
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict, Unpack, get_args

from pydantic import BaseModel, ConfigDict

from performer.models import _redact_secrets

TranscriptType = Literal[
    "system",
    "user",
    "assistant_text",
    "thinking",
    "tool_call",
    "tool_result",
    "usage",
    "result",
    "error",
    "capability",
    "truncated",
]

TRANSCRIPT_TYPES: frozenset[str] = frozenset(get_args(TranscriptType))

# Same key list as coordinare.lib.redaction.SENSITIVE_KEYS. Values under these
# keys are masked wherever they appear in a record's input or output.
_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "token",
    "password",
    "secret",
    "secrets",
    "api_key",
    "webhook_url",
    "authorization",
    "auth_token",
})
_MASK = "***REDACTED***"


class TranscriptUsage(BaseModel):
    """Token accounting for one model turn (or a cumulative total, see ``text``)."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    model: str | None = None


class RecordFields(TypedDict, total=False):
    """The optional payload of one record, passed as keyword arguments to emit()."""

    text: str | None
    tool_name: str | None
    call_id: str | None
    input_data: Any
    output_data: Any
    is_error: bool | None
    usage: TranscriptUsage | None


class TranscriptRecord(BaseModel):
    """One transcript line. Field names are the wire and file contract."""

    model_config = ConfigDict(extra="ignore")

    ts: str
    run_id: str = ""
    seq: int
    type: TranscriptType
    text: str | None = None
    tool_name: str | None = None
    # Correlates a tool_call with its tool_result. Extra to the issue's field
    # list, needed because parallel tool calls can finish out of order.
    call_id: str | None = None
    input: Any = None
    output: Any = None
    is_error: bool | None = None
    usage: TranscriptUsage | None = None


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_secrets(value)
    if isinstance(value, dict):
        return {
            str(k): (_MASK if str(k).lower() in _SENSITIVE_KEYS else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    return value


def _jsonable(value: Any) -> Any:
    """Round-trip through JSON so the stored record is plain data (no objects)."""
    if value is None:
        return None
    return json.loads(json.dumps(value, default=str))


class TranscriptBuffer:
    """Seq-numbered, byte-capped, in-memory record store for one job.

    Seq numbers are 1-based and contiguous, so record ``seq`` lives at index
    ``seq - 1``. The daemon's cursor is the highest seq it has written.
    """

    def __init__(self, run_id: str, *, max_bytes: int) -> None:
        self._run_id = run_id
        self._max_bytes = max_bytes
        self._records: list[TranscriptRecord] = []
        self._bytes = 0
        self._truncated = False
        self._dropped_records = 0
        self._dropped_bytes = 0

    @property
    def last_seq(self) -> int:
        return len(self._records)

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def dropped_records(self) -> int:
        return self._dropped_records

    def append(self, record_type: TranscriptType, **fields: Unpack[RecordFields]) -> None:
        seq = self.last_seq + 1
        text = fields.get("text")
        record = TranscriptRecord(
            ts=datetime.now(UTC).isoformat(),
            run_id=self._run_id,
            seq=seq,
            type=record_type,
            text=_redact_value(text) if text is not None else None,
            tool_name=fields.get("tool_name"),
            call_id=fields.get("call_id"),
            input=_redact_value(_jsonable(fields.get("input_data"))),
            output=_redact_value(_jsonable(fields.get("output_data"))),
            is_error=fields.get("is_error"),
            usage=fields.get("usage"),
        )
        size = _line_bytes(record)
        if self._truncated or self._bytes + size > self._max_bytes:
            self._drop(record, size)
            return
        self._records.append(record)
        self._bytes += size

    def _drop(self, record: TranscriptRecord, size: int) -> None:
        if not self._truncated:
            # The first over-cap record becomes the single truncated marker.
            # It is appended even though it may slightly exceed the cap, so the
            # reader always sees why content stops.
            self._truncated = True
            marker = TranscriptRecord(
                ts=record.ts,
                run_id=self._run_id,
                seq=record.seq,
                type="truncated",
                text=f"transcript byte cap reached ({self._max_bytes} bytes); "
                "later content records were dropped",
                input={"max_bytes": self._max_bytes, "written_bytes": self._bytes},
            )
            self._records.append(marker)
            self._bytes += _line_bytes(marker)
            return
        self._dropped_records += 1
        self._dropped_bytes += size

    def after(self, seq: int, limit: int) -> list[dict[str, Any]]:
        """Records with ``seq > seq`` in ascending order, at most ``limit`` of them."""
        start = max(seq, 0)
        return [
            r.model_dump(exclude_none=True) for r in self._records[start:start + limit]
        ]


def _line_bytes(record: TranscriptRecord) -> int:
    return len(record.model_dump_json(exclude_none=True).encode()) + 1


_transcript_var: contextvars.ContextVar[TranscriptBuffer | None] = contextvars.ContextVar(
    "_transcript", default=None,
)


def set_active_buffer(buffer: TranscriptBuffer | None) -> contextvars.Token[TranscriptBuffer | None]:
    """Install ``buffer`` for the current context. Called by the job runner."""
    return _transcript_var.set(buffer)


def transcript_enabled() -> bool:
    """True when a buffer is active for the current job. Backends may use this
    to skip building large payloads when transcripts are off."""
    return _transcript_var.get() is not None


def emit(record_type: TranscriptType, **fields: Unpack[RecordFields]) -> None:
    """Append one record to the active job's transcript. No-op when disabled.

    Keyword fields: text, tool_name, call_id, input_data, output_data, is_error, usage.
    """
    buffer = _transcript_var.get()
    if buffer is None:
        return
    buffer.append(record_type, **fields)


__all__ = [
    "TRANSCRIPT_TYPES",
    "TranscriptBuffer",
    "TranscriptRecord",
    "TranscriptType",
    "TranscriptUsage",
    "emit",
    "set_active_buffer",
    "transcript_enabled",
]
