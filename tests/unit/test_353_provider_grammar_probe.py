"""The opt-in provider grammar probe sends every review schema at its largest bounds, never secrets."""
from __future__ import annotations

import json

import pytest


def test_probe_requests_cover_every_structured_phase_at_maximum_bounds():
    from scripts.probe_provider_grammar import MAX_CRITERIA, MAX_DECISIONS, probe_requests

    from coordinare.services.environment_structured_wire import wire_schema

    requests = probe_requests('model-x')
    assert set(requests) == {'scope', 'scope_revision', 'scope_assessment', 'assessment', 'probe', 'manifest'}
    for body in requests.values():
        response_format = body['response_format']
        assert body['model'] == 'model-x' and response_format['type'] == 'json_schema'
        assert response_format['json_schema']['strict'] is True
        assert 'properties' in response_format['json_schema']['schema']
    scope = requests['scope_assessment']['response_format']['json_schema']['schema']
    wire = scope['$defs']['SourceDecisionWire']['properties']
    assert len(wire['finding_id']['enum']) == MAX_DECISIONS == 32
    assert len(wire['criterion_ids']['items']['enum']) == MAX_CRITERIA == 128
    from coordinare.services.environment_response_schema import (
        constrain_source_criteria,
        response_schema,
    )
    expected = constrain_source_criteria(response_schema('scope_assessment', review_decision_ids=[
        f'F{i:04}' for i in range(1, MAX_DECISIONS+1)]), [f'S{i:016x}' for i in range(MAX_CRITERIA)])
    assert scope == wire_schema(expected)


@pytest.mark.parametrize(('status', 'text', 'expected'), [
    (200, '{}', 'ok'),
    (400, '{"error":"The compiled grammar is too large, which would cause performance issues."}', 'grammar_too_large'),
    (400, '{"error":"other"}', 'http_400'),
])
def test_probe_outcome_is_classified_without_echoing_bodies(status, text, expected):
    from scripts.probe_provider_grammar import classify

    assert classify(status, text) == expected


def test_probe_cli_reads_the_key_only_from_the_named_environment_variable(monkeypatch, capsys):
    from scripts import probe_provider_grammar

    seen = {}

    def fake_post(url, body, key):
        seen['key'] = key
        return 200, '{}'
    monkeypatch.setattr(probe_provider_grammar, '_post', fake_post)
    monkeypatch.setenv('PROBE_TEST_KEY', 'secret-value')
    assert probe_provider_grammar.main(['--base-url', 'http://route/v1', '--model', 'model-x', '--key-env', 'PROBE_TEST_KEY']) == 0
    output = capsys.readouterr().out
    assert seen['key'] == 'secret-value' and 'secret-value' not in output
    assert json.loads(output)['scope_assessment'] == 'ok'
