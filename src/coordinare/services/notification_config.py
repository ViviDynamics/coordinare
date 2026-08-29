"""Tolerant construction of the notification configuration (spec 139 / issue #187).

Notifications are **non-essential**: coordinare's work is unaffected by their
absence. So a channel that cannot be used makes that channel inactive, rather
than stopping the daemon.

That reasoning is deliberately narrow. The test for whether a subsystem may
degrade is *what happens if coordinare proceeds*: with a broken notification
channel it does its work and tells nobody, which is recoverable and made visible
by the startup posture line. With broken GitHub, board or model-endpoint
configuration it would do the **wrong** work while appearing healthy, so those
must keep failing loudly. This module is not a precedent for relaxing them.

**Why here rather than in the validators.** The obvious simplification is to
delete the rule in ``config.py`` that rejects a Slack channel with no
``webhook_url``. That fixes the daemon and blinds ``config validate`` in the same
move — trading a loud failure for a silent typo, which is the worse of the two,
because a boot failure at least tells you immediately. The models keep their
rules; only the daemon's construction of its config tolerates what they reject.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

__all__ = ["SkippedChannel", "describe_notification_posture", "sanitize_notifications"]


@dataclass(frozen=True, slots=True)
class SkippedChannel:
    """A channel that will not be used, and why.

    The reason is not decoration. The failure this replaces was at least loud; a
    silent drop would trade one invisible problem for another, so every skip has
    to be discoverable without reading the config file.
    """

    name: str
    reason: str


def _channel_error(channel: dict[str, Any]) -> str | None:
    """The reason this channel cannot be used, or ``None`` if it can.

    Asks the real model rather than re-implementing its rules, so a validation
    rule added in ``config.py`` is honoured here without anyone remembering to
    mirror it.
    """
    from pydantic import ValidationError

    from coordinare.config import ChannelConfig

    try:
        ChannelConfig(**channel)
    except ValidationError as exc:
        parts = []
        for error in exc.errors():
            location = ".".join(str(item) for item in error.get("loc", ()) if item != "__root__")
            message = error.get("msg", "invalid")
            parts.append(f"{location}: {message}" if location else message)
        return "; ".join(parts)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        return str(exc)
    return None


def sanitize_notifications(raw: dict[str, Any]) -> tuple[dict[str, Any], list[SkippedChannel]]:
    """Return *raw* with unusable notification config removed, plus what was removed.

    Covers the top-level ``notifications`` block **and** each symphony's
    ``overrides.notifications``. Missing the latter was a real hole: a
    multi-symphony config with a bad channel in an override passes the daemon's
    tolerance check and builds ``CoordinareConfiguration`` without complaint —
    because ``overrides`` is an untyped ``dict[str, Any]`` — and then raises much
    later, from ``SymphonyConfig.effective_config``, at a point far from the cause.

    Never raises: the whole point is that this cannot be the thing that stops
    coordinare starting. The input is not mutated, because callers hand the same
    dict to other consumers.
    """
    top_level = raw.get("notifications")
    symphonies = raw.get("symphonies")
    has_symphony_blocks = isinstance(symphonies, list) and any(
        isinstance(s, dict) and isinstance((s.get("overrides") or {}).get("notifications"), dict)
        for s in symphonies
    )
    if not isinstance(top_level, dict) and not has_symphony_blocks:
        return raw, []

    sanitized = copy.deepcopy(raw)
    skipped: list[SkippedChannel] = []

    if isinstance(sanitized.get("notifications"), dict):
        skipped.extend(_sanitize_block(sanitized["notifications"], label=""))

    for symphony in sanitized.get("symphonies") or []:
        if not isinstance(symphony, dict):
            continue
        block = (symphony.get("overrides") or {}).get("notifications")
        if isinstance(block, dict):
            name = str(symphony.get("name") or "?")
            skipped.extend(_sanitize_block(block, label=f"symphony {name}: "))

    return sanitized, skipped


def _sanitize_block(block: dict[str, Any], *, label: str) -> list[SkippedChannel]:
    """Strip unusable channels and unreachable routing from one notifications block.

    Mutates *block* in place; the caller owns a copy. *label* distinguishes a
    symphony's block from the global one in the reported reason, since otherwise
    two channels of the same name in different symphonies are indistinguishable.
    """
    skipped: list[SkippedChannel] = []

    channels = block.get("channels")
    if isinstance(channels, list):
        usable = []
        for index, channel in enumerate(channels):
            if not isinstance(channel, dict):
                skipped.append(SkippedChannel(f"{label}channel[{index}]", "not a mapping"))
                continue
            name = f"{label}{channel.get('name') or f'channel[{index}]'}"
            reason = _channel_error(channel)
            if reason is None:
                usable.append(channel)
            else:
                skipped.append(SkippedChannel(name, reason))
        block["channels"] = usable

    # A routing entry can only reach a channel that survived. Entries naming one
    # that did not are dropped rather than being allowed to fail the load — the
    # ordinary way to reach this is removing a channel and leaving its routing
    # behind.
    surviving = {c.get("name") for c in block.get("channels", []) if isinstance(c, dict)}
    routing = block.get("routing")
    if isinstance(routing, list):
        kept = []
        for entry in routing:
            if not isinstance(entry, dict):
                continue
            names = entry.get("channels")
            if not isinstance(names, list):
                kept.append(entry)
                continue
            reachable = [n for n in names if n in surviving]
            if not reachable:
                continue  # nothing left to deliver to
            entry = {**entry, "channels": reachable}
            kept.append(entry)
        block["routing"] = kept

    return skipped


def describe_notification_posture(
    *, active: list[str], skipped: list[SkippedChannel]
) -> dict[str, Any]:
    """What will and will not be told to the operator, as structured log fields.

    The developer this spec came from could not tell "nothing is wrong" from
    "nothing is watching". That is the question this answers, in one line.

    Channel **names** appear; channel **configuration** never does. Names are
    operator-chosen labels, while webhook URLs and passwords are secrets.
    """
    if active:
        summary = (
            f"notifications active: {', '.join(sorted(active))}; "
            "stall signals also appear in the coordinare log and the dashboard activity feed"
        )
    else:
        summary = (
            "no notification channels configured; stall signals appear in the coordinare "
            "log and the dashboard activity feed only"
        )

    if skipped:
        summary += f"; {len(skipped)} channel(s) skipped as unusable"

    return {
        "active_channels": sorted(active),
        "skipped_channels": [{"name": s.name, "reason": s.reason} for s in skipped],
        "summary": summary,
    }
