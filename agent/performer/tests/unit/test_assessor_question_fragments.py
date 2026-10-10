"""Malformed assessment output must not discard a pending human decision."""
from __future__ import annotations

import json
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
    ('intro "[" note broken "questions":["Keep me?"]', ["Keep me?"]),
    (json.dumps({"notes": '[{"questions":["Unrelated?"]}]', "sufficient": False,
                 "questions": ["Keep me?"]}), ["Keep me?"]),
    (json.dumps({"notes": '[{"value":"note"}]', "assessment": {
        "ready": False, "questions": ["Keep me?"]}}), ["Keep me?"]),
    ('"notes": ' + json.dumps('[{"value":"note"}]') + ' "questions":["Keep me?"]', ["Keep me?"]),
    ('"notes": ' + json.dumps('[{"value":"note"}]') + ' broken "questions":["Keep me?"]', ["Keep me?"]),
    ('"notes": ' + json.dumps('[{"value":"note"}]') + ' "assessment":{"ready":false,"questions":["Keep me?"]}', ["Keep me?"]),
    (json.dumps({'[{"value":"note"}]': "ignored", "sufficient": False,
                 "questions": ["Keep me?"]}), ["Keep me?"]),
    (json.dumps({'[{"value":"note"}]': "ignored", "assessment": {
        "ready": False, "questions": ["Keep me?"]}}), ["Keep me?"]),
    (json.dumps(['[{"value":"note"}]']) + ', "questions":["Keep me?"]', ["Keep me?"]),
    (json.dumps(['[{"questions":["Unrelated?"]}]']) + ', "questions":["Keep me?"]', ["Keep me?"]),
    (json.dumps('[{"value":"note"}]') + ', "questions":["Keep me?"]', ["Keep me?"]),
    (json.dumps('[{"questions":["Unrelated?"]}]') + ', "questions":["Keep me?"]', ["Keep me?"]),
    ('broken "questions": ["Choose?"], "metadata": {}', ["Choose?"]),
    ('broken "questions": ["Choose?"], "metadata": {"assessment": {"ready": true}}', ["Choose?"]),
    (r'broken "quest\u0069ons": ["Choose?"]', ["Choose?"]),
    ('broken "questions": ["Keep me?"], "questions": ["truncated?"', ["Keep me?"]),
    ('broken "questions": ["truncated?", "questions": ["Keep me?"]', ["Keep me?"]),
    ('broken "questions": null, "questions": ["Keep me?"]', ["Keep me?"]),
    ('broken "questions": ["Keep me?"], "questions": [null]', ["Keep me?"]),
    ('{"assessment": null, "questions": ["Need a decision?"]}', ["Need a decision?"]),
    ('{"assessment": [], "questions": ["Need a decision?"]}', ["Need a decision?"]),
    ('{"assessment": "ready", "questions": ["Need a decision?"]}', ["Need a decision?"]),
    ('{"assessment": false, "questions": ["Need a decision?"]}', ["Need a decision?"]),
    ('{"assessment": 1, "questions": ["Need a decision?"]}', ["Need a decision?"]),
    ('{"sufficient": false, "questions": ["Keep me?"], "questions": null}', ["Keep me?"]),
    ('{"sufficient": false, "questions": null, "questions": ["Keep me?"]}', ["Keep me?"]),
    ('{"sufficient": false, "questions": ["Keep me?"], "questions": [null]}', ["Keep me?"]),
    ('{"sufficient": false, "questions": ["Keep me?"], "questions": ["Also me?"]}', ["Keep me?", "Also me?"]),
    ('{"sufficient": false, "questions": ["Keep me?"], "metadata": {"questions": ["Unrelated?"], "questions": []}}', ["Keep me?"]),
    ('{"assessment":{"ready":false,"questions":["Need a decision?"]},"assessment":{"ready":true,"questions":[]}}', ["Need a decision?"]),
    ('{"assessment":{"ready":true,"questions":[]},"assessment":{"ready":false,"questions":["Need a decision?"]}}', ["Need a decision?"]),
    ('{"assessment":{"ready":false,"ready":true,"questions":["Need a decision?"]}}', ["Need a decision?"]),
    ('{"assessment":{"ready":false,"questions":["Keep me?"],"questions":null}}', ["Keep me?"]),
    ('{"assessment":{"ready":false,"questions":["Keep me?"],"verdict":"work","verdict":"not_work"}}', ["Keep me?"]),
    ('{"assessment":{"ready":false,"questions":["Keep me?"],"questions":[]},"metadata":{"questions":["Unrelated?"]}}', ["Keep me?"]),
    ('{"sufficient":false,"questions":["Keep me?"],"questions":null,"metadata":{"questions":["Unrelated?"]}}', ["Keep me?"]),
    ('{"sufficient":"false","questions":["Need a decision?"]}', ["Need a decision?"]),
    ('{"sufficient":1,"questions":["Need a decision?"]}', ["Need a decision?"]),
    ('{"sufficient":null,"questions":["Need a decision?"]}', ["Need a decision?"]),
    ('{"assessment":{"ready":"false","questions":["Need a decision?"]}}', ["Need a decision?"]),
    ('{"assessment":{"ready":1,"questions":["Need a decision?"]}}', ["Need a decision?"]),
    ('{"assessment":{"ready":null,"questions":["Need a decision?"]}}', ["Need a decision?"]),
    ('intro {example}\n```json\n{"sufficient":false,"questions":["Keep me?"],"questions":[]}\n```', ["Keep me?"]),
    ('intro {example}\n```json\n{"assessment":{"ready":false,"questions":["Keep me?"]},"assessment":{"ready":true,"questions":[]}}\n```', ["Keep me?"]),
    ('intro {example}\n```json\n{"sufficient":false,"questions":["Keep me?"],"questions":null,"metadata":{"questions":["Unrelated?"]}}\n```', ["Keep me?"]),
    ('intro "metadata": {\n```json\n{"assessment":{"ready":false,"questions":["Need a decision?"]}}\n```', ["Need a decision?"]),
    ('intro "metadata": [\n```json\n{"sufficient":false,"questions":["Need a decision?"]}\n```', ["Need a decision?"]),
    ('intro "metadata": {\n```json\n{"assessment":{"ready":false,"questions":["Keep me?"],"questions":[]}}\n```', ["Keep me?"]),
    ('broken "questions":["Keep me?"]\n```json\n{"assessment":{"ready":true,"questions":[]}}\n```', ["Keep me?"]),
    ('intro {example}\nbroken "questions":["Keep me?"]\n```json\n{"assessment":{"ready":true,"questions":[]}}\n```', ["Keep me?"]),
    ('```json\n{"assessment":{"ready":true,"questions":[]}}\n```\nbroken "questions":["Keep me?"]\ntrailer {example}', ["Keep me?"]),
    ('intro {example}\nbroken "questions":["Keep me?"]\n```json\n{"assessment":{"ready":true,"questions":[]}}\n```\nbroken "questions":["Also me?"]\ntrailer {example}', ["Keep me?", "Also me?"]),
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
    '{"assessment": null}',
    '{"assessment": [], "questions": []}',
    '{"assessment":{"ready":false,"questions":[]},"assessment":{"ready":true,"questions":[]}}',
    '{"assessment":{"ready":false,"ready":true,"questions":[]}}',
    '{"sufficient":"false","questions":[]}',
    '{"sufficient":1,"questions":[]}',
    '{"sufficient":false,"questions":"Need a decision?"}',
    '{"sufficient":false,"questions":["Need a decision?",42]}',
    '{"sufficient":true,"questions":null}',
    '{"assessment":{"ready":"false","questions":[]}}',
    '{"assessment":{"ready":false,"questions":"Need a decision?"}}',
    '{"assessment":{"ready":false,"questions":["Need a decision?",42]}}',
    '{"assessment":{"ready":true,"questions":{}}}',
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


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["broken", 'intro "[" note broken',
                                    '"notes": ' + json.dumps('[{"value":"note"}]') + ',',
                                    '"notes": ' + json.dumps('[{"value":"note"}]'),
                                    '"notes": ' + json.dumps('[{"value":"note"}]') + ' broken',
                                    json.dumps(['[{"value":"note"}]']) + ',',
                                    json.dumps('[{"value":"note"}]') + ','])
@pytest.mark.parametrize("repaired", [
    '{"sufficient":true,"questions":[]}',
    '{"assessment":{"ready":true,"questions":[]}}',
    'The issue is clear and ready for implementation.',
    '{"assessment":{"ready":false,"questions":["Different?"]}}',
    '{"assessment":{"ready":false,"verdict":"not_work","questions":[]}}',
    '{"assessment":{"ready":false,"verdict":"needs_split","questions":[]}}',
])
async def test_parse_repair_cannot_discard_an_unanswered_question(repaired, prefix):
    perf = performance(prefix + ' "questions": ["' + QUESTION + '"]')
    settings = Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=1)
    message = PerformerMessage(action="status", session_id="synthetic")
    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        first = await handle_status(message, perf, settings)
        assert first.status == "working"
        perf.backend.get_status.return_value = BackendStatus(state="done", output=repaired)
        second = await handle_status(message, perf, settings)
    assert second.status == "blocked"
    assert second.questions == [QUESTION]
    assert perf.assessment_questions == [QUESTION]
    assert perf.open_questions == [QUESTION]
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_question_is_redacted_and_retained_during_parse_retry():
    token = "ghp_" + "B" * 36
    perf = performance('broken "questions": ["Use ' + token + '?"]')
    response = await handle_status(
        PerformerMessage(action="status", session_id="synthetic"), perf,
        Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=1),
    )
    assert response.status == "working"
    assert perf.open_questions and perf.assessment_questions == perf.open_questions
    assert token not in str(perf.open_questions)


@pytest.mark.asyncio
@pytest.mark.parametrize("output,expected", [
    ('broken "metadata": {"questions":["Unrelated?"]}', []),
    ('broken "metadata":[garbage } {"questions":["Unrelated?"]}]', []),
    ('broken "metadata":{garbage ] "questions":["Unrelated?"]}', []),
    ('broken "metadata": {"assessment":{"ready":false,"questions":["Unrelated?"]}}', []),
    ('broken "metadata": {"questions":["Unrelated?"]', []),
    ('broken "questions":["Keep me?"], "metadata":{"questions":["Unrelated?"]}', ["Keep me?"]),
    ('broken "questions":["Keep me?"], "metadata":{"questions":["Unrelated?"]', ["Keep me?"]),
    ('broken "metadata":[{"questions":["Unrelated?"]}], "questions":["Keep me?"]', ["Keep me?"]),
    ('broken "metadata":{"sufficient":false,"questions":["Unrelated?"]}', []),
    ('broken "questions":["Keep me?"], "metadata":{"notes":"brace } and [", "questions":["Unrelated?"]}', ["Keep me?"]),
])
async def test_malformed_metadata_cannot_supply_assessment_questions(output, expected):
    perf = performance(output)
    with patch("performer.main.commit_file", new=AsyncMock()):
        response = await handle_status(
            PerformerMessage(action="status", session_id="synthetic"), perf,
            Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=0),
        )
    assert (response.questions or []) == expected
    assert "Unrelated?" not in str(perf.open_questions)
    if not expected:
        assert response.status == "assessment_complete"


@pytest.mark.asyncio
async def test_fresh_assessment_after_human_answer_can_advance():
    # Human answers cause daemon redispatch with a fresh Performance, rather
    # than format-repair feedback being mistaken for the human's decision.
    previous = performance('broken "questions":["' + QUESTION + '"]')
    settings = Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=0)
    message = PerformerMessage(action="status", session_id="synthetic")
    with patch("performer.main.commit_file", new=AsyncMock()):
        blocked = await handle_status(message, previous, settings)
        assert blocked.status == "blocked"
        reassessment = performance('{"sufficient":true,"questions":[]}')
        ready = await handle_status(message, reassessment, settings)
    assert ready.status == "assessment_complete"
    assert previous.open_questions == [QUESTION]


@pytest.mark.asyncio
@pytest.mark.parametrize("output,questions", [
    ('broken "questions":[{"questions":["Unrelated?"]}]', []),
    ('broken "questions":{"questions":["Unrelated?"]}', []),
    ('broken "ready":{"questions":["Unrelated?"]}', []),
    ('broken "sufficient":{"questions":["Unrelated?"]}', []),
    ('broken "assessment":[{"questions":["Unrelated?"]}]', []),
    ('broken "questions":["Keep me?"], "ready":{"questions":["Unrelated?"]}', ["Keep me?"]),
    ('broken "questions":[{"questions":["Unrelated?"]}', []),
    ('broken "questions":[{"questions":["Unrelated?"]},{"questions":["Also unrelated?"]}', []),
    ('broken "assessment":{"ready":false,"assessment":{"questions":["Unrelated?"]}}', []),
    ('broken "questions":["Keep me?"], "assessment":{"assessment":{"questions":["Unrelated?"]}}', ["Keep me?"]),
    ('broken "assessment":{"ready":false,"questions":["Keep me?"]}', ["Keep me?"]),
])
async def test_invalid_contract_values_cannot_supply_nested_questions(output, questions):
    perf = performance(output)
    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        response = await handle_status(
            PerformerMessage(action="status",session_id="synthetic"),perf,
            Settings(AGENT_BACKEND="claude_code",BACKEND_PARSE_RETRIES=0),
        )
    assert response.status == ("blocked" if questions else "error")
    assert (response.questions or []) == questions
    commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("output,questions", [
    ('[{"questions":["Unrelated?"]}]', []),
    ('[garbage, {"questions":["Unrelated?"]}]', []),
    ('[garbage } {"assessment":{"ready":true,"questions":[]}}]', []),
    ('[garbage } {"questions":["Unrelated?"]}]', []),
    ('[garbage } {"questions":["Unrelated?"]}], "questions":["Keep me?"]', ["Keep me?"]),
    ('[{"questions":[]}, garbage, {"questions":["Unrelated?"]}]', []),
    ('[garbage, {"questions":["Unrelated?"]}], "questions":["Keep me?"]', ["Keep me?"]),
    ('[garbage, {"assessment":{"ready":true,"questions":[]}}]', []),
    ('[garbage, {"sufficient":true,"questions":[]}]', []),
    ('[garbage, {"assessment":{"ready":false,"questions":[],"verdict":"not_work"}}]', []),
    ('[garbage, {"assessment":{"ready":true,"questions":[]}}', []),
    ('["note" garbage, {"assessment":{"ready":true,"questions":[]}}]', []),
    ('["note" {"assessment":{"ready":true,"questions":[]}}]', []),
    ('": false, [{"questions":["Unrelated?"]}]', []),
    ('broken "prefix [{"questions":["Unrelated?"]}]', []),
    ('": false, [" note", {"questions":["Unrelated?"]}]', []),
    ('": false, ["\\tnote", {"questions":["Unrelated?"]}]', []),
    ('broken "prefix [not an array]" "questions":["Keep me?"]', ["Keep me?"]),
    ('": false, "questions":["Keep me?"]', ["Keep me?"]),
    ('[{"assessment":{"ready":false,"questions":["Unrelated?"]}}]', []),
    ('```json\n[{"questions":["Unrelated?"]}]\n```', []),
    ('```json\n[{"assessment":{"ready":false,"questions":["Unrelated?"]}}]\n```', []),
    ('broken [{"questions":["Unrelated?"]}]', []),
    ('[{"questions":["Unrelated?"]}', []),
    ('broken [{"assessment":{"ready":true,"questions":["Unrelated?"]}}]', []),
    ('broken "questions":["Keep me?"], [{"questions":["Unrelated?"]}]', ["Keep me?"]),
    ('broken "questions":["Keep me?"]\n```json\n[{"questions":["Unrelated?"]}]\n```', ["Keep me?"]),
    ('intro [\n```json\n{"assessment":{"ready":false,"questions":["Keep me?"]}}\n```', ["Keep me?"]),
])
async def test_root_array_children_are_not_assessment_questions(output, questions):
    perf = performance(output)
    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        response = await handle_status(
            PerformerMessage(action="status", session_id="synthetic"), perf,
            Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=0),
        )
    assert response.status == ("blocked" if questions else "error")
    assert (response.questions or []) == questions
    assert "Unrelated?" not in str(perf.open_questions)
    commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("output,status", [
    ('intro "metadata": {\n```json\n{"assessment":{"ready":true,"questions":[]}}\n```', "assessment_complete"),
    ('intro "metadata": {\n```json\n{"assessment":{"ready":false,"verdict":"not_work","questions":[]}}\n```', "assessment_not_work"),
])
async def test_selected_fenced_assessment_keeps_its_workflow_outcome(output, status):
    perf = performance(output)
    with patch("performer.main.commit_file", new=AsyncMock()):
        response = await handle_status(
            PerformerMessage(action="status", session_id="synthetic"), perf,
            Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=0),
        )
    assert response.status == status
    assert not perf.open_questions


@pytest.mark.parametrize("raw,kind,selected", [
    (' {"sufficient":true} ', "full", ' {"sufficient":true} '),
    ('intro {example}\n```json\n{"sufficient":true}\n```', "fence", '{"sufficient":true}'),
    ('intro {"sufficient":true} tail', "substring", '{"sufficient":true}'),
])
def test_json_parser_reports_the_successful_candidate(raw, kind, selected):
    from performer.main import _extract_json

    candidates = []
    result = _extract_json(raw, candidate_callback=lambda *args: candidates.append(args))
    assert result == _extract_json(raw) == {"sufficient": True}
    assert len(candidates) == 1
    actual_kind, start, end = candidates[0]
    assert actual_kind == kind
    assert raw[start:end] == selected


@pytest.mark.asyncio
@pytest.mark.parametrize("contract,status", [
    ({"sufficient": True, "questions": []}, "assessment_complete"),
    ({"assessment": {"ready": True, "questions": []}}, "assessment_complete"),
    ({"assessment": {"ready": False, "verdict": "not_work", "questions": []}}, "assessment_not_work"),
])
async def test_metadata_key_string_preserves_valid_assessment(contract, status):
    perf = performance(json.dumps({'[{"value":"note"}]': "ignored", **contract}))
    with patch("performer.main.commit_file", new=AsyncMock()):
        response = await handle_status(
            PerformerMessage(action="status", session_id="synthetic"), perf,
            Settings(AGENT_BACKEND="claude_code", BACKEND_PARSE_RETRIES=0),
        )
    assert response.status == status
    assert not perf.open_questions
