"""Phase-specific provider schemas; independent local validation remains authoritative."""
from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.scope import (
    ScopeAssessmentEnvelope,
    ScopeEnvelope,
    ShardSummaryEnvelope,
)
from coordinare_service_inference.environment.scope_revision import ScopeRevisionEnvelope

from coordinare.services.environment_discovery_prompts import (
    _MANIFEST_ENVELOPE,
    assessment_envelope_schema,
)
from coordinare.services.environment_evidence import check_evidence_context
from coordinare.services.environment_findings import MAX_FINDINGS
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


_SOURCE_DECISIONS = ('CoveredDecision', 'SourceCorrectionDecision', 'SourceGapDecision', 'DuplicateDecision')


def _flat_source_decision(definitions: dict[str, Any]) -> dict[str, Any]:
    """One wire object per decision: providers cannot compile the four-branch union's grammar.

    The disposition selects the strict branch; local validation rejects populated inapplicable fields.
    """
    covered, gap = definitions['CoveredDecision']['properties'], definitions['SourceGapDecision']['properties']
    duplicate = definitions['DuplicateDecision']['properties']
    properties = {
        'finding_id': covered['finding_id'], 'disposition': {'type': 'string', 'enum': [
            definitions[name]['properties']['disposition']['const'] for name in _SOURCE_DECISIONS]},
        'rationale': covered['rationale'], 'sources': covered['sources'],
        'criterion_ids': {'type': 'array', 'items': {'type': 'string'}},
        'gap_kind': {'anyOf': [{key: value for key, value in gap['gap_kind'].items() if key != 'title'}, {'type': 'null'}]},
        'affected_ids': {'type': 'array', 'items': {'type': 'string'}},
        'inspection_requests': gap['inspection_requests'],
        'duplicate_of': {'anyOf': [{key: value for key, value in duplicate['duplicate_of'].items() if key != 'title'},
                                   {'type': 'null'}]},
    }
    return {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(properties)}


def _source_review_constraints(definitions: dict[str, Any], identifiers: Sequence[str] | None) -> None:
    decisions = definitions['ScopeAssessmentProposal']['properties']['decisions']
    definitions['SourceDecisionWire'] = _flat_source_decision(definitions)
    for name in _SOURCE_DECISIONS:
        del definitions[name]
    decisions['items'] = {'$ref': '#/$defs/SourceDecisionWire'}
    if identifiers is not None:
        decisions.update(minItems=len(identifiers), maxItems=len(identifiers))
        definitions['ScopeAssessmentProposal']['properties']['findings']['maxItems'] = MAX_FINDINGS-len(identifiers)
        if identifiers:
            wire = definitions['SourceDecisionWire']['properties']
            wire['finding_id'] = {'type': 'string', 'enum': list(identifiers)}
            wire['duplicate_of'] = {'anyOf': [{'type': 'string', 'enum': list(identifiers)}, {'type': 'null'}]}


def constrain_source_criteria(schema: dict[str, Any], criterion_ids: Sequence[str]) -> dict[str, Any]:
    """Limit source review criterion references to the reviewed definition; local validation stays authoritative."""
    definitions = schema['$defs']
    for name, field in (('SourceDecisionWire', 'criterion_ids'), ('SourceDecisionWire', 'affected_ids'),
                        ('ScopeFindingProposal', 'affected_ids')):
        definitions[name]['properties'][field]['items'] = {'type': 'string', 'enum': list(criterion_ids)}
    return schema


def _review_constraints(declaration: dict[str, Any], phase: str, identifiers: Sequence[str] | None) -> None:
    definitions = declaration['$defs']
    if phase == 'scope_assessment':
        _source_review_constraints(definitions, identifiers)
        return
    assessment = definitions['CoverageAssessmentProposal']
    decision = definitions['FindingDecisionProposal']
    if identifiers is not None:
        assessment['properties']['decisions'].update(minItems=len(identifiers), maxItems=len(identifiers))
        assessment['properties']['findings']['maxItems'] = MAX_FINDINGS-len(identifiers)
        if identifiers:
            decision['properties']['finding_id']['enum'] = list(identifiers)
            decision['properties']['duplicate_of'] = {'anyOf': [
                {'type': 'string', 'enum': list(identifiers)}, {'type': 'null'}]}
        else:
            decision['properties']['duplicate_of'] = {'type': 'null'}
    open_decision, resolved_decision = deepcopy(decision), deepcopy(decision)
    open_decision['properties']['status'] = {'const': 'open', 'type': 'string'}
    open_decision['properties']['duplicate_of'] = {'type': 'null'}
    resolved_decision['properties']['status'] = {'const': 'resolved', 'type': 'string'}
    definitions['FindingDecisionProposal'] = {'anyOf': [open_decision, resolved_decision]}


def response_schema(phase: str, *, allow_patch: bool = False, scope_contract: ScopeContract | None = None,
                    review_decision_ids: Sequence[str] | None = None,
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
             'scope_assessment': ScopeAssessmentEnvelope.model_json_schema, 'scope_revision': ScopeRevisionEnvelope.model_json_schema,
             'shard_summary': ShardSummaryEnvelope.model_json_schema}
    if phase == 'source_tool':
        return {'type': 'object', 'anyOf': tools}
    declaration = final[phase]()
    if phase in {'scope_assessment', 'assessment'}:
        _review_constraints(declaration, phase, review_decision_ids)
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
                      *, schema: dict[str, Any], max_context_bytes: int | None = None,
                      context_phase: str = "discovery") -> str | DiscoveryChatReply:
    """Pass schema and final-context admission through capable transport boundaries."""
    if max_context_bytes is not None:
        check_evidence_context(messages, max_bytes=max_context_bytes, phase=context_phase)
    parameters = inspect.signature(chat).parameters
    kwargs: dict[str, Any] = {}
    if 'response_schema' in parameters:
        kwargs['response_schema'] = schema
    if 'max_context_bytes' in parameters:
        kwargs['max_context_bytes'] = max_context_bytes
    if 'context_phase' in parameters:
        kwargs['context_phase'] = context_phase
    return await chat(messages, **kwargs)
