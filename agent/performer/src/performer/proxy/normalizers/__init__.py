"""Format-keyed response normalizers for the self-hosted robustness layer (spec 078).

A normalizer is a pure, reusable transform keyed by response format / model-family
(not by agent — quirks are per-format and shared across agents). Each normalizer
handles BOTH the non-streaming JSON path (``normalize_json``) and the streaming SSE
path (``sse_filter`` -> a stateful filter that buffers across chunk boundaries and
never emits a half-parsed tool call). See ``base.py`` for the protocol.

``NORMALIZER_REGISTRY`` maps a registry key (e.g. ``harmony_tool_calls``,
``strip_reasoning``) to its normalizer instance. The routing-table loader validates
that every declared ``normalizers`` key resolves here at config-load time, so an
unknown key fails fast rather than at runtime.
"""

from __future__ import annotations

from .base import Normalizer, StatefulSSEFilter
from .harmony import HarmonyToolCallsNormalizer
from .reasoning import StripReasoningNormalizer

# Format-keyed normalizer instances. Keys are validated against this registry by
# the routing-table loader at config-load time, so an unknown key fails fast.
# Instances are stateless and reusable across backends (the per-stream state lives
# in the fresh StatefulSSEFilter each ``sse_filter()`` call returns).
NORMALIZER_REGISTRY: dict[str, Normalizer] = {
    n.key: n
    for n in (HarmonyToolCallsNormalizer(), StripReasoningNormalizer())
}

__all__ = [
    "NORMALIZER_REGISTRY",
    "Normalizer",
    "StatefulSSEFilter",
    "HarmonyToolCallsNormalizer",
    "StripReasoningNormalizer",
]
