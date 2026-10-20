"""Phase-specific provider schemas; independent local validation remains authoritative."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.scope import ScopeAssessmentEnvelope, ScopeEnvelope
from coordinare_service_inference.environment.scope_revision import ScopeRevisionEnvelope

from coordinare.services.environment_discovery_prompts import (
    _MANIFEST_ENVELOPE,
    assessment_envelope_schema,
)
from coordinare.services.environment_probes import ProbeEnvelope
from coordinare.services.environment_proposal_patch import PATCH_SCHEMA

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from coordinare_service_inference.environment.scope import ScopeContract

    from coordinare.services.environment_attempt import DiscoveryChatReply


def _require_scope_mapping(declaration: dict[str, Any], scope: ScopeContract) -> None:
    identifiers = [criterion.id for criterion in scope.definition.criteria if criterion.applicability == 'required']
    manifest = declaration['$defs']['EnvironmentManifestProposal']
    manifest['required'].append('criterion_checks')
    manifest['properties']['criterion_checks'] = {
        'type': 'object', 'additionalProperties': False, 'required': identifiers,
        'properties': {identifier: {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}}
                       for identifier in identifiers},
    }


@dataclass(frozen=True)
class ProbeRepairSchema:
    command_ids: Sequence[str]
    inspected_sources: Sequence[str]


def response_schema(phase: str, *, allow_patch: bool = False, scope_contract: ScopeContract | None = None,
                    scope_decision_ids: Sequence[str] | None = None,
                    probe_repair: ProbeRepairSchema | None = None) -> dict[str, Any]:
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
             'assessment': assessment_envelope_schema, 'scope': ScopeEnvelope.model_json_schema,
             'scope_assessment': ScopeAssessmentEnvelope.model_json_schema, 'scope_revision': ScopeRevisionEnvelope.model_json_schema}
    if phase == 'source_tool':
        return {'type': 'object', 'anyOf': tools}
    declaration = final[phase]()
    if phase == 'scope_assessment' and scope_decision_ids is not None:
        decisions = declaration['$defs']['ScopeAssessmentProposal']['properties']['decisions']
        decisions.update(minItems=len(scope_decision_ids), maxItems=len(scope_decision_ids))
        if scope_decision_ids:
            declaration['$defs']['FindingDecisionProposal']['properties']['finding_id']['enum'] = list(scope_decision_ids)
    if phase == 'probe' and probe_repair is not None:
        properties = declaration['$defs']['ProbeRequest']['properties']
        citation = {'type': 'array', 'minItems': 1, 'items': {'type': 'string', **(
            {'enum': list(probe_repair.inspected_sources)} if probe_repair.inspected_sources else {})}}
        question = {'type': 'string', 'minLength': 1, 'maxLength': 512, 'pattern': r'\S'}
        for name, value in [('citations', citation), ('questions', question)]:
            properties[name] = {'type': 'object', 'additionalProperties': False,
                'required': list(probe_repair.command_ids), 'properties': dict.fromkeys(probe_repair.command_ids, value)}
    if phase == 'manifest' and scope_contract is not None:
        _require_scope_mapping(declaration, scope_contract)
    definitions = declaration.pop('$defs', {})
    return {'type': 'object', 'anyOf': [*tools, declaration], '$defs': definitions}


async def invoke_chat(chat: Callable[..., Awaitable[str | DiscoveryChatReply]], messages: list[dict[str, str]],
                      *, schema: dict[str, Any]) -> str | DiscoveryChatReply:
    """Older injected transports keep their signature; API transports receive schemas."""
    if 'response_schema' in inspect.signature(chat).parameters:
        return await chat(messages, response_schema=schema)
    return await chat(messages)
