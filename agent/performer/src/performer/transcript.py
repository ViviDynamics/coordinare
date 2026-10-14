"""Full-fidelity run transcript records (spec 129).

Every backend records what its model and tools actually produced into an
in-memory, seq-numbered buffer owned by the job runner. The daemon pulls the
buffer incrementally over ``GET /jobs/{id}?transcript_after=N`` and writes the
records to disk, so the transcript outlives the performer pod.

This module is the performer-side half of the record schema. The daemon owns
the mirror in ``coordinare.services.run_transcript`` and the file. The two
packages do not import each other; the field names are the contract.

Design rules:

* Text is kept whole up to ``FIELD_LIMIT_CHARS`` per string field. A longer field
  is cut with a marker, and a record that is still larger than
  ``RECORD_LIMIT_BYTES`` has its input and output withheld. So one record cannot
  approach the per-run cap, the client timeout, or double the heap. The
  200-character ``BackendEvent.text`` summary is a separate channel and is never a
  transcript source.
* Redaction runs when a batch is served, not when a record is stored, with the
  shared rules in ``performer.transcript_redaction``. Stored records are raw, and
  nothing leaves the performer unredacted. The daemon redacts again at write time.
* A record that cannot be captured or redacted keeps its seq and is replaced by a
  small ``error`` record saying so. It is never dropped silently and never raised.
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
import time
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

# Wall-clock budget for one ``after()`` call. A record that is slow to redact ends the
# batch early, so one large record cannot hold up completion detection (review round 4).
TIME_BUDGET_SECONDS = 1.0

# Size bounds on one record (review item 9). A string field longer than the field
# limit is cut with a marker. A record still over the record limit has its input
# and output withheld.
FIELD_LIMIT_CHARS = 256 * 1024
RECORD_LIMIT_BYTES = 1024 * 1024

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


_WITHHELD_PAYLOAD: dict[str, Any] = {"withheld": "value exceeded the per-record size limit"}


def _bounded_json(value: Any) -> Any:
    """A plain-JSON copy of ``value``, or the withheld marker when it is too large.

    The size is checked on the encoded form before any walk over the structure, so a
    huge value costs one encode and no recursive pass (review round 4, item 1).
    """
    if value is None:
        return None
    encoded = json.dumps(value, default=str)
    if len(encoded) > RECORD_LIMIT_BYTES:
        return dict(_WITHHELD_PAYLOAD)
    return _bounded(json.loads(encoded))


def _bounded(value: Any) -> Any:
    """Cut every string longer than ``FIELD_LIMIT_CHARS``, keeping a marker."""
    if isinstance(value, str):
        if len(value) <= FIELD_LIMIT_CHARS:
            return value
        return (
            value[:FIELD_LIMIT_CHARS]
            + f"\n[transcript field truncated: {len(value)} characters, "
            + f"first {FIELD_LIMIT_CHARS} kept]"
        )
    if isinstance(value, dict):
        return {key: _bounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_bounded(item) for item in value]
    return value


def _line_bytes(record: TranscriptRecord) -> int:
    return len(record.model_dump_json(exclude_none=True).encode()) + 1


def _withheld(run_id: str, seq: int, reason: str) -> TranscriptRecord:
    """A small ``error`` record that keeps a seq whose content could not be captured."""
    return TranscriptRecord(
        ts=datetime.now(UTC).isoformat(),
        run_id=run_id,
        seq=seq,
        type="error",
        text=f"transcript record {seq} withheld: {reason}",
    )


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
        # redaction of job secrets comes from the provider on every batch, so a
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

    def _build(
        self,
        seq: int,
        record_type: TranscriptType,
        fields: RecordFields,
    ) -> TranscriptRecord:
        record = TranscriptRecord(
            ts=datetime.now(UTC).isoformat(),
            run_id=self._run_id,
            seq=seq,
            type=record_type,
            text=_bounded(fields.get("text")),
            tool_name=_bounded(fields.get("tool_name")),
            call_id=_bounded(fields.get("call_id")),
            input=_bounded_json(fields.get("input_data")),
            output=_bounded_json(fields.get("output_data")),
            is_error=fields.get("is_error"),
            usage=fields.get("usage"),
            scope=fields.get("scope"),
        )
        if _line_bytes(record) > RECORD_LIMIT_BYTES:
            # Input and output are the only fields that can be large in bulk. They
            # are withheld, and the record keeps its type, text and seq.
            withheld = {"withheld": "record exceeded the per-record size limit"}
            record = record.model_copy(update={"input": withheld, "output": withheld})
        return record

    def append(self, record_type: TranscriptType, **fields: Unpack[RecordFields]) -> int | None:
        """Store one record. Returns its seq, or None when the byte cap dropped it (211).

        A withheld record is still stored under its seq, so it returns that seq.
        """
        # Stored raw. Redaction happens in ``after()``, on the worker thread that
        # serves the batch, so the event loop that runs the backends never does
        # regex work over a large tool output (129 review, item 2).
        seq = self.last_seq + 1
        try:
            record = self._build(seq, record_type, fields)
            size = _line_bytes(record)
        except Exception as exc:  # per-record guard: keep the seq, withhold the content
            _logger.warning("transcript.record_withheld", seq=seq, exc_type=type(exc).__name__)
            record = _withheld(self._run_id, seq, f"could not be captured ({type(exc).__name__})")
            size = _line_bytes(record)
        if self._truncated or self._bytes + size > self._max_bytes:
            self._drop(record, size)
            return None
        self._records.append(record)
        self._bytes += size
        return seq

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

    def after(
        self,
        seq: int,
        limit: int,
        *,
        time_budget_s: float = TIME_BUDGET_SECONDS,
    ) -> list[dict[str, Any]]:
        """Records with ``seq > seq`` in ascending order, redacted for the wire.

        Returns at most ``limit`` records and at most ``max_batch_bytes`` of
        serialized output. The first record is always returned, so a single
        large record cannot stall delivery. A record that cannot be redacted is
        served as a withheld ``error`` record with the same seq (review item 1).
        """
        started = time.monotonic()
        start = max(seq, 0)
        secrets = self._secret_values()
        batch: list[dict[str, Any]] = []
        total = 0
        for record in self._records[start : start + limit]:
            if batch and time.monotonic() - started > time_budget_s:
                break
            data, size = _serve(record, secrets)
            if batch and total + size > self._max_batch_bytes:
                break
            batch.append(data)
            total += size
        return batch


def _serve(record: TranscriptRecord, secrets: list[str]) -> tuple[dict[str, Any], int]:
    """One record as it leaves the performer, with its serialized size.

    Per-record guard: a record that cannot be redacted is replaced by a withheld
    ``error`` record that keeps the seq, so a batch never fails because of one.
    """
    try:
        data = _redacted_dump(record, secrets)
        size = len(json.dumps(data, ensure_ascii=True).encode()) + 1
    except Exception as exc:  # per-record guard (review item 1)
        _logger.warning("transcript.record_withheld", seq=record.seq, exc_type=type(exc).__name__)
        data = _withheld(record.run_id, record.seq, "could not be redacted").model_dump(
            exclude_none=True,
        )
        size = len(json.dumps(data, ensure_ascii=True).encode()) + 1
    return data, size


def _redacted_dump(record: TranscriptRecord, secrets: list[str]) -> dict[str, Any]:
    """The record as it leaves the performer: every free-form field redacted."""
    data = record.model_dump(exclude_none=True)
    for key in ("text", "tool_name", "call_id"):
        if isinstance(data.get(key), str):
            data[key] = redact_text(data[key], secrets)
    for key in ("input", "output"):
        if key in data:
            data[key] = redact_value(data[key], secrets)
    return data


_transcript_var: contextvars.ContextVar[TranscriptBuffer | None] = contextvars.ContextVar(
    "_transcript",
    default=None,
)


def set_active_buffer(
    buffer: TranscriptBuffer | None,
) -> contextvars.Token[TranscriptBuffer | None]:
    """Install ``buffer`` for the current context. Called by the job runner."""
    return _transcript_var.set(buffer)


def transcript_enabled() -> bool:
    """True when a buffer is active for the current job. Backends may use this
    to skip building large payloads when transcripts are off."""
    return _transcript_var.get() is not None


@best_effort
def emit(record_type: TranscriptType, **fields: Unpack[RecordFields]) -> int | None:
    """Append one record to the active job's transcript. No-op when disabled.

    Keyword fields: text, tool_name, call_id, input_data, output_data, is_error,
    usage, scope. Never raises: a capture failure is logged and dropped.

    Returns the record's seq (211), so the backend can attach it to the activity event
    it built from the same step. None when no buffer is active, when the byte cap
    dropped the record, or when capture failed.
    """
    buffer = _transcript_var.get()
    if buffer is None:
        return None
    return buffer.append(record_type, **fields)


__all__ = [
    "FIELD_LIMIT_CHARS",
    "MAX_BATCH_BYTES",
    "RECORD_LIMIT_BYTES",
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
