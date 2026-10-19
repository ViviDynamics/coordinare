"""Strict provider wire encoding preserves generic contract values and rejects ambiguity."""
from __future__ import annotations

import json

import jsonschema
import pytest

from coordinare.services.environment_response_schema import response_schema


def test_every_phase_schema_uses_supported_strict_object_representation():
    from coordinare.services.environment_structured_wire import wire_schema

    for phase in ['source_tool', 'probe', 'manifest', 'assessment', 'scope', 'scope_assessment']:
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
