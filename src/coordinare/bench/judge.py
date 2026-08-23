"""Spec 135 — the LLM judge, to the spec-077 contract (persona_bench.judge).

Same grading model as ``scripts/persona_bench.py``: rubric + deterministic
signals + evidence → strict JSON ``{"correct": bool, "quality": 0-5, "reason":
str}`` at temperature 0 over the LiteLLM proxy's OpenAI-compatible
``chat/completions``. A judge failure returns ``(None, None, "judge_error: …")``
and never flips a verdict (the grader falls back to deterministic-only for that
card, visibly). Reimplemented here rather than imported because ``scripts/`` is
not a package (research.md R3).
"""

from __future__ import annotations

import json
from typing import Protocol

import httpx

# (correct, quality 0-5, reason) — None correct means "not judged / judge error".
JudgeVerdict = tuple[bool | None, int | None, str]


class JudgeFn(Protocol):
    def __call__(self, rubric: str, det_detail: str, evidence: str) -> JudgeVerdict: ...


def parse_verdict(content: str) -> JudgeVerdict:
    """Parse the judge's raw completion into a verdict.

    Malformed output — no JSON object, or a missing/null ``correct`` field — is a
    judge ERROR (``correct=None``), never a False verdict: the grader must fall
    back to deterministic-only for that card instead of failing it (FR-004 edge
    case). ``quality`` outside the documented 0-5 range is dropped to None."""
    s = content[content.find("{") : content.rfind("}") + 1]
    d = json.loads(s)
    if d.get("correct") is None:
        return None, None, "judge_error: malformed response (missing 'correct' field)"
    quality = d.get("quality")
    if not isinstance(quality, int) or isinstance(quality, bool) or not 0 <= quality <= 5:
        quality = None
    return bool(d["correct"]), quality, str(d.get("reason", ""))[:160]


def make_judge(base_url: str, api_key: str, model: str, *, timeout: float = 120.0) -> JudgeFn:
    """Build a judge callable bound to a LiteLLM-proxy endpoint."""

    def _judge(rubric: str, det_detail: str, evidence: str) -> JudgeVerdict:
        prompt = (
            "You are grading whether an AI-driven development pipeline correctly "
            "completed a whole work card (issue -> implementation -> review -> "
            f"CI -> merge decision). GROUND TRUTH / RUBRIC:\n{rubric}\n\n"
            f"Deterministic signals already gathered: {det_detail}\n\n"
            f"The recorded lifecycle evidence (truncated):\n{evidence[:2500]}\n\n"
            'Reply with ONLY a JSON object: {"correct": true|false, '
            '"quality": 0-5, "reason": "one sentence"}.'
        )
        try:
            r = httpx.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 300,
                },
                timeout=timeout,
            )
            content = r.json()["choices"][0]["message"]["content"]
            return parse_verdict(content)
        except Exception as exc:
            return None, None, f"judge_error: {type(exc).__name__}: {exc}"[:160]

    return _judge
