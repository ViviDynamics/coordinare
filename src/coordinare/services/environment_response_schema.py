"""Phase-specific provider schemas; independent local validation remains authoritative."""
from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from coordinare.services.environment_discovery_prompts import (
    _MANIFEST_ENVELOPE,
    assessment_envelope_schema,
)
from coordinare.services.environment_probes import ProbeEnvelope
from coordinare.services.environment_proposal_patch import PATCH_SCHEMA

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from coordinare.services.environment_attempt import DiscoveryChatReply


def response_schema(phase: str, *, allow_patch: bool = False) -> dict[str, Any]:
    tools = [
        {'type': 'object', 'additionalProperties': False, 'required': ['tool', 'path'],
         'properties': {'tool': {'const': 'read_file'}, 'path': {'type': 'string'}}},
        {'type': 'object', 'additionalProperties': False, 'required': ['tool', 'paths'],
         'properties': {'tool': {'const': 'read_files'}, 'paths': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {'type': 'string'}}}},
        {'type': 'object', 'additionalProperties': False, 'required': ['tool', 'path', 'cursor'],
         'properties': {'tool': {'const': 'list_files'}, 'path': {'type': 'string'}, 'cursor': {'type': 'integer', 'minimum': 0}}},
    ]
    if allow_patch:
        tools.append(PATCH_SCHEMA)
    final: dict[str, Callable[[], dict[str, Any]]] = {'probe': ProbeEnvelope.model_json_schema, 'manifest': _MANIFEST_ENVELOPE.model_json_schema,
             'assessment': assessment_envelope_schema}
    if phase == 'source_tool':
        return {'type': 'object', 'anyOf': tools}
    declaration = final[phase]()
    definitions = declaration.pop('$defs', {})
    return {'type': 'object', 'anyOf': [*tools, declaration], '$defs': definitions}


async def invoke_chat(chat: Callable[..., Awaitable[str | DiscoveryChatReply]], messages: list[dict[str, str]],
                      *, schema: dict[str, Any]) -> str | DiscoveryChatReply:
    """Older injected transports keep their signature; API transports receive schemas."""
    if 'response_schema' in inspect.signature(chat).parameters:
        return await chat(messages, response_schema=schema)
    return await chat(messages)
