"""138: bounded in-memory activity log behind the dashboard's activity feed.

Owns retention (FR-019 through FR-021), ingest-time truncation (FR-035 through
FR-037), per-card duplicate suppression (FR-022 through FR-024), and the live
sink fan-out to the SSE transport (FR-001, FR-004).

Nothing here is persisted — the log is per-process and discarded on restart
(FR-018). Single-process, single-threaded under asyncio, so no lock is needed.
"""
from __future__ import annotations

import contextlib
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

MAX_ENTRIES = 2000
MAX_TEXT = 200
MAX_TITLE = 80
MAX_SEEN_PER_CARD = 256


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
        }


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
    ) -> None:
        self._entries: deque[ActivityEntry] = deque(maxlen=maxlen)
        self._max_text = max_text
        self._max_seen = max_seen_per_card
        self._seen: dict[str, set[str]] = {}
        self._seen_order: dict[str, deque[str]] = {}
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
    ) -> ActivityEntry | None:
        """Append one entry. Returns None when suppressed as a duplicate."""
        entry = self._append(
            activity_type=activity_type,
            card_id=card_id,
            card_number=card_number,
            card_title=card_title,
            stage=stage,
            text=text,
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

    def _append(
        self,
        *,
        activity_type: str,
        card_id: Any,
        card_number: Any,
        card_title: Any,
        stage: Any,
        text: Any,
    ) -> ActivityEntry | None:
        # Truncate BEFORE building the dedup key, or key size is unbounded and
        # dedup diverges between long and short lines (G2).
        clipped_text, truncated = _clip(text, self._max_text)
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
        key = f"{kind}|{cid}|{stage_str}|{clipped_text}"
        seen = self._seen.setdefault(cid, set())
        if key in seen:
            return None
        order = self._seen_order.setdefault(cid, deque(maxlen=self._max_seen))
        if len(order) == order.maxlen:
            seen.discard(order[0])
        order.append(key)
        seen.add(key)

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
