"""098 (T002/US1/US3): unit tests for the assessor failure-shape classifier.

`classify_assessor_failure(reason)` is a pure, secret-free function that maps an
assessor (junie) failure *reason* to one of four shapes — or ``None`` when the
reason is NOT a transient parse/empty assessor failure (e.g. a genuine
model-capability prose verdict, which MUST NOT be reclassified as retryable).
"""
from __future__ import annotations

import pytest

from coordinare.services.assessor_failure import classify_assessor_failure


@pytest.mark.parametrize(
    "reason",
    [
        "Junie failed with the message: Failed to build 'issue.md.junie_standalone'",
        "Failed to build 'issue.md.junie_standalone'",
        "OpenAICompletion deserialization failed: invalid control character at line 3",
        "could not parse response body: malformed JSON",
    ],
)
def test_malformed_body_shapes(reason: str) -> None:
    assert classify_assessor_failure(reason) == "malformed_body"


@pytest.mark.parametrize(
    "reason",
    [
        "model returned empty content with finish_reason=length",
        "empty content: the model produced no answer",
        "finish_reason=length",
    ],
)
def test_empty_answer_shapes(reason: str) -> None:
    assert classify_assessor_failure(reason) == "empty_answer"


@pytest.mark.parametrize(
    "reason",
    [
        "upstream returned an empty response body",
        "empty body",
        "no response from upstream model",
        "upstream returned nothing",
        "",
    ],
)
def test_empty_body_shapes(reason: str) -> None:
    assert classify_assessor_failure(reason) == "empty_body"


@pytest.mark.parametrize(
    "reason",
    [
        "response truncated before completion",
        "output truncated: max_tokens reached",
    ],
)
def test_truncated_shapes(reason: str) -> None:
    assert classify_assessor_failure(reason) == "truncated"


@pytest.mark.parametrize(
    "reason",
    [
        "The implementation does not satisfy the acceptance criteria for this card.",
        "Assessment failed: the plan omits the required database migration.",
        "model produced an assessment but it referenced the wrong file",
        "git push failed: workflow scope not permitted",
    ],
)
def test_non_assessor_reasons_return_none(reason: str) -> None:
    """A real model-capability / non-parse failure MUST NOT be classified as a
    retryable assessor shape (else it would retry forever — the critical guard)."""
    assert classify_assessor_failure(reason) is None


def test_classifier_tolerates_format_error_prefix() -> None:
    """The reason may already carry the BACKEND_FORMAT_ERROR: tag (re-classified
    at system-error exhaustion for US3) — the underlying shape must still resolve."""
    tagged = "BACKEND_FORMAT_ERROR: upstream returned an empty response body"
    assert classify_assessor_failure(tagged) == "empty_body"
