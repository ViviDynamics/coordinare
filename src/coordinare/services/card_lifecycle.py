"""147: the card lifecycle log behind the pipeline timeline.

A card's session is deleted when the card finishes, so the session cannot hold the history
of a Done card. This log is run-global, like ``attempt_log``: each lifecycle event is recorded
where it happens (dispatch, the stage verdict, the CI gate, PR artefacts, review intake, human
commands, overrides and the merge), keyed by symphony and card, and it outlives the session.

An event is a kind, a stage code, a UTC time and a few small detail values. Free text goes
through ``redact_state_value`` before it is kept. The log is bounded per symphony (the most
recently active cards) and per card (the newest events). It is written with the snapshot as
``card_lifecycle`` (schema v35). A snapshot from before v35 loads as an empty log, so an event
that was never recorded shows as not recorded, never as a guess.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from coordinare.lib.redaction import redact_state_value

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The lifecycle events the log keeps. Anything else is refused at record time.
EVENT_KINDS: frozenset[str] = frozenset({
    "dispatched",
    "feedback_relayed",
    "verdict",
    "ci_result",
    "pr_opened",
    "review_received",
    "human_action",
    "override",
    "merged",
})
MAX_CARDS_PER_SYMPHONY = 100
MAX_EVENTS_PER_CARD = 100
MAX_KEY_CHARS = 160
MAX_DETAIL_KEYS = 8
MAX_DETAIL_KEY_CHARS = 40
MAX_DETAIL_TEXT_CHARS = 200

EventRecord = dict[str, Any]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clean_value(value: object) -> object:
    """A detail value the log keeps: a short string, a number, a bool or None. Text is redacted."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    text = redact_state_value(str(value))
    return str(text)[:MAX_DETAIL_TEXT_CHARS]


def _clean_detail(detail: Mapping[str, object] | None) -> dict[str, object]:
    if not detail:
        return {}
    out: dict[str, object] = {}
    for name, value in detail.items():
        if len(out) >= MAX_DETAIL_KEYS:
            break
        out[str(name)[:MAX_DETAIL_KEY_CHARS]] = _clean_value(value)
    return out


def _valid_event(raw: object) -> EventRecord | None:
    """A loaded event, or None when it is not one the log could have written."""
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    kind = raw.get("kind")
    if not isinstance(at, str) or not isinstance(kind, str) or kind not in EVENT_KINDS:
        return None
    stage = raw.get("stage")
    event: EventRecord = {
        "at": at,
        "kind": kind,
        "stage": stage if isinstance(stage, str) and stage else None,
        "detail": _clean_detail(raw.get("detail") if isinstance(raw.get("detail"), dict) else None),
    }
    key = raw.get("key")
    if isinstance(key, str) and key:
        event["key"] = key[:MAX_KEY_CHARS]
    return event


class CardLifecycleLog:
    """Bounded, persistable lifecycle events per symphony and card. Single-threaded (asyncio)."""

    def __init__(self) -> None:
        self._cards: dict[str, dict[str, list[EventRecord]]] = {}

    def record(self, symphony: str, card_id: str, kind: str, **fields: Any) -> bool:
        """Append one event. False when it is refused or a same-key event of this kind exists.

        ``fields`` are the optional parts of the event: ``stage``, ``detail``, ``key`` (makes the
        event once-only per card: a check that runs every cycle records its result the first time
        and not again) and ``at`` (the UTC time, now when omitted). An unknown field is an error.
        Returns True when the event was kept.
        """
        if kind not in EVENT_KINDS or not card_id:
            return False
        stage = fields.get("stage")
        detail = fields.get("detail")
        key = fields.get("key")
        at = fields.get("at")
        unknown = set(fields) - {"stage", "detail", "key", "at"}
        if unknown:
            message = f"unknown lifecycle event field: {sorted(unknown)[0]}"
            raise TypeError(message)
        events = self._cards.setdefault(symphony, {}).setdefault(card_id, [])
        if key is not None and any(e["kind"] == kind and e.get("key") == str(key)[:MAX_KEY_CHARS] for e in events):
            return False
        event: EventRecord = {
            "at": str(at) if at else _now_iso(),
            "kind": kind,
            "stage": str(stage) if stage else None,
            "detail": _clean_detail(detail if isinstance(detail, dict) else None),
        }
        if key:
            event["key"] = str(key)[:MAX_KEY_CHARS]
        events.append(event)
        del events[:-MAX_EVENTS_PER_CARD]
        self._trim(symphony)
        return True

    def _trim(self, symphony: str) -> None:
        cards = self._cards.get(symphony, {})
        if len(cards) <= MAX_CARDS_PER_SYMPHONY:
            return
        newest = sorted(cards, key=lambda cid: cards[cid][-1]["at"], reverse=True)
        for card_id in newest[MAX_CARDS_PER_SYMPHONY:]:
            del cards[card_id]

    def events(self, symphony: str, card_id: str) -> list[EventRecord]:
        """A copy of the card's events, oldest first. Empty when none were recorded."""
        return copy.deepcopy(self._cards.get(symphony, {}).get(card_id, []))

    def has_card(self, symphony: str, card_id: str) -> bool:
        return card_id in self._cards.get(symphony, {})

    def cards(self, symphony: str) -> list[str]:
        return list(self._cards.get(symphony, {}))

    def symphonies(self) -> list[str]:
        return list(self._cards)

    def to_snapshot(self) -> dict[str, dict[str, list[EventRecord]]]:
        return copy.deepcopy(self._cards)

    def load(self, raw: object) -> None:
        """Replace the log with a loaded snapshot payload. A malformed entry is dropped, never fatal."""
        self._cards = {}
        if not isinstance(raw, dict):
            return
        for symphony, cards in raw.items():
            if not isinstance(symphony, str) or not isinstance(cards, dict):
                continue
            kept: dict[str, list[EventRecord]] = {}
            for card_id, events in cards.items():
                if not isinstance(card_id, str) or not isinstance(events, list):
                    continue
                cleaned = [e for e in (_valid_event(raw_event) for raw_event in events) if e is not None]
                if cleaned:
                    kept[card_id] = cleaned[-MAX_EVENTS_PER_CARD:]
            if kept:
                self._cards[symphony] = kept
                self._trim(symphony)


def stage_of(state: Mapping[str, object]) -> str | None:
    """The performer stage the state is in, or None when it names none."""
    stage = state.get("performer_stage")
    return stage if isinstance(stage, str) and stage else None


def card_id_of(state: Mapping[str, object]) -> str:
    """The id of the card the state is working, or '' when it has none."""
    card = state.get("current_card")
    return str(card.get("id") or "") if isinstance(card, dict) else ""


def record_lifecycle(state: Mapping[str, object], kind: str, card_id: str, **fields: Any) -> bool:
    """Record one event for a card of the running symphony. A no-op with no log in state.

    ``fields`` are as for :meth:`CardLifecycleLog.record`.
    """
    log = state.get("card_lifecycle")
    if not isinstance(log, CardLifecycleLog):
        return False
    symphony = str(state.get("current_symphony") or "")
    return log.record(symphony, str(card_id), kind, **fields)


__all__ = [
    "EVENT_KINDS",
    "MAX_CARDS_PER_SYMPHONY",
    "MAX_EVENTS_PER_CARD",
    "CardLifecycleLog",
    "card_id_of",
    "record_lifecycle",
    "stage_of",
]
