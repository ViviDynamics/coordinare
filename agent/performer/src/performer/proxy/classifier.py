"""080 — difficulty classifier for conditional escalation.

A cheap pre-pass that scores a turn's difficulty in [0, 1]. Used only by
``ConditionalEscalation``: score ≥ threshold → think→act, else act-only. A
classifier failure MUST default to "think" (score 1.0) — conservative for
correctness (FR-015).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from performer.proxy.llm_turn import LLMRequest
from performer.proxy.upstreams import Upstream, UpstreamError

# On any failure, escalate (think). 1.0 ≥ any threshold in [0, 1].
_FAIL_SCORE = 1.0


@runtime_checkable
class DifficultyClassifier(Protocol):
    async def score(self, request: LLMRequest) -> float:
        """Return a difficulty score in [0, 1]; never raises (failure → 1.0)."""
        ...


@dataclass
class ModelClassifier:
    """Scores difficulty with a cheap model upstream.

    Asks the model for a 0..1 difficulty estimate and parses the first float in
    its reply. Any failure (call error, unparseable) → escalate (1.0).
    """

    upstream: Upstream

    async def score(self, request: LLMRequest) -> float:
        from performer.proxy.llm_turn import LLMRequest as _Req
        from performer.proxy.llm_turn import Message

        probe = _Req(
            messages=(
                Message.system(
                    "Rate how much deliberate planning the next assistant turn needs, "
                    "from 0.0 (trivial/mechanical) to 1.0 (hard, multi-step reasoning). "
                    "Reply with ONLY the number.",
                ),
                *request.messages,
            ),
        )
        try:
            resp = await self.upstream.complete(probe, tools_enabled=False)
        except UpstreamError:
            return _FAIL_SCORE
        return _parse_score(resp.content or resp.reasoning or "")


def _parse_score(text: str) -> float:
    import re

    m = re.search(r"\d*\.?\d+", text)
    if not m:
        return _FAIL_SCORE
    try:
        return max(0.0, min(1.0, float(m.group())))
    except ValueError:
        return _FAIL_SCORE
