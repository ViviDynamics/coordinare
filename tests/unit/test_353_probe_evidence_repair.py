"""Probe evidence corrections preserve commands and require inspected citations."""
from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_probes import ProbeReport
from coordinare.services.environment_proposal_patch import apply_patch
from coordinare.services.environment_response_schema import ProbeRepairSchema, response_schema
from tests.unit.scope_chat_fixture import discover_with_fixture_scope
from tests.unit.test_353_discovery_repair import (
    CAPABILITIES,
    COMMIT,
    PATH,
    SOURCE,
    assessment,
    candidate,
)
from tests.unit.test_353_environment_probes import report_data, request_data


def test_corrected_probe_schema_rejects_missing_evidence_and_unknown_ids():
    schema = response_schema('probe', probe_repair=ProbeRepairSchema(['tool'], [PATH]))
    valid = {'probe': request_data()}
    jsonschema.validate(valid, schema)
    for field, value in [('citations', {}), ('questions', {}), ('citations', {'tool': []}),
                         ('questions', {'tool': ''}), ('questions', {'tool': '  \t\n'}), ('citations', {'tool': ['unread.md']}),
                         ('questions', {'other': 'Invented ID'})]:
        invalid = copy.deepcopy(valid)
        invalid['probe'][field] = value
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_probe_evidence_patch_preserves_commands_and_cannot_modify_execution():
    base = {'probe': request_data()}
    original = copy.deepcopy(base)
    patch = {'tool': 'repair_proposal', 'changes': [
        {'path': ['probe', 'citations', 'tool', 0], 'value_json': '"new-evidence.md"'}]}
    result = apply_patch(base, patch, max_bytes=262144)
    assert result['probe']['citations'] == {'tool': ['new-evidence.md']}
    assert result['probe']['commands'] == original['probe']['commands']
    assert result['probe']['questions'] == original['probe']['questions']
    assert base == original
    for path in [['probe', 'commands', 0, 'argv'], ['probe', 'execution_context'], ['probe']]:
        with pytest.raises(EnvironmentDiscoveryError):
            apply_patch(base, {'tool': 'repair_proposal', 'changes': [{'path': path, 'value_json': '[]'}]}, max_bytes=262144)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_correction', [None, 'commands', 'empty_evidence', 'execution_context'])
async def test_unread_probe_citation_can_be_read_then_corrected_without_regenerating_plan(invalid_correction):
    new_path = 'nested/new-evidence.md'
    proposal = request_data()
    proposal['citations']['tool'] = [new_path]
    commands = copy.deepcopy(proposal['commands'])
    patch = {'tool': 'repair_proposal', 'changes': [
        {'path': ['probe', 'citations', 'tool'], 'value_json': json.dumps([new_path])}]}
    altered = copy.deepcopy(proposal)
    if invalid_correction == 'commands':
        altered['commands'][0]['argv'] = ['different-executable']
    elif invalid_correction == 'empty_evidence':
        altered['citations'] = {}
        altered['questions'] = {}
    elif invalid_correction == 'execution_context':
        altered['execution_context'] = 'private_service'
    replies = iter([{'tool': 'read_file', 'path': PATH}, {'probe': proposal},
        {'tool': 'read_file', 'path': new_path}, *([{'probe': altered}] if invalid_correction else []),
        patch, {'manifest': candidate()}, {'tool': 'read_file', 'path': PATH}, assessment(candidate())])
    feedback = []
    executed = []

    async def fetch(path):
        return SOURCE if path in {PATH, new_path} else None

    async def execute(plan):
        assert plan.inputs[new_path]
        assert plan.request.commands[0].argv == ['unfamiliar-tool', '--version']
        executed.append(plan)
        return ProbeReport.model_validate(report_data(plan))

    async def chat(messages, *, response_schema):
        for message in messages[-1:]:
            value = json.loads(message['content'])
            if 'validation_errors' in value:
                feedback.append(value)
        reply = next(replies)
        if reply == patch:
            invalid = {'probe': {**proposal, 'citations': {}, 'questions': {}}}
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(invalid, response_schema)
        return json.dumps(reply)

    budget = AttemptBudget()
    manifest, review = await discover_with_fixture_scope(symphony_name='suite', commit=COMMIT,
        paths=[PATH, new_path], fetch=fetch, chat=chat, capabilities=CAPABILITIES,
        budget=budget, probe_executor=execute)
    error = feedback[0]['validation_errors'][0]
    assert error['location'] == ['probe', 'citations', 'tool', 0]
    assert error['correction_path'] == error['location']
    assert new_path not in json.dumps(feedback[0])
    assert len(executed) == 1
    assert executed[0].request.commands[0].id == commands[0]['id']
    assert manifest.digest() == review.manifest_digest and manifest.checks[0].required
    assert budget.schema_repairs == 1 + int(invalid_correction is not None)


def test_probe_repair_wire_requires_the_same_evidence_keys_after_encoding():
    from coordinare.services.environment_probes import ProbeRequest
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema

    logical_schema = response_schema('probe', probe_repair=ProbeRepairSchema(['tool'], [PATH]))
    logical = {'probe': ProbeRequest.model_validate(request_data()).model_dump(mode='json')}
    logical['probe']['execution_context'] = 'outer'
    encoded = copy.deepcopy(logical)
    encoded['probe']['commands'][0]['env'] = []
    wire = {'response': encoded}
    jsonschema.validate(wire, wire_schema(logical_schema))
    assert json.loads(decode_wire(json.dumps(wire), logical_schema)) == logical
    for field in ['citations', 'questions']:
        invalid = copy.deepcopy(wire)
        invalid['response']['probe'][field] = {}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, wire_schema(logical_schema))
        with pytest.raises(ValueError):
            decode_wire(json.dumps(invalid), logical_schema)
