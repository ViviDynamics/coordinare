"""Strict provider wire encoding preserves generic contract values and rejects ambiguity."""
from __future__ import annotations

import json

import jsonschema
import pytest

from coordinare.services.environment_response_schema import response_schema


def test_every_phase_schema_uses_supported_strict_object_representation():
    from coordinare.services.environment_structured_wire import wire_schema

    for phase in ['source_tool', 'probe', 'manifest', 'assessment', 'scope', 'scope_assessment', 'scope_revision']:
        schema = wire_schema(response_schema(phase))
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema['type'] == 'object' and 'anyOf' not in schema
        def visit(node):
            if isinstance(node, dict):
                assert 'default' not in node and 'const' not in node
                if node.get('type') == 'object':
                    assert node['additionalProperties'] is False
                    assert set(node['required']) == set(node['properties'])
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)
        visit(schema)


def test_dynamic_map_round_trip_preserves_names_values_and_empty_maps():
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema

    schema = {'type': 'object', 'properties': {'env': {'type': 'object', 'additionalProperties': {'type': 'string'}}},
              'required': ['env'], 'additionalProperties': False}
    data = {'response': {'env': [{'key': 'CUSTOM', 'value': '${repo}/a'}, {'key': 'OTHER', 'value': '{repo}/b'}]}}
    jsonschema.validate(data, wire_schema(schema))
    assert json.loads(decode_wire(json.dumps(data), schema)) == {'env': {'CUSTOM': '${repo}/a', 'OTHER': '{repo}/b'}}
    assert json.loads(decode_wire('{"response":{"env":[]}}', schema)) == {'env': {}}
    data['response']['env'].append({'key': 'CUSTOM', 'value': 'replacement'})
    with pytest.raises(ValueError):
        decode_wire(json.dumps(data), schema)


@pytest.mark.parametrize('raw', [
    '{"response":{"tool":"read_file","path":"one"},"response":{"tool":"read_file","path":"two"}}',
    '{"response":{"tool":"read_file","path":"one","extra":true}}',
    '{"tool":"read_file","path":"one"}',
])
def test_ambiguous_or_nonmatching_wire_never_extracts_a_payload(raw):
    from coordinare.services.environment_structured_wire import decode_wire

    with pytest.raises(ValueError):
        decode_wire(raw, response_schema('source_tool'))


@pytest.mark.parametrize('value', [{'tool': 'read_file', 'path': 'Makefile'},
                                  {'tool': 'read_files', 'paths': ['README']},
                                  {'tool': 'list_files', 'path': '.', 'cursor': 0}])
def test_real_source_tool_wire_validates_and_decodes(value):
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema

    schema = response_schema('source_tool')
    wire = {'response': value}
    jsonschema.validate(wire, wire_schema(schema))
    assert json.loads(decode_wire(json.dumps(wire), schema)) == value


def test_native_schema_preserves_execution_path_grammar_and_operator_variables():
    from coordinare.services.environment_discovery_prompts import _MANIFEST_ENVELOPE
    from coordinare.services.environment_structured_wire import wire_schema

    schema = wire_schema(_MANIFEST_ENVELOPE.model_json_schema())
    path_schema = schema['$defs']['Command']['properties']['cwd']
    for path in ['{repo}', '{cache}/bin', '${repo}/lib', '/tmp/work']:
        jsonschema.validate(path, path_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate('repo', path_schema)


def test_revision_schema_and_native_transport_have_identical_envelopes():
    from coordinare_service_inference.environment.scope_revision import ScopeRevisionEnvelope

    from coordinare.services.environment_function_wire import decode_function, functions

    schema = response_schema('scope_revision')
    declaration = schema['anyOf'][-1]
    assert set(declaration['required']) == {'scope_revision', 'finding_responses'}
    assert declaration['additionalProperties'] is False
    revision = schema['$defs']['ScopeRevision']
    assert set(revision['required']) == {'base_digest', 'add', 'reassess'}
    assert revision['additionalProperties'] is False
    assert revision['properties']['add']['maxItems'] == revision['properties']['reassess']['maxItems'] == 16
    reassessment = schema['$defs']['ScopeReassessment']
    assert set(reassessment['required']) == {'criterion_id', 'criterion'}
    assert reassessment['additionalProperties'] is False
    values = {'base_digest': 'a'*64, 'add': [], 'reassess': []}
    responses = [{'finding_id': 'F0001', 'rationale': 'Source evidence defends the existing definition',
                  'sources': ['guide'], 'affected_ids': []}]
    native = next(f['function'] for f in functions(schema) if f['function']['name'] == 'submit_environment_scope_revision')
    assert set(native['parameters']['required']) == {'base_digest', 'add', 'reassess', 'finding_responses'}
    raw = json.dumps({'tool_calls': [{'type': 'function', 'function': {'name': native['name'],
        'arguments': json.dumps({**values, 'finding_responses': responses})}}]})
    decoded = json.loads(decode_function(raw, schema))
    assert decoded == {'scope_revision': values, 'finding_responses': responses}
    assert ScopeRevisionEnvelope.model_validate(decoded).finding_responses[0].finding_id == 'F0001'
    assert 'ScopeEnvelope' not in schema.get('$defs', {})
    assert 'scope' in response_schema('scope')['anyOf'][-1]['properties']


@pytest.mark.parametrize('arguments', [
    '{"base_digest":"x","add":[],"add":[],"reassess":[],"finding_responses":[]}',
    '{"base_digest":"x","add":[],"reassess":[],"finding_responses":[],"scope_definition":{}}',
])
def test_invalid_native_revision_never_salvages_arguments(arguments):
    from coordinare_service_inference.environment.scope_revision import ScopeRevisionEnvelope

    from coordinare.services.environment_function_wire import decode_function

    raw = json.dumps({'tool_calls': [{'type': 'function', 'function': {
        'name': 'submit_environment_scope_revision', 'arguments': arguments}}]})
    with pytest.raises(ValueError):
        decoded = decode_function(raw, response_schema('scope_revision'))
        ScopeRevisionEnvelope.model_validate_json(decoded)


def test_revision_rejects_full_scope_function_and_redacts_unknown_fields():
    from coordinare_service_inference.environment.scope_revision import ScopeRevisionEnvelope
    from pydantic import ValidationError

    from coordinare.services.environment_admission import _validation_feedback
    from coordinare.services.environment_discovery_prompts import correction_instruction
    from coordinare.services.environment_function_wire import decode_function

    raw = json.dumps({'tool_calls': [{'type': 'function', 'function': {
        'name': 'submit_environment_scope', 'arguments': '{}'}}]})
    with pytest.raises(KeyError):
        decode_function(raw, response_schema('scope_revision'))
    with pytest.raises(ValidationError) as failure:
        ScopeRevisionEnvelope.model_validate({'scope_revision': {'base_digest': 'a'*64, 'add': [],
            'reassess': [], 'private-value': 'private-source'}, 'finding_responses': []})
    errors = _validation_feedback(failure.value)
    assert errors == [{'location': ['scope_revision', '*'], 'code': 'extra_forbidden'}]
    assert 'private' not in json.dumps(errors)
    instruction = correction_instruction('scope_revision')
    assert 'base_digest' in instruction and 'scope_revision' in instruction
    assert 'complete scope' in instruction and 'commands' in instruction


@pytest.mark.parametrize(('raw', 'code'), [
    ('{"scope":{"criteria":[]},"scope":"whole_documented_development_test_environment","finding_responses":[]}', 'duplicate_json_key'),
    ('{"scope_revision":{"base_digest":"'+'a'*64+'","add":[],"reassess":[]},"finding_responses":[],"scope_definition":{}}', 'extra_forbidden'),
])
def test_saved_elm_failure_shapes_remain_held(raw, code):
    from coordinare_service_inference.environment.scope_revision import ScopeRevisionEnvelope
    from pydantic import ValidationError

    from coordinare.services.environment_admission import _decode_reply, _validation_feedback
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_discovery import DiscoveryLimits

    try:
        data = _decode_reply(raw, 'scope_revision', DiscoveryLimits())
        ScopeRevisionEnvelope.model_validate(data)
    except EnvironmentDiscoveryError as exc:
        assert exc.code == code
    except ValidationError as exc:
        assert code in [error['code'] for error in _validation_feedback(exc)]
    else:
        pytest.fail('Malformed saved Elm response must never become an accepted revision')


@pytest.mark.parametrize('identifiers', [[], ['F0001']])
def test_scope_ledger_decision_constraints_survive_strict_wire_encoding(identifiers):
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema

    schema = response_schema('scope_assessment', review_decision_ids=identifiers)
    decision = {'finding_id': 'F0001', 'status': 'resolved', 'rationale': 'Inspected source resolves the finding',
                'sources': ['fixture'], 'duplicate_of': None, 'inspection_requests': []}
    assessment = {'verdict': 'accept', 'rationale': 'Source evidence covers the scope',
                  'sources': ['fixture'], 'findings': [], 'decisions': [decision] if identifiers else []}
    payload = {'response': {'scope_assessment': assessment}}
    jsonschema.validate(payload, wire_schema(schema))
    assert json.loads(decode_wire(json.dumps(payload), schema)) == {'scope_assessment': assessment}
    assessment['decisions'] = [{**decision, 'finding_id': 'F9999'}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, wire_schema(schema))


@pytest.mark.parametrize('inspection', [
    {'operation': 'read_file', 'path': 'unfamiliar/entry'},
    {'operation': 'list_files', 'path': '', 'cursor': 0},
])
def test_nonempty_scope_inspection_request_survives_strict_transport(inspection):
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema

    assessment = {'verdict': 'revise', 'rationale': 'Inspect nested source', 'sources': ['fixture'],
        'findings': [{'category': 'coverage', 'issue': 'Nested obligation needs inspection',
            'sources': ['fixture'], 'affected_ids': [], 'inspection_requests': [inspection]}], 'decisions': []}
    schema = response_schema('scope_assessment', review_decision_ids=[])
    encoded = {'response': {'scope_assessment': assessment}}
    assert json.loads(decode_wire(json.dumps(encoded), schema)) == {'scope_assessment': assessment}
    wire = wire_schema(schema)
    jsonschema.validate(encoded, wire)
    def visit(value):
        if isinstance(value, dict):
            assert 'oneOf' not in value and 'discriminator' not in value
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(wire)
    assessment['findings'][0]['inspection_requests'][0] = {'operation': 'read_file', 'path': 'nested', 'cursor': 0}
    with pytest.raises(ValueError):
        decode_wire(json.dumps(encoded), schema)
