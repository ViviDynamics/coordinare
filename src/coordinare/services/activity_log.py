"""138: bounded in-memory activity log behind the dashboard's activity feed.

Owns retention (FR-019 through FR-021), ingest-time truncation (FR-035 through
FR-037), per-card duplicate suppression (FR-022 through FR-024), and the live
sink fan-out to the SSE transport (FR-001, FR-004).

Nothing here is persisted — the log is per-process and discarded on restart
(FR-018). Single-process, single-threaded under asyncio, so no lock is needed.
"""
from __future__ import annotations

import contextlib
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog

from coordinare.lib.redaction import redact_command_text

logger = structlog.get_logger(__name__)

MAX_ENTRIES = 2000
MAX_TEXT = 200
MAX_TITLE = 80
# 148: a tool call's one-line summary, e.g. "Bash · uv run pytest -q". The
# performer clips its argument to the same figure (backends/_tool_summary.py).
MAX_SUMMARY = 160
MAX_EVENT_AT = 40
# 148: the only tool outcomes the feed distinguishes.
TOOL_STATUSES = frozenset({"ok", "error"})
MAX_SEEN_PER_CARD = 256
# 327: the most delta entries any ONE stream may occupy in the shared log.
# A degenerate model loop emits ~10 deltas/second; without a per-stream cap a
# single looping message fills all MAX_ENTRIES slots within minutes and evicts
# every other card's activity from the feed. The dashboard already concatenates
# consecutive same-stream deltas into one displayed block, so the cap costs no
# information that was actually being rendered.
MAX_STREAM_ENTRIES = 200
# How many stream budgets to remember. Bounded so the counters cannot become
# their own leak on a long-running daemon.
MAX_TRACKED_STREAMS = 128
# 135: a stream's hidden count goes to the feed at a power of two, or once a second. Between
# sends the stored count stays exact, and the keepalive flush sends the last one still pending.
RESEND_INTERVAL_SECONDS = 1.0
# 211: the transcript record a feed entry was built from. The browser reads integers above
# 2**53 - 1 inexactly, so a larger value is not a seq it can name, and it is dropped.
MIN_TRANSCRIPT_SEQ = 1
MAX_TRANSCRIPT_SEQ = 2**53 - 1


def transcript_seq_or_none(value: object) -> int | None:
    """211: the value as a transcript seq when it is one, else None. A bool is never a seq."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if MIN_TRANSCRIPT_SEQ <= value <= MAX_TRANSCRIPT_SEQ:
        return value
    return None


@dataclass(frozen=True, slots=True)
class EventLink:
    """211: where a feed entry opens in its run. The transcript record is exact; the time is the fallback."""

    event_at: str = ""
    transcript_seq: int | None = None


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    """One immutable record of one thing that happened to one card."""

    seq: int
    timestamp: datetime
    card_id: str
    card_number: int | None
    card_title: str
    stage: str
    activity_type: str
    text: str
    truncated: bool
    session_id: str = ""
    performer_id: str = ""
    is_delta: bool = False
    stream_id: str = ""
    summary: str = ""
    status: str = ""
    # 133: the performer's own time for the event (ISO 8601), when the event has one. A
    # transcript record is stamped on the performer clock, so this is the time to open a run at.
    event_at: str = ""
    # 211: the transcript record this event was built from. Set, the row opens the run at that
    # exact record. None (an older performer, or no record) falls back to event_at.
    transcript_seq: int | None = None
    # 135: what each cut removed, so the feed can say how much is hidden. None means the count
    # was not recorded (an entry from before 135), and the feed shows nothing rather than a guess.
    # The lengths are the original sizes before the clip. hidden_updates is set on a stream's
    # truncation marker only: the updates past the stream budget. log_dropped is how many
    # entries the bounded log had dropped when this entry was recorded.
    text_length: int | None = None
    summary_length: int | None = None
    hidden_updates: int | None = None
    log_dropped: int | None = None
    # 135: the bound the log holds, so the feed states its own limit rather than guessing one.
    log_capacity: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp.isoformat(),
            "card_id": self.card_id,
            "card_number": self.card_number,
            "card_title": self.card_title,
            "stage": self.stage,
            "activity_type": self.activity_type,
            "text": self.text,
            "truncated": self.truncated,
            "session_id": self.session_id,
            "performer_id": self.performer_id,
            "is_delta": self.is_delta,
            "stream_id": self.stream_id,
            "summary": self.summary,
            "status": self.status,
            "event_at": self.event_at,
            "transcript_seq": self.transcript_seq,
            "text_length": self.text_length,
            "summary_length": self.summary_length,
            "hidden_updates": self.hidden_updates,
            "log_dropped": self.log_dropped,
            "log_capacity": self.log_capacity,
        }


@dataclass(slots=True)
class _StreamMarker:
    """135: one stream's truncation marker, and when its count last went out to the feed."""

    seq: int
    sent_at: float
    pending: bool = False


def _clip(value: Any, limit: int) -> tuple[str, bool]:
    """Coerce to str and truncate, preserving the prefix (FR-037).

    Never raises — a malformed event must not break the feed (G10).
    """
    if value is None:
        return "", False
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class ActivityLog:
    """Bounded, deduplicated activity history with an optional live sink."""

    def __init__(
        self,
        *,
        maxlen: int = MAX_ENTRIES,
        max_text: int = MAX_TEXT,
        max_seen_per_card: int = MAX_SEEN_PER_CARD,
        max_stream_entries: int = MAX_STREAM_ENTRIES,
        max_tracked_streams: int = MAX_TRACKED_STREAMS,
    ) -> None:
        # Sequence numbers restart with this in-memory history. The browser
        # needs its identity to distinguish a new producer from replay.
        self.run_id = uuid4().hex
        self._entries: deque[ActivityEntry] = deque(maxlen=maxlen)
        self._max_text = max_text
        self._max_seen = max_seen_per_card
        self._max_stream_entries = max_stream_entries
        self._max_tracked_streams = max_tracked_streams
        self._seen: dict[str, set[str]] = {}
        self._seen_order: dict[str, deque[str]] = {}
        # 327: per-stream delta budgets. Insertion-ordered; oldest evicted.
        self._stream_counts: dict[str, int] = {}
        # 135: each tracked stream's truncation marker. Evicted together with the stream's budget.
        self._stream_markers: dict[str, _StreamMarker] = {}
        # 135: the time source for the re-send schedule. A test replaces it with a clock it moves by hand.
        self._clock: Callable[[], float] = time.monotonic
        # 135: entries pushed out of the bounded deque, for the feed's "dropped" line.
        self._dropped = 0
        self._next_seq = 0
        # Live fan-out, set once by DashboardStore to broadcaster.broadcast_activity.
        # None everywhere else (tests, non-dashboard embeddings).
        self.sink: Callable[[list[ActivityEntry]], None] | None = None

    def record(
        self,
        *,
        activity_type: str,
        card_id: str = "",
        card_number: int | None = None,
        card_title: str = "",
        stage: str = "",
        text: str = "",
        session_id: str = "",
        performer_id: str = "",
        is_delta: bool = False,
        stream_id: str = "",
        source_event_id: str = "",
        summary: str = "",
        status: str = "",
        link: EventLink | None = None,
    ) -> ActivityEntry | None:
        """Append one entry. Returns None when suppressed as a duplicate."""
        entry = self._append(
            activity_type=activity_type,
            card_id=card_id,
            card_number=card_number,
            card_title=card_title,
            stage=stage,
            text=text,
            session_id=session_id,
            performer_id=performer_id,
            is_delta=is_delta, stream_id=stream_id, source_event_id=source_event_id,
            summary=summary, status=status, link=link,
        )
        if entry is not None:
            self._fan_out([entry])
        return entry

    def record_many(self, items: Iterable[Mapping[str, Any]]) -> list[ActivityEntry]:
        """Batch form. Returns only the entries actually appended."""
        appended: list[ActivityEntry] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            entry = self._append(
                activity_type=str(item.get("activity_type", "")),
                card_id=item.get("card_id", ""),
                card_number=item.get("card_number"),
                card_title=item.get("card_title", ""),
                stage=item.get("stage", ""),
                text=item.get("text", ""),
                session_id=item.get("session_id", ""),
                performer_id=item.get("performer_id", ""),
                is_delta=item.get("is_delta") is True,
                stream_id=item.get("stream_id", ""),
                source_event_id=item.get("source_event_id", ""),
                summary=item.get("summary", ""),
                status=item.get("status", ""),
                link=EventLink(
                    event_at=str(item.get("event_at") or ""),
                    transcript_seq=item.get("transcript_seq"),
                ),
            )
            if entry is not None:
                appended.append(entry)
        if appended:
            self._fan_out(appended)
        return appended

    def snapshot(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Oldest-first serialised entries — the wire order (FR-003).

        Newest-first is a display rule the client applies; the server never
        reverses. ``limit`` returns the newest N, still oldest-first.
        """
        entries = list(self._entries)
        if limit is not None and limit >= 0:
            entries = entries[-limit:] if limit else []
        return [e.to_dict() for e in entries]

    def forget_card(self, card_id: str) -> None:
        """Release a departed card's dedup bookkeeping. Entries are retained."""
        self._seen.pop(card_id, None)
        self._seen_order.pop(card_id, None)

    # -- internals ---------------------------------------------------------

    def _count_stream_update(self, stream: str) -> int:
        """327: count one update against its stream's budget, evicting the oldest tracked stream if full.

        Returns how many of the stream's updates are past the budget, this one included. It is
        one or less while the stream is still within its budget.
        """
        used = self._stream_counts.pop(stream, 0)
        self._stream_counts[stream] = used + 1  # re-insert: most recent last
        while len(self._stream_counts) > self._max_tracked_streams:
            # Evict the oldest stream that has NOT yet hit its cap. Dropping
            # a capped stream would hand it a fresh budget the moment it is
            # seen again, so one looping message could flood the log twice
            # over. The dict stays bounded either way: when every tracked
            # stream is capped, the oldest of those goes.
            victim = next(
                (
                    sid_
                    for sid_, count in self._stream_counts.items()
                    if sid_ != stream and count <= self._max_stream_entries
                ),
                None,
            )
            if victim is None:
                victim = next(sid_ for sid_ in self._stream_counts if sid_ != stream)
            self._stream_counts.pop(victim)
            self._stream_markers.pop(victim, None)
        return used + 1 - self._max_stream_entries

    def _count_hidden_update(self, stream: str, hidden: int) -> None:
        """135: store how many of a stream's updates are hidden, and re-send it on a schedule.

        The stored count is exact on every update. It goes to the feed at a power of two, so a
        doubling is never missed, or once a second has passed since the last send. Anything
        else stays pending until the next send or flush_pending_markers(). The re-sent copy
        keeps the marker's seq, so a connected feed redraws it where it already sits.
        """
        marker = self._stream_markers.get(stream)
        if marker is None:
            return
        index = self._index_of(marker.seq)
        if index is None:
            return
        self._entries[index] = replace(self._entries[index], hidden_updates=hidden)
        if hidden & (hidden - 1) == 0 or self._clock() - marker.sent_at >= RESEND_INTERVAL_SECONDS:
            self._send_marker(marker, index)
        else:
            marker.pending = True

    def flush_pending_markers(self) -> None:
        """135: send every stream count still pending, so a stream that went quiet ends on its last count.

        The SSE keepalive calls this. Once a count is sent it is no longer pending, so
        several clients each calling it send each count once.
        """
        for marker in list(self._stream_markers.values()):
            if not marker.pending:
                continue
            index = self._index_of(marker.seq)
            if index is not None:
                self._send_marker(marker, index)
            marker.pending = False

    def _send_marker(self, marker: _StreamMarker, index: int) -> None:
        marker.sent_at = self._clock()
        marker.pending = False
        self._fan_out([self._entries[index]])

    def _index_of(self, seq: int) -> int | None:
        """The deque index of an entry still in the log. Seqs in the deque are contiguous, so this is O(1)."""
        if not self._entries:
            return None
        index = seq - self._entries[0].seq
        if 0 <= index < len(self._entries) and self._entries[index].seq == seq:
            return index
        return None

    def _append(
        self,
        *,
        activity_type: str,
        card_id: Any,
        card_number: Any,
        card_title: Any,
        stage: Any,
        text: Any,
        session_id: Any = "",
        performer_id: Any = "",
        is_delta: bool = False,
        stream_id: Any = "",
        source_event_id: Any = "",
        summary: Any = "",
        status: Any = "",
        link: EventLink | None = None,
    ) -> ActivityEntry | None:
        # Truncate BEFORE building the dedup key, or key size is unbounded and
        # dedup diverges between long and short lines (G2).
        raw_text = redact_command_text(str(text or ""))
        clipped_text, truncated = _clip(raw_text, self._max_text)
        # 148: a tool's argument is a command or a path, so it is redacted in the
        # daemon, before the entry, the dedup key or the snapshot can hold it.
        raw_summary = redact_command_text(str(summary or ""))
        clipped_summary, _ = _clip(raw_summary, MAX_SUMMARY)
        # 135: the hidden update count, set only when this append is a stream's marker.
        hidden_updates: int | None = None
        # 133: a timestamp is a short ISO string; anything longer is not one and is dropped.
        link = link or EventLink()
        clipped_event_at, _ = _clip(str(link.event_at or ""), MAX_EVENT_AT)
        status_str, _ = _clip(status, 16)
        if status_str not in TOOL_STATUSES:
            status_str = ""
        sid, _ = _clip(session_id, 200)
        pid, _ = _clip(performer_id, 200)
        stream, _ = _clip(stream_id, 200)
        source_id, _ = _clip(source_event_id, 200)
        clipped_title, _ = _clip(card_title, MAX_TITLE)
        cid, _ = _clip(card_id, 200)
        stage_str, _ = _clip(stage, 80)
        kind, _ = _clip(activity_type, 40)

        try:
            number = int(card_number) if card_number is not None else None
        except (TypeError, ValueError):
            number = None

        # `timestamp` and `seq` are deliberately NOT in the key: a re-reported
        # event arrives with a new observation time, so including either would
        # disable suppression entirely and make a wedged agent scroll (FR-022).
        key = f"{kind}|{cid}|{stage_str}|{sid}|{pid}|{clipped_text}"
        if clipped_summary or status_str:
            key += f"|{clipped_summary}|{status_str}"
        if source_id and (is_delta or kind in ("tool_use", "tool_result")):
            # Backend identity is stable on replay. Distinct executions of the
            # same tool, like repeated streaming words, are real new activity.
            key += f"|{stream}|{source_id}"
        seen = self._seen.setdefault(cid, set())
        if key in seen:
            return None
        order = self._seen_order.setdefault(cid, deque(maxlen=self._max_seen))
        if len(order) == order.maxlen:
            seen.discard(order[0])
        order.append(key)
        seen.add(key)

        # 327: bound how much of the shared log one delta stream may occupy, so
        # a looping model cannot evict every other card's activity. The stream
        # keeps its budget under LRU so an ACTIVE stream is never the one
        # dropped from the bookkeeping. 135: only a kept update is charged. A repeat
        # was suppressed above, and nothing about it is hidden.
        if is_delta and stream:
            hidden = self._count_stream_update(stream)
            if hidden > 1:
                self._count_hidden_update(stream, hidden)
                return None  # already announced below; drop the rest quietly
            if hidden == 1:
                # Say it once, rather than silently swallowing the output.
                kind = "stream_truncated"
                raw_text = (
                    f"stream {stream} exceeded {self._max_stream_entries} updates "
                    "(looping or unusually long); further deltas are truncated"
                )
                clipped_text, truncated = _clip(raw_text, self._max_text)
                is_delta = False
                hidden_updates = hidden
                self._stream_markers[stream] = _StreamMarker(seq=self._next_seq, sent_at=self._clock())
        if len(self._entries) == self._entries.maxlen:
            self._dropped += 1
        entry = ActivityEntry(
            seq=self._next_seq,
            timestamp=datetime.now(UTC),
            card_id=cid,
            card_number=number,
            card_title=clipped_title,
            stage=stage_str,
            activity_type=kind,
            text=clipped_text,
            truncated=truncated,
            session_id=sid,
            performer_id=pid,
            is_delta=is_delta, stream_id=stream,
            summary=clipped_summary, status=status_str, event_at=clipped_event_at,
            # 211: validated here as well as at the caller. Not part of the dedup key: a re-reported
            # event that now names its record is still the same activity.
            transcript_seq=transcript_seq_or_none(link.transcript_seq),
            text_length=len(raw_text), summary_length=len(raw_summary),
            hidden_updates=hidden_updates, log_dropped=self._dropped,
            log_capacity=self._entries.maxlen,
        )
        self._next_seq += 1
        self._entries.append(entry)
        return entry

    def _fan_out(self, appended: list[ActivityEntry]) -> None:
        """Push to the live sink. A broken transport must not break recording."""
        sink = self.sink
        if sink is None:
            return
        with contextlib.suppress(Exception):
            sink(appended)
