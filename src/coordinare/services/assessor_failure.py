"""098: assessor (junie) failure-shape classification.

The assessor stage runs the Junie harness against a heavily-shared self-hosted
model. Under load the upstream returns responses Junie's strict parser cannot
use, in a handful of recognisable *shapes*. This module maps a failure *reason*
to its shape so the coordinare can:

  - route a flaky parse/empty failure into the existing bounded system-error
    retry instead of terminal-blocking the card (US1), and
  - distinguish a persistently empty/overloaded upstream (``empty_body``) — an
    infrastructure condition surfaced as ENV_BLOCKED (US3) — from a malformed
    response that simply blocks after the retry budget.

Pure and secret-free: it matches only on the *shape* of the reason string and
returns a shape token — never raw model output, never tokens. Returns ``None``
for any reason that is NOT a transient assessor parse/empty failure (e.g. a
genuine model-capability prose verdict), which MUST NOT be reclassified as
retryable — otherwise a real failure would retry forever.
"""
from __future__ import annotations

from typing import Literal

# The coordinare system-error retry tag (see monitor_performer). A reason may
# already carry it when re-classified at exhaustion; strip before matching.
_FORMAT_ERROR_PREFIX = "BACKEND_FORMAT_ERROR:"

AssessorFailureShape = Literal["empty_answer", "malformed_body", "empty_body", "truncated"]

# Empty/no-response-at-all: the shared model returned nothing (overload/down).
# These surface as ENV_BLOCKED after the retry budget (infra, not the card).
_EMPTY_BODY_MARKERS = (
    "empty response body",
    "empty body",
    "empty response",
    "no response from",
    "no response body",
    "returned nothing",
    "upstream returned empty",
    "empty upstream",
)

# A response arrived but carried no usable answer (reasoning model spent its
# whole budget thinking; finish_reason=length with empty content).
_EMPTY_ANSWER_MARKERS = (
    "empty content",
    "no answer",
    "no content",
    "empty completion",
)

# The answer was cut off mid-stream.
_TRUNCATED_MARKERS = (
    "finish_reason=length",
    "finish reason: length",
    "finish reason length",
    "truncated",
    "max_tokens reached",
    "max tokens reached",
)

# The body could not be deserialised by the strict harness parser (control
# characters, malformed JSON, junie's "Failed to build 'issue.md…'").
_MALFORMED_BODY_MARKERS = (
    "failed to build",
    "junie_standalone",
    "could not parse",
    "cannot parse",
    "deserialize",
    "deserialization",
    "invalid control character",
    "control character",
    "malformed json",
    "malformed body",
    "jsondecode",
    "openaicompletion",
)


def classify_assessor_failure(
    reason: str | None, *, finish_reason: str | None = None,
) -> AssessorFailureShape | None:
    """Return the assessor failure shape for ``reason``, or ``None``.

    ``None`` means "not a transient assessor parse/empty failure" — the caller
    must NOT route it into the retry path. An empty/whitespace reason is treated
    as ``empty_body`` (the upstream produced nothing).
    """
    if finish_reason in {"length", "max_tokens"}:
        return "truncated"
    if reason is None:
        return "empty_body"
    text = reason.strip()
    if text.startswith(_FORMAT_ERROR_PREFIX):
        text = text[len(_FORMAT_ERROR_PREFIX):].strip()
    if not text:
        return "empty_body"
    low = text.lower()

    if finish_reason is None and any(m in low for m in _TRUNCATED_MARKERS):
        return "truncated"

    # Order: empty-body (infra) is the most consequential distinction, then
    # parse/malformed, then the answer-content shapes. A reason rarely carries
    # more than one marker; this ordering makes the priority explicit.
    if any(m in low for m in _EMPTY_BODY_MARKERS):
        return "empty_body"
    if any(m in low for m in _MALFORMED_BODY_MARKERS):
        return "malformed_body"
    if any(m in low for m in _EMPTY_ANSWER_MARKERS):
        return "empty_answer"
    return None
