"""Discovery schemas reach the provider without weakening local acceptance."""
from __future__ import annotations

import json

import httpx
import jsonschema
import pytest

from coordinare.services.conducting import OpenAiApiBackend


@pytest.mark.asyncio
async def test_backend_sends_exact_schema_per_request_and_keeps_legacy_json_mode(monkeypatch):
    requests = []
    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(answer), **kwargs))
    backend = OpenAiApiBackend(api_key='fixture', base_url='https://fixture.invalid/v1')
    schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    await backend.chat_json_with_metadata([{'role': 'user', 'content': 'first'}], response_schema=schema)
    await backend.chat_json_with_metadata([{'role': 'user', 'content': 'second'}])
    from coordinare.services.environment_structured_wire import wire_schema

    assert requests[0]['response_format'] == {'type': 'json_schema', 'json_schema': {
        'name': 'coordinare_environment_response', 'strict': True, 'schema': wire_schema(schema)}}
    assert requests[1]['response_format'] == {'type': 'json_object'}


def test_phase_schemas_reject_known_bad_shapes_without_losing_source_tools():
    from coordinare.services.environment_response_schema import response_schema

    for phase in ['source_tool', 'probe', 'manifest', 'assessment']:
        schema = response_schema(phase)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate({'tool': 'read_files', 'paths': ['Makefile']}, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'assessment': True}, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'tool': 'read_files', 'paths': ['Makefile'], 'assessment': True}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'probe': {}}, response_schema('assessment'))


@pytest.mark.asyncio
async def test_real_discovery_passes_phase_schema_through_optional_chat_boundary():
    from tests.unit.test_351_environment_discovery import PATH
    from tests.unit.test_353_discovery_repair import Replies, assessment, candidate, run

    valid = candidate()
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': valid},
                       {'tool': 'read_file', 'path': PATH}, assessment(valid)])
    schemas = []
    async def chat(messages, *, response_schema=None):
        assert response_schema is not None
        schemas.append(response_schema)
        return await replies(messages)
    manifest, review = await run(chat)
    assert review.manifest_digest == manifest.digest()
    assert schemas[0] == schemas[1]
    assert schemas[2] == schemas[3]
    assert schemas[1] != schemas[2]


@pytest.mark.asyncio
async def test_unsupported_schema_endpoint_never_silently_downgrades(monkeypatch):
    count = 0
    def answer(request):
        nonlocal count
        count += 1
        return httpx.Response(400, json={'error': {'type': 'unsupported_response_format'}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(answer), **kwargs))
    backend = OpenAiApiBackend(api_key='fixture', base_url='https://fixture.invalid/v1')
    reply = await backend.chat_json_with_metadata([{'role': 'user', 'content': 'fixture'}], response_schema={'type': 'object'})
    assert reply.error_code == 'provider_request_failed'
    assert not reply.text
    assert count == 1


def test_mixed_assessment_envelope_is_not_selected_or_silently_unwrapped():
    from coordinare.services.environment_response_schema import response_schema
    from tests.unit.test_353_discovery_repair import assessment, candidate

    wrapped = assessment(candidate())
    malformed = {**wrapped, **wrapped['assessment']}
    jsonschema.validate(wrapped, response_schema('assessment'))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(malformed, response_schema('assessment'))


@pytest.mark.asyncio
async def test_native_wire_reply_is_decoded_without_losing_raw_provenance(monkeypatch):
    from coordinare.services.environment_response_schema import response_schema

    raw = '{"response":{"tool":"read_file","path":"fixture"}}'
    def answer(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(answer), **kwargs))
    backend = OpenAiApiBackend(api_key='fixture', base_url='https://fixture.invalid/v1')
    reply = await backend.chat_json_with_metadata([{'role': 'user', 'content': 'fixture'}], response_schema=response_schema('source_tool'))
    assert json.loads(reply.text) == {'tool': 'read_file', 'path': 'fixture'}
    assert reply.wire_text == raw
    assert reply.error_code is None


@pytest.mark.asyncio
async def test_transport_instruction_follows_logical_schema_without_mutating_messages(monkeypatch):
    from coordinare.services.environment_response_schema import response_schema
    from coordinare.services.environment_structured_wire import WIRE_INSTRUCTION

    messages = [{'role': 'system', 'content': 'Logical syntax: {tool: read_file}.'}, {'role': 'user', 'content': 'Read fixture.'}]
    original_messages = json.loads(json.dumps(messages))
    def answer(request):
        sent = json.loads(request.content)['messages']
        assert sent[0]['content'].startswith(messages[0]['content'])
        assert sent[0]['content'].endswith(WIRE_INSTRUCTION)
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"response":{"tool":"read_file","path":"fixture"}}'}, 'finish_reason': 'stop'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(answer), **kwargs))
    backend = OpenAiApiBackend(api_key='fixture', base_url='https://fixture.invalid/v1')
    reply = await backend.chat_json_with_metadata(messages, response_schema=response_schema('source_tool'))
    assert reply.error_code is None
    assert messages == original_messages


@pytest.mark.asyncio
async def test_reviewed_scope_mapping_schema_requires_exact_ids_in_every_transport():
    import copy

    from coordinare_service_inference.environment.models import EnvironmentManifestProposal
    from coordinare_service_inference.environment.scope import ScopeContract

    from coordinare.services.environment_function_wire import decode_function, functions
    from coordinare.services.environment_response_schema import response_schema
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema
    from tests.unit.test_353_scope_mapping import mapped_manifest

    data, identifier = await mapped_manifest()
    scope = ScopeContract.model_validate(data['scope_contract'])
    proposal = EnvironmentManifestProposal.model_validate({
        key: value for key, value in data.items() if key in EnvironmentManifestProposal.model_fields
    }).model_dump(mode='json')
    logical = {'manifest': proposal, 'finding_responses': []}
    schema = response_schema('manifest', scope_contract=scope, allow_patch=True)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(logical, schema)
    for mapping in [None, {}, {identifier: []}, {identifier: ['operational'], 'invented': ['operational']}]:
        malformed = copy.deepcopy(logical)
        if mapping is None:
            malformed['manifest'].pop('criterion_checks')
        else:
            malformed['manifest']['criterion_checks'] = mapping
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(malformed, schema)

    declarations = {item['function']['name']: item['function'] for item in functions(schema)}
    arguments = {**proposal, 'finding_responses': []}
    parameters = declarations['submit_environment_manifest']['parameters']
    jsonschema.validate(arguments, parameters)
    missing = copy.deepcopy(arguments)
    missing.pop('criterion_checks')
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing, parameters)
    message = {'content': None, 'tool_calls': [{'type': 'function', 'function': {
        'name': 'submit_environment_manifest', 'arguments': json.dumps(arguments),
    }}]}
    assert json.loads(decode_function(json.dumps(message), schema)) == logical

    encoded = copy.deepcopy(logical)
    encoded['manifest']['activation']['env'] = []
    for check in encoded['manifest']['checks']:
        check['env'] = []
    wire = {'response': encoded}
    jsonschema.validate(wire, wire_schema(schema))
    assert json.loads(decode_wire(json.dumps(wire), schema)) == logical
    # Binding one request must not alter schemas for unrelated/unscoped callers.
    generic = response_schema('manifest')
    assert 'criterion_checks' not in generic['$defs']['EnvironmentManifestProposal']['required']
    assert response_schema('assessment', scope_contract=scope) == response_schema('assessment')


@pytest.mark.asyncio
async def test_canonical_json_http_request_delivers_the_current_scope_bound_schema(monkeypatch):
    from coordinare_service_inference.environment.scope import ScopeContract

    from coordinare.services.environment_response_schema import response_schema
    from tests.unit.test_353_scope_mapping import mapped_manifest

    data, identifier = await mapped_manifest()
    scope = ScopeContract.model_validate(data['scope_contract'])
    schema = response_schema('manifest', scope_contract=scope, allow_patch=True)
    messages = [{'role': 'system', 'content': 'Earlier generic phase guidance.'},
                {'role': 'user', 'content': 'Construct the complete declaration.'}]
    original_messages = json.loads(json.dumps(messages))
    requests = []
    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {
            'content': '{"tool":"read_file","path":"guide"}'}, 'finish_reason': 'stop'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(answer), **kwargs))
    backend = OpenAiApiBackend(api_key='fixture', base_url='https://fixture.invalid/v1', structured_transport='json_object')
    reply = await backend.chat_json_with_metadata(messages, response_schema=schema)
    assert reply.error_code is None
    assert json.loads(reply.text) == {'tool': 'read_file', 'path': 'guide'}
    payload = requests[0]
    assert payload['response_format'] == {'type': 'json_object'}
    system = payload['messages'][0]['content']
    marker = 'Authoritative logical response schema for this request:\n'
    assert marker in system
    delivered = json.loads(system.split(marker, 1)[1])
    assert delivered == schema
    mapping = delivered['$defs']['EnvironmentManifestProposal']['properties']['criterion_checks']
    assert mapping['required'] == [identifier]
    assert messages == original_messages


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['json_schema', 'json_object', 'tool_call'])
async def test_final_transport_context_is_admitted_before_api_call(monkeypatch, transport):
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_evidence import check_evidence_context
    from coordinare.services.environment_response_schema import invoke_chat, response_schema

    requests = []
    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(answer), **kwargs))
    backend = OpenAiApiBackend(api_key='fixture', base_url='https://fixture.invalid/v1', structured_transport=transport)
    schema = response_schema('scope')
    messages = [{'role': 'system', 'content': 'Return one scope. Schema: '+json.dumps(schema)}]
    admitted = check_evidence_context(messages, max_bytes=100000, phase='scope_author')
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await invoke_chat(backend.chat_json_with_metadata, messages, schema=schema,
                          max_context_bytes=admitted, context_phase='scope_author')
    assert held.value.code == 'scope_author_context_limit'
    assert requests == []
    assert messages[0]['content'] == 'Return one scope. Schema: '+json.dumps(schema)

    await invoke_chat(backend.chat_json_with_metadata, messages, schema=schema,
                      max_context_bytes=100000, context_phase='scope_author')
    assert len(requests) == 1
    final_size = check_evidence_context(requests[0]['messages'], max_bytes=100000, phase='scope_author')
    assert final_size > admitted
    await invoke_chat(backend.chat_json_with_metadata, messages, schema=schema,
                      max_context_bytes=final_size, context_phase='scope_author')
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_scope_phase_delivers_its_limit_and_preserves_context_hold():
    from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
    from coordinare.services.environment_evidence import check_evidence_context
    from tests.unit.test_353_scope_contract import criterion, proposal, review, run

    values = iter([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()])
    observed = []
    async def chat(messages, *, response_schema, max_context_bytes, context_phase):
        observed.append((max_context_bytes, context_phase))
        if len(observed) == 2:
            check_evidence_context([{'role': 'system', 'content': 'x'*max_context_bytes}],
                                   max_bytes=max_context_bytes, phase=context_phase)
        return json.dumps(next(values))
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await run(chat, budget=budget)
    assert held.value.code == 'scope_author_context_limit'
    assert observed == [(1048576, 'scope_author'), (1048576, 'scope_author')]
    assert budget.schema_repairs == 0
    assert budget.scope_contract is None


@pytest.mark.asyncio
async def test_manifest_phase_delivers_context_limit_to_transport():
    from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
    from tests.unit.test_351_environment_discovery import PATH
    from tests.unit.test_353_discovery_repair import candidate, run

    values = iter([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()}])
    phases = []
    async def chat(messages, *, response_schema, max_context_bytes, context_phase):
        phases.append(context_phase)
        assert max_context_bytes == 1048576
        if len(phases) == 2:
            raise EnvironmentDiscoveryError('synthetic transport context hold', code=context_phase+'_context_limit')
        return json.dumps(next(values))
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await run(chat, budget=budget)
    assert held.value.code == 'discovery_context_limit'
    assert phases == ['discovery', 'discovery']
    assert budget.schema_repairs == 0


@pytest.mark.asyncio
async def test_legacy_injected_chat_cannot_exceed_explicit_context_bound():
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_response_schema import invoke_chat

    async def legacy(messages):
        pytest.fail('oversized context reached legacy injected transport')
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await invoke_chat(legacy, [{'role': 'user', 'content': 'x'*200}], schema={},
                          max_context_bytes=100, context_phase='discovery')
    assert held.value.code == 'discovery_context_limit'
