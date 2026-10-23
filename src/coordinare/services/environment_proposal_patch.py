"""Atomic, bounded field corrections; full proposal admission remains authoritative."""
from __future__ import annotations

import copy
import json
from typing import Any

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

MAX_CHANGES = 16
MAX_PATH_DEPTH = 8
_PATH = {'type': 'array', 'minItems': 1, 'maxItems': MAX_PATH_DEPTH,
         'items': {'anyOf': [{'type': 'string'}, {'type': 'integer', 'minimum': 0}]}}
_VALUE_JSON = {'type': 'string', 'description': 'A complete JSON-encoded replacement value, e.g. [] encoded as the string "[]".'}
_VALUE = {'description': 'The replacement value itself, as plain JSON; give value or value_json, not both.'}


def _patch_schema(record: dict[str, Any]) -> dict[str, Any]:
    return {'type': 'object', 'additionalProperties': False, 'required': ['tool', 'changes'],
            'properties': {'tool': {'const': 'repair_proposal'},
                           'changes': {'type': 'array', 'minItems': 1, 'maxItems': MAX_CHANGES, 'items': record}}}


PATCH_SCHEMA = _patch_schema({'type': 'object', 'additionalProperties': False, 'required': ['path', 'value_json'],
                              'properties': {'path': _PATH, 'value_json': _VALUE_JSON}})
# Where the schema is only described to the model (no provider enforcement), a record may carry its replacement as
# plain JSON in ``value``: a long shell command inside value_json is quoted three levels deep, and v26's author never
# closed one. Strict transports keep PATCH_SCHEMA, which cannot express an arbitrary JSON value.
TEXT_PATCH_SCHEMA = _patch_schema({'type': 'object', 'additionalProperties': False, 'required': ['path'],
                                   'properties': {'path': _PATH, 'value_json': _VALUE_JSON, 'value': _VALUE}})
PATCH_INSTRUCTION = (
    'A previous unambiguous complete declaration is available for correction. Prefer small correction records or source inspection requests, preserving unrelated records: '
    '{"tool":"repair_proposal","changes":[{"path":["finding_responses"],"value_json":"[]"}]}. '
    'Paths start with manifest, finding_responses, assessment, or probe as appropriate for the current declaration. '
    'Probe corrections may change only probe.citations and probe.questions; preserve every command and execution setting. '
    'For manifest field validation locations, prefix manifest unless the location already starts with finding_responses. '
    f'Use at most {MAX_CHANGES} correction records and paths of depth at most {MAX_PATH_DEPTH}. '
    'If more fields need correction, replace their common existing array or object once with its complete corrected value, '
    'preserving all unrelated records and required obligations. '
    'For large array or object replacements, return the complete corrected declaration in the expected envelope instead, '
    'preserving every unrelated record and obligation; this avoids encoding a large nested value inside value_json. '
    'Each value_json string encodes exactly one complete JSON replacement value; where the schema offers value, give the '
    'replacement there as plain JSON instead, which avoids escaping it twice. Array indices must identify existing entries; '
    'parents must exist. Corrections are applied atomically to the most recent unambiguous complete declaration, then fully revalidated. '
    'Do not split an invalid declaration across partial batches or mix a declaration with correction records. '
    'Required obligations, original budgets, independent review and required execution remain mandatory.'
)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            message = 'duplicate replacement JSON key'
            raise ValueError(message)
        result[key] = value
    return result


def _constant(_value: str) -> None:
    message = 'nonfinite replacement JSON value'
    raise ValueError(message)


def _index(node: Any, part: Any) -> Any:
    """A digit string naming an array entry can only mean that index (v27 wrote "17" for 17): a string is otherwise
    never valid on an array, so reading it as the index changes no correction's meaning."""
    if isinstance(node, list) and isinstance(part, str) and part.isascii() and part.isdecimal():
        return int(part)
    return part


def _replace(target: dict[str, Any], change: Any, roots: set[str]) -> None:
    if not isinstance(change, dict) or set(change) not in ({'path', 'value_json'}, {'path', 'value'}):
        message = 'invalid correction record'
        raise ValueError(message)
    path = change['path']
    if (not isinstance(path, list) or not 1 <= len(path) <= MAX_PATH_DEPTH
            or any(type(part) not in (str, int) for part in path) or path[0] not in roots):
        message = 'invalid correction path'
        raise ValueError(message)
    node: Any = target
    for raw in path[:-1]:
        part = _index(node, raw)
        if isinstance(node, list):
            if type(part) is not int or not 0 <= part < len(node):
                message = 'invalid correction index'
                raise ValueError(message)
        elif not isinstance(node, dict) or not isinstance(part, str):
            message = 'invalid correction parent'
            raise TypeError(message)
        node = node[part]
    key = _index(node, path[-1])
    if isinstance(node, list):
        if type(key) is not int or not 0 <= key < len(node):
            message = 'invalid correction index'
            raise ValueError(message)
    elif not isinstance(node, dict) or not isinstance(key, str):
        message = 'invalid correction key'
        raise TypeError(message)
    # A plain value was parsed with the reply itself, which already rejects duplicate keys; the corrected
    # declaration's size check rejects non-finite numbers.
    node[key] = (copy.deepcopy(change['value']) if 'value' in change
                 else json.loads(change['value_json'], object_pairs_hook=_unique, parse_constant=_constant))


def _apply(base: dict[str, Any] | None, request: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    if base is None:
        message = 'no unambiguous proposal to correct'
        raise ValueError(message)
    changes = request['changes']
    if (set(request) != {'tool','changes'} or request['tool'] != 'repair_proposal'
            or not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES):
        message = 'invalid correction request'
        raise ValueError(message)
    target = copy.deepcopy(base)
    roots = {'probe'} if 'probe' in base else ({'manifest', 'finding_responses'} if 'manifest' in base else {'assessment'})
    for change in changes:
        if 'probe' in base and (not isinstance(change, dict) or not isinstance(change.get('path'), list)
                or change['path'][:2] not in [['probe', 'citations'], ['probe', 'questions']]):
            message = 'probe corrections may change evidence only'
            raise ValueError(message)
        _replace(target, change, roots)
    if len(json.dumps(target, ensure_ascii=False, allow_nan=False).encode()) > max_bytes:
        message = 'corrected declaration exceeds bound'
        raise ValueError(message)
    return target


def apply_patch(base: dict[str, Any] | None, request: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    try:
        result = _apply(base, request, max_bytes)
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        message = 'proposal correction is invalid or exceeds its bound'
        raise EnvironmentDiscoveryError(message, code='invalid_proposal_patch') from None
    else:
        return result


def text_schema(schema: Any) -> Any:
    """The schema as described in a prompt-only transport: the patch tool also accepts plain ``value`` records."""
    if schema == PATCH_SCHEMA:
        return TEXT_PATCH_SCHEMA
    if isinstance(schema, dict):
        return {key: text_schema(value) for key, value in schema.items()}
    if isinstance(schema, list):
        return [text_schema(value) for value in schema]
    return schema
