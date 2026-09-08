"""163: structured output exhaustion takes precedence over malformed prose."""
from __future__ import annotations

import pytest

from coordinare.services.assessor_failure import classify_assessor_failure


@pytest.mark.parametrize('finish', ['length', 'max_tokens'])
@pytest.mark.parametrize('reason', ['malformed JSON', 'empty response body', '', None])
def test_structured_exhaustion_is_truncated(finish, reason):
    assert classify_assessor_failure(reason, finish_reason=finish) == 'truncated'


@pytest.mark.parametrize('reason', ['malformed JSON truncated', 'empty content finish_reason=length', 'finish reason: length'])
def test_prose_exhaustion_remains_supported(reason):
    assert classify_assessor_failure(reason) == 'truncated'


def test_normal_structured_finish_overrules_stale_truncation_prose():
    assert classify_assessor_failure('empty content, previously truncated', finish_reason='stop') == 'empty_answer'


def test_actual_malformed_response_stays_malformed():
    assert classify_assessor_failure('malformed JSON', finish_reason='stop') == 'malformed_body'
