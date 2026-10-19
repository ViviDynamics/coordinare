"""Explicit native function transport; action admission remains in discovery."""
from __future__ import annotations

import copy
import json
from typing import Any

MAX_SCHEMA_DEPTH = 40
MAX_REPLY_BYTES = 256 * 1024

INSTRUCTION = (
    'Transport: call exactly one provided function with object arguments. Do not return ordinary content '
    'or JSON envelopes. Source functions take path/paths/cursor directly; submission functions take the '
    'probe, manifest (plus finding_responses), or assessment fields directly. This changes representation '
    'only: all pinned-source, review, execution, secret and readiness rules remain mandatory.'
)


def _declarations(schema: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    declarations = {}
    definitions = schema.get('$defs', {})
    for branch in schema['anyOf']:
        properties = branch['properties']
        if 'tool' in properties:
            name = properties['tool']['const']
            parameters = copy.deepcopy(branch)
            parameters['properties'].pop('tool')
            parameters['required'].remove('tool')
            kind = name
        else:
            kind = next(key for key in properties if key != 'finding_responses')
            name = 'submit_environment_' + kind
            parameters = copy.deepcopy(definitions[properties[kind]['$ref'].removeprefix('#/$defs/')])
            if kind in {'manifest', 'scope'}:
                parameters['properties']['finding_responses'] = copy.deepcopy(properties['finding_responses'])
                parameters.setdefault('required', []).append('finding_responses')
        parameters['$defs'] = copy.deepcopy(definitions)
        declarations[name] = (kind, parameters)
    return declarations


def _inline(value: Any, definitions: dict[str, Any], depth: int = 0) -> Any:
    """Expose argument types to function parsers that do not resolve local refs."""
    if depth > MAX_SCHEMA_DEPTH:
        message = 'recursive function schema exceeds bound'
        raise ValueError(message)
    if isinstance(value, list):
        return [_inline(item, definitions, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    if '$ref' in value:
        target = definitions[value['$ref'].removeprefix('#/$defs/')]
        return _inline({**target, **{key: item for key, item in value.items() if key != '$ref'}}, definitions, depth + 1)
    return {key: _inline(item, definitions, depth + 1) for key, item in value.items() if key != '$defs'}


def functions(schema: dict[str, Any]) -> list[dict[str, Any]]:
    return [{'type': 'function', 'function': {'name': name, 'description': (
        'Read immutable source evidence.' if kind in {'read_file', 'read_files', 'list_files'} else
        'Submit the phase declaration for local validation; this grants no readiness or execution permission.'),
        'parameters': _inline(parameters, parameters['$defs']), 'strict': False}}
        for name, (kind, parameters) in _declarations(schema).items()]


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            message = 'duplicate JSON key'
            raise ValueError(message)
        result[key] = value
    return result


def load_unique(raw: str) -> Any:
    return json.loads(raw, object_pairs_hook=_unique)


def decode_function(raw: str, schema: dict[str, Any]) -> str:
    if len(raw.encode()) > MAX_REPLY_BYTES:
        message = 'function response exceeds bound'
        raise ValueError(message)
    message = load_unique(raw)
    if not isinstance(message, dict):
        error = 'object message required'
        raise TypeError(error)
    if message.get('content') not in (None, '') and (not isinstance(message['content'], str) or message['content'].strip()):
        message = 'ambiguous function response'
        raise ValueError(message)
    calls = message.get('tool_calls')
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict) or calls[0].get('type') != 'function':
        message = 'exactly one function call required'
        raise ValueError(message)
    function = calls[0]['function']
    declarations = _declarations(schema)
    kind, _ = declarations[function['name']]
    arguments = load_unique(function['arguments'])
    if not isinstance(arguments, dict):
        error = 'object arguments required'
        raise TypeError(error)
    if kind in {'read_file', 'read_files', 'list_files', 'repair_proposal'}:
        if 'tool' in arguments:
            message = 'reserved action selector'
            raise ValueError(message)
        canonical = {'tool': kind, **arguments}
    elif kind in {'manifest', 'scope'}:
        if 'finding_responses' not in arguments:
            message = 'finding responses required'
            raise ValueError(message)
        findings = arguments.pop('finding_responses')
        canonical = {kind: arguments, 'finding_responses': findings}
    else:
        canonical = {kind: arguments}
    return json.dumps(canonical)
