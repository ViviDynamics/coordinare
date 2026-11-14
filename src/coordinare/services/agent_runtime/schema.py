"""Schema adaptations preserve assertions while accommodating the pinned runtime."""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from nare.schema import UnsupportedSchema, check_supported

from coordinare.services.agent_runtime.errors import refuse_agent

if TYPE_CHECKING:
    from coordinare.services.agent_runtime.models import JsonObject


def native_tool_schema(schema: JsonObject) -> JsonObject:
    """Inline local references because nare checks each tool property in isolation."""
    def expand(node: JsonObject, seen: frozenset[str] = frozenset()) -> JsonObject:
        node = deepcopy(node)
        if '$ref' in node:
            ref = node.pop('$ref')
            if not isinstance(ref, str) or not ref.startswith('#/') or ref in seen:
                message = 'Unsupported tool schema reference'
                raise ValueError(message)
            target: Any = schema
            for part in unquote(ref[2:]).split('/'):
                target = target[part.replace('~1', '/').replace('~0', '~')]
            resolved = expand(target, seen | {ref})
            return {'allOf': [resolved, expand(node, seen)]} if node else resolved
        node.pop('$defs', None)
        node.pop('definitions', None)
        for keyword in ('properties',):
            if isinstance(mapping := node.get(keyword), dict):
                node[keyword] = {key: expand(value, seen) for key, value in mapping.items()}
        for keyword in ('oneOf', 'anyOf', 'allOf'):
            if isinstance(branches := node.get(keyword), list):
                node[keyword] = [expand(value, seen) for value in branches]
        for keyword in ('items', 'additionalProperties'):
            if isinstance(value := node.get(keyword), dict):
                node[keyword] = expand(value, seen)
        return node

    try:
        result = expand(schema)
        check_supported(result)
    except (UnsupportedSchema, ValueError, KeyError, TypeError, RecursionError):
        refuse_agent('Tool schema cannot preserve its constraints.',
                                        code='agent_schema_unsupported')
    return result
