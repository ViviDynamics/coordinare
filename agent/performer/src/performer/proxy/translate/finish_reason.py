"""Single source of truth for the OpenAI ``finish_reason`` ↔ Anthropic
``stop_reason`` mapping (spec 084, FR-005).

The same mapping MUST be applied in the non-streaming path
(:func:`proxy.translate.response.translate_response`) and the streaming path
(the closing ``message_delta`` of the ``proxy.translate.sse`` filter). Both
import :func:`map_finish_reason` from here so the two paths cannot drift
(Decision 6).

Mapping (per ``contracts/response-translation.md``):

==========================  =========================
OpenAI ``finish_reason``    Anthropic ``stop_reason``
==========================  =========================
``stop``                    ``end_turn``
``length``                  ``max_tokens``
``tool_calls``              ``tool_use``
``content_filter``          ``end_turn`` (closest safe terminal)
unknown / absent / ``null`` ``end_turn`` (default)
==========================  =========================
"""

from __future__ import annotations

#: The default Anthropic ``stop_reason`` for unknown, absent, or ``null``
#: OpenAI ``finish_reason`` values (and the closest safe terminal for
#: ``content_filter``).
DEFAULT_STOP_REASON = "end_turn"

#: Explicit, documented OpenAI → Anthropic terminal-reason mapping. Any value
#: not present here (including ``None``) maps to :data:`DEFAULT_STOP_REASON`.
FINISH_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


def map_finish_reason(finish_reason: str | None) -> str:
    """Map an OpenAI ``finish_reason`` to an Anthropic ``stop_reason``.

    ``None`` (absent / ``null`` in the upstream payload) and any unrecognized
    value both fall back to :data:`DEFAULT_STOP_REASON` (``end_turn``) — a
    deterministic, no-silent-drop default (FR-005).
    """
    if finish_reason is None:
        return DEFAULT_STOP_REASON
    return FINISH_REASON_MAP.get(finish_reason, DEFAULT_STOP_REASON)
