"""Strict JSON-schema wire representation with lossless, ambiguity-rejecting map decoding."""
from __future__ import annotations

import json
from typing import Any

WIRE_INSTRUCTION = (
    'Transport encoding: return exactly one object with a response property containing your source-tool or final envelope. '
    'Follow the supplied response_format schema for wire shape. Dynamic dictionaries such as env, citations and questions '
    'are arrays of {key,value} entries with unique keys; use [] for an empty dictionary. '
    'All named properties must be supplied, including defaults and null optional values. '
    'This encoding changes representation only; it grants no readiness or execution authority.'
)


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def _compile(node: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in node.items()
              if key not in {'default', 'title', '$defs', 'properties', 'additionalProperties', 'required', 'items', 'anyOf', 'oneOf', 'discriminator', 'const'}}
    if 'default' in node:
        result['description'] = str(node.get('description', '')) + ' Declared logical default: ' + json.dumps(node['default'])
    if 'const' in node:
        result['enum'] = [node['const']]
        if 'type' not in result:
            result['type'] = {str: 'string', int: 'integer', bool: 'boolean'}[type(node['const'])]
    if 'anyOf' in node or 'oneOf' in node:
        result['anyOf'] = [_compile(branch) for branch in node.get('anyOf', node.get('oneOf', []))]
        if 'properties' not in node:
            result.pop('type', None)
            return result
    if node.get('type') == 'object':
        additional = node.get('additionalProperties', False)
        if isinstance(additional, dict):
            result.update(type='array', items=_object_schema({'key': {'type': 'string'}, 'value': _compile(additional)}))
        else:
            if additional is not False:
                message = 'untyped dictionary schemas are unsupported'
                raise ValueError(message)
            result.update(_object_schema({key: _compile(value) for key, value in node.get('properties', {}).items()}))
    if 'items' in node:
        result['items'] = _compile(node['items'])
    return result


def wire_schema(schema: dict[str, Any]) -> dict[str, Any]:
    result = _object_schema({'response': _compile(schema)})
    if schema.get('$defs'):
        result['$defs'] = {key: _compile(value) for key, value in schema['$defs'].items()}
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            message = 'duplicate wire JSON key'
            raise ValueError(message)
        result[key] = value
    return result


def _decode_map(value: Any, schema: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, list):
        message = 'wire dictionary must be an entry list'
        raise TypeError(message)
    result = {}
    for entry in value:
        if (not isinstance(entry, dict) or set(entry) != {'key', 'value'}
                or not isinstance(entry['key'], str) or entry['key'] in result):
            message = 'invalid or duplicate wire dictionary entry'
            raise ValueError(message)
        result[entry['key']] = _decode(entry['value'], schema, definitions)
    return result


def _decode_union(value: Any, branches: list[dict[str, Any]], definitions: dict[str, Any]) -> Any:
    matches = []
    for branch in branches:
        try:
            matches.append(_decode(value, branch, definitions))
        except (ValueError, TypeError):
            continue
    if len(matches) != 1:
        message = 'wire value does not identify exactly one schema branch'
        raise ValueError(message)
    return matches[0]


def _decode(value: Any, schema: dict[str, Any], definitions: dict[str, Any]) -> Any:
    if '$ref' in schema:
        return _decode(value, definitions[schema['$ref'].removeprefix('#/$defs/')], definitions)
    if 'anyOf' in schema or 'oneOf' in schema:
        return _decode_union(value, schema.get('anyOf', schema.get('oneOf', [])), definitions)
    if ('const' in schema and value != schema['const']) or ('enum' in schema and value not in schema['enum']):
        message = 'wire discriminator does not match schema'
        raise ValueError(message)
    kind = schema.get('type')
    if kind == 'object':
        additional = schema.get('additionalProperties', False)
        if isinstance(additional, dict):
            return _decode_map(value, additional, definitions)
        properties = schema.get('properties', {})
        if not isinstance(value, dict) or set(value) != set(properties):
            message = 'wire object has missing or extra properties'
            raise ValueError(message)
        return {key: _decode(item, properties[key], definitions) for key, item in value.items()}
    if kind == 'array':
        if not isinstance(value, list):
            message = 'wire value is not an array'
            raise ValueError(message)
        return [_decode(item, schema['items'], definitions) for item in value]
    allowed_types: dict[str, tuple[type, ...]] = {
        'string': (str,), 'integer': (int,), 'boolean': (bool,), 'number': (int, float), 'null': (type(None),)}
    literal_only = kind is None and ('const' in schema or 'enum' in schema)
    if not literal_only and type(value) not in allowed_types.get(str(kind), ()):
        message = 'wire scalar type does not match schema'
        raise ValueError(message)
    return value


def decode_wire(text: str, schema: dict[str, Any]) -> str:
    if len(text.encode()) > 256 * 1024:
        message = 'wire output limit exceeded'
        raise ValueError(message)
    value = json.loads(text, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != {'response'}:
        message = 'wire response requires one explicit envelope'
        raise ValueError(message)
    decoded = _decode(value['response'], schema, schema.get('$defs', {}))
    return json.dumps(decoded, ensure_ascii=False, separators=(',', ':'))
