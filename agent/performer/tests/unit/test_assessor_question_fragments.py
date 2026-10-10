"""Malformed assessment output must not discard a pending human decision."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends.base import BackendStatus
from performer.config import Settings
from performer.main import handle_status
from performer.models import Performance, Score, Stand
from performer.protocol import PerformerMessage

QUESTION = "Should empty input return a blank string or raise ValueError?"


def performance(output: str) -> Performance:
    backend = MagicMock()
    backend.get_status.return_value = BackendStatus(state="done", output=output)
    backend.relay_feedback = AsyncMock()
    perf = Performance(
        session_id="synthetic",
        stand=Stand(path=Path("/tmp/synthetic"), branch="synthetic"),
        score=Score(title="Synthetic assessment", repo_url="https://github.com/example/sample",
                    branch="synthetic", github_token="synthetic"),
        backend=backend,
    )
    perf.role = "assessing"
    perf.state = "working"
    return perf


@pytest.mark.asyncio
@pytest.mark.parametrize("output,questions", [
    ('": false, "questions": ["' + QUESTION + '"]}', [QUESTION]),
    ('{"sufficient": false, "questions": ["' + QUESTION + '"]', [QUESTION]),
    ('prefix "questions": ["Choose \\\"blank\\\"?", "Second [choice]?\\nExplain."] suffix',
     ['Choose "blank"?', "Second [choice]?\nExplain."]),
    ('broken "questions": ["One?"], "questions": ["Two?", "One?"]', ["One?", "Two?"]),
    ('"assessment": {"ready": false, "questions": ["' + QUESTION + '"]}', [QUESTION]),
    ('{"questions": ["' + QUESTION + '"]}', [QUESTION]),
    ('broken "questions": ["Should {} count as empty?" ]', ["Should {} count as empty?"]),
    ('broken "questions": ["Choose?"], "metadata": {}', ["Choose?"]),
    ('broken "questions": ["Choose?"], "metadata": {"assessment": {"ready": true}}', ["Choose?"]),
    (r'broken "quest\u0069ons": ["Choose?"]', ["Choose?"]),
])
async def test_recovered_questions_block_without_committing(output, questions):
    perf = performance(output)
    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf,
                                      Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=0))
    assert response.status == "blocked"
    assert response.questions == questions
    assert perf.open_questions == questions
    assert perf.assessment_questions == questions
    commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    'broken "questions": ["Truncated?',
    'broken "questions": []',
    'broken "questions": "Choose?"',
    'broken "questions": [null]',
    'broken "questions": ["Choose?", 1]',
    'broken "questions": ["   "]',
    'broken "sufficient": false',
    'broken "assessment": {"ready": false',
    '"assessment": {"ready": false, "questions": []}',
    'broken "questions": [], "metadata": {}',
    r'broken "suffici\u0065nt": false',
])
async def test_unrecoverable_contract_output_fails_closed(output):
    perf = performance(output)
    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf,
                                      Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=0))
    assert response.status == "error"
    assert perf.state == "error"
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_fragment_still_gets_configured_parse_retry():
    perf = performance('": false, "questions": ["' + QUESTION + '"]}')
    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf,
                                      Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=1))
    assert response.status == "working"
    assert perf.parse_retry_count == 1
    perf.backend.relay_feedback.assert_awaited_once()
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_questions_redact_secrets_before_surface_and_persistence():
    token = "ghp_" + "A" * 36
    perf = performance('broken "questions": ["Can I use ' + token + '?"]')
    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf,
                                      Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=0))
    assert response.status == "blocked"
    assert token not in str(response.questions)
    assert token not in str(perf.open_questions)
    assert token not in str(perf.assessment_questions)
    assert response.questions and "Can I use" in response.questions[0]
    commit.assert_not_awaited()
