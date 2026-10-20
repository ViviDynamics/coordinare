"""Atomic, bounded field corrections; full proposal admission remains authoritative."""
from __future__ import annotations

import copy
import json
from typing import Any

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

MAX_CHANGES = 16
MAX_PATH_DEPTH = 8
PATCH_SCHEMA = {'type':'object','additionalProperties':False,'required':['tool','changes'],
    'properties': {'tool':{'const':'repair_proposal'}, 'changes':{'type':'array','minItems':1,'maxItems':MAX_CHANGES,
        'items':{'type':'object','additionalProperties':False,'required':['path','value_json'],
            'properties':{'path':{'type':'array','minItems':1,'maxItems':MAX_PATH_DEPTH,
                'items':{'anyOf':[{'type':'string'},{'type':'integer','minimum':0}]}},
                'value_json':{'type':'string','description':'A complete JSON-encoded replacement value, e.g. [] encoded as the string "[]".'}}}}}}
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
    'Each value_json string encodes exactly one complete JSON replacement value. Array indices must identify existing entries; '
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


def _replace(target: dict[str, Any], change: Any, roots: set[str]) -> None:
    if not isinstance(change, dict) or set(change) != {'path','value_json'}:
        message = 'invalid correction record'
        raise ValueError(message)
    path = change['path']
    if (not isinstance(path, list) or not 1 <= len(path) <= MAX_PATH_DEPTH
            or any(type(part) not in (str, int) for part in path) or path[0] not in roots):
        message = 'invalid correction path'
        raise ValueError(message)
    node: Any = target
    for part in path[:-1]:
        if isinstance(node, list):
            if type(part) is not int or not 0 <= part < len(node):
                message = 'invalid correction index'
                raise ValueError(message)
        elif not isinstance(node, dict) or not isinstance(part, str):
            message = 'invalid correction parent'
            raise TypeError(message)
        node = node[part]
    key = path[-1]
    if isinstance(node, list):
        if type(key) is not int or not 0 <= key < len(node):
            message = 'invalid correction index'
            raise ValueError(message)
    elif not isinstance(node, dict) or not isinstance(key, str):
        message = 'invalid correction key'
        raise TypeError(message)
    node[key] = json.loads(change['value_json'], object_pairs_hook=_unique, parse_constant=_constant)


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
                or len(change['path']) < 2 or change['path'][:2] not in [['probe', 'citations'], ['probe', 'questions']]):
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
