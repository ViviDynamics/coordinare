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
* Redaction runs before a record is stored, with the shared rules in
  ``performer.transcript_redaction``: credential-named keys, ``NAME=value``
  lines, embedded JSON, token shapes, and the exact secret values the job was
  handed (``secrets`` provider). The daemon redacts again at write time.
* Capture is best effort. :func:`emit` and every backend helper decorated with
  :func:`best_effort` swallow and log their own failures, so transcript code can
  never break a backend's reader loop.
* When no buffer is active (transcripts disabled, or a unit test that never
  starts a job) :func:`emit` is a no-op. Disabled runs pay one ContextVar read.
* The buffer enforces ``max_bytes``. The first record that would exceed the cap
  is replaced by one ``truncated`` marker; later content records are counted and
  dropped. :meth:`TranscriptBuffer.after` returns at most ``MAX_BATCH_BYTES`` per
  delivery (always at least one record).
"""
from __future__ import annotations

import contextvars
import functools
import json
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict, TypeVar, Unpack, cast, get_args

import structlog
from pydantic import BaseModel, ConfigDict

from performer.transcript_redaction import redact_text, redact_value, secret_values_from_env

_logger = structlog.get_logger(__name__)

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

TranscriptScope = Literal["message", "run"]

TRANSCRIPT_TYPES: frozenset[str] = frozenset(get_args(TranscriptType))

# Upper bound on the serialized size of one delivery batch. A single record
# larger than this is still delivered alone, so the stream always makes progress.
MAX_BATCH_BYTES = 8 * 1024 * 1024

SecretProvider = Callable[[], Iterable[str]]

F = TypeVar("F", bound=Callable[..., Any])


class TranscriptUsage(BaseModel):
    """Token accounting for one model turn or one run total (see ``scope`` on the record)."""

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
    # "message": usage of one model message. "run": a run or cumulative total.
    # A consumer summing usage must count only one scope.
    scope: TranscriptScope | None


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
    scope: TranscriptScope | None = None


def _jsonable(value: Any) -> Any:
    """Round-trip through JSON so the stored record is plain data (no objects)."""
    if value is None:
        return None
    return json.loads(json.dumps(value, default=str))


def _line_bytes(record: TranscriptRecord) -> int:
    return len(record.model_dump_json(exclude_none=True).encode()) + 1


_failure_sites_logged: set[str] = set()


def best_effort(func: F) -> F:
    """Run a transcript helper so that its exceptions never reach the caller.

    The first failure per call site is logged as ``transcript.capture_failed``.
    Later failures at the same site are swallowed silently, so a broken helper
    cannot flood the log.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            site = func.__qualname__
            if site not in _failure_sites_logged:
                _failure_sites_logged.add(site)
                _logger.warning(
                    "transcript.capture_failed",
                    site=site,
                    exc_type=type(exc).__name__,
                )
            return None

    return cast(F, wrapper)


class TranscriptBuffer:
    """Seq-numbered, byte-capped, in-memory record store for one job.

    Seq numbers are 1-based and contiguous, so record ``seq`` lives at index
    ``seq - 1``. The daemon's cursor is the highest seq it has written.
    """

    def __init__(
        self,
        run_id: str,
        *,
        max_bytes: int,
        secrets: SecretProvider | None = None,
        max_batch_bytes: int = MAX_BATCH_BYTES,
    ) -> None:
        self._run_id = run_id
        self._max_bytes = max_bytes
        self._max_batch_bytes = max_batch_bytes
        self._secrets = secrets
        # Environment credentials are read once, at job start. Exact-value
        # redaction of job secrets comes from the provider on every record, so a
        # token refreshed mid-run is covered from its first use.
        self._env_secrets = secret_values_from_env(os.environ)
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

    def _secret_values(self) -> list[str]:
        values = list(self._env_secrets)
        if self._secrets is not None:
            values.extend(str(v) for v in self._secrets())
        return values

    def append(self, record_type: TranscriptType, **fields: Unpack[RecordFields]) -> None:
        seq = self.last_seq + 1
        secrets = self._secret_values()
        text = fields.get("text")
        tool_name = fields.get("tool_name")
        call_id = fields.get("call_id")
        record = TranscriptRecord(
            ts=datetime.now(UTC).isoformat(),
            run_id=self._run_id,
            seq=seq,
            type=record_type,
            text=redact_text(text, secrets) if text is not None else None,
            tool_name=redact_text(tool_name, secrets) if tool_name is not None else None,
            call_id=redact_text(call_id, secrets) if call_id is not None else None,
            input=redact_value(_jsonable(fields.get("input_data")), secrets),
            output=redact_value(_jsonable(fields.get("output_data")), secrets),
            is_error=fields.get("is_error"),
            usage=fields.get("usage"),
            scope=fields.get("scope"),
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
        """Records with ``seq > seq`` in ascending order.

        Returns at most ``limit`` records and at most ``max_batch_bytes`` of
        serialized output. The first record is always returned, so a single
        large record cannot stall delivery.
        """
        start = max(seq, 0)
        batch: list[dict[str, Any]] = []
        total = 0
        for record in self._records[start:start + limit]:
            size = _line_bytes(record)
            if batch and total + size > self._max_batch_bytes:
                break
            batch.append(record.model_dump(exclude_none=True))
            total += size
        return batch


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


@best_effort
def emit(record_type: TranscriptType, **fields: Unpack[RecordFields]) -> None:
    """Append one record to the active job's transcript. No-op when disabled.

    Keyword fields: text, tool_name, call_id, input_data, output_data, is_error,
    usage, scope. Never raises: a capture failure is logged and dropped.
    """
    buffer = _transcript_var.get()
    if buffer is None:
        return
    buffer.append(record_type, **fields)


__all__ = [
    "MAX_BATCH_BYTES",
    "TRANSCRIPT_TYPES",
    "TranscriptBuffer",
    "TranscriptRecord",
    "TranscriptScope",
    "TranscriptType",
    "TranscriptUsage",
    "best_effort",
    "emit",
    "set_active_buffer",
    "transcript_enabled",
]
