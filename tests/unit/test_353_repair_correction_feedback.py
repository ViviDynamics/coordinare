"""Corrections a repair author can act on (Dettmore e2e-71: nine rejected replies held the attempt)."""
from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.models import (
    EnvironmentManifestProposal,
    validate_local_credentials,
)
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from coordinare.services.environment_admission import ALLOWED_KEYS_GUIDANCE, _validation_feedback
from coordinare.services.environment_capabilities import SERVICE_START_GUIDANCE
from coordinare.services.environment_discovery import _settled_envelope


def _command(identifier: str, script: str) -> dict:
    return {'id': identifier, 'argv': ['bash', '-c', script], 'cwd': '{repo}', 'env': {}, 'depends_on': [],
            'timeout_seconds': 60.0, 'required': True, 'scope': []}


def _proposal(health: str, **extra) -> dict:
    return {'requirements': [], 'install': [], 'checks': [_command('c-smoke', 'true')], 'empty_reason': 'none',
            'activation': {'path': ['{cache}/bin'], 'env': {}},
            'services': [{'id': 'db', 'start': _command('db-start', 'exec db-server'),
                          'health': _command('db-health', health), 'stop': _command('db-stop', 'true'),
                          'required': True}],
            'local_credentials': [{'name': 'DB_PASSWORD', 'service': 'db', 'purpose': 'database password'}], **extra}


def test_an_unknown_key_lists_the_schema_keys_without_echoing_it():
    with pytest.raises(ValidationError) as caught:
        EnvironmentManifestProposal.model_validate(_proposal('true', services_note='private note'))
    [feedback] = [item for item in _validation_feedback(caught.value) if item['code'] == 'extra_forbidden']
    assert feedback['location'] == ['*']
    assert 'services' in feedback['allowed_keys'] and 'activation' in feedback['allowed_keys']
    assert feedback['guidance'].endswith(ALLOWED_KEYS_GUIDANCE)
    assert 'services_note' not in json.dumps(feedback) and 'private note' not in json.dumps(feedback)


def test_an_unknown_nested_key_lists_that_records_keys():
    data = _proposal('true')
    data['services'][0]['restart_note'] = 'x'
    with pytest.raises(ValidationError) as caught:
        EnvironmentManifestProposal.model_validate(data)
    [feedback] = [item for item in _validation_feedback(caught.value) if item['code'] == 'extra_forbidden']
    assert feedback['location'] == ['services', 0, '*']
    assert feedback['allowed_keys'] == ['health', 'id', 'init', 'required', 'start', 'stop']


@pytest.mark.parametrize('tool', [None, '', 'repair_proposal'])
def test_a_complete_declaration_drops_a_leftover_tool_key_and_hoists_responses(tool):
    reply = {'manifest': {'install': [], 'finding_responses': [{'finding_id': 'F1'}]}, 'tool': tool}
    settled = _settled_envelope(reply, 'manifest', discovery=True)
    assert settled == {'manifest': {'install': []}, 'finding_responses': [{'finding_id': 'F1'}]}
    assert reply['manifest']['finding_responses'] == [{'finding_id': 'F1'}]  # the reply itself is not mutated


def test_a_real_correction_or_navigation_is_left_alone():
    patch = {'tool': 'repair_proposal', 'changes': [{'path': ['manifest'], 'value_json': '{}'}]}
    assert _settled_envelope(patch, 'manifest', discovery=True) == patch
    read = {'tool': 'read_file', 'path': 'README.md'}
    assert _settled_envelope(read, 'manifest', discovery=True) == read
    named = {'manifest': {}, 'tool': 'read_file'}
    assert _settled_envelope(named, 'manifest', discovery=True) == named
    both = {'manifest': {'finding_responses': [1]}, 'finding_responses': [2]}
    assert _settled_envelope(both, 'manifest', discovery=True) == both
    review = {'assessment': {}, 'tool': None}
    assert _settled_envelope(review, 'assessment', discovery=False) == review


def test_a_part_is_held_to_the_credential_rules_its_records_can_break():
    proposal = EnvironmentManifestProposal.model_validate(
        _proposal('PGPASSWORD=${DB_PASSWORD} {cache}/deps/bin/psql -c "select 1"'))
    service = proposal.services[0]
    with pytest.raises(PydanticCustomError) as caught:
        validate_local_credentials(proposal.local_credentials, proposal.services, proposal.install, proposal.activation,
                                   [service.start, service.health, service.stop], complete=False)
    assert caught.value.type == 'local_credential_cache_data'
    clean = EnvironmentManifestProposal.model_validate(_proposal('true'))
    service = clean.services[0]
    # Use of each credential is judged on the complete manifest, where the checks are present.
    validate_local_credentials(clean.local_credentials, clean.services, clean.install, clean.activation,
                               [service.start, service.health, service.stop], complete=False)
    with pytest.raises(PydanticCustomError) as unused:
        validate_local_credentials(clean.local_credentials, clean.services, clean.install, clean.activation,
                                   [service.start, service.health, service.stop])
    assert unused.value.type == 'local_credential_unused'


def test_service_lifecycle_guidance_states_start_and_health_semantics():
    assert 'init runs before its start' in SERVICE_START_GUIDANCE
    assert 'passes as soon as it is spawned' in SERVICE_START_GUIDANCE
    assert 'never only until a TCP port' in SERVICE_START_GUIDANCE


def test_a_changed_executed_command_is_corrected_with_retirement_guidance_and_falls_back():
    from types import SimpleNamespace

    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_delta import DELTA_FALLBACK
    from coordinare.services.environment_discovery import _declaration_feedback
    from coordinare.services.environment_obligations import (
        EXECUTION_COMMAND_CHANGED,
        REPAIR_CORRECTABLE,
        RETIRE_FAILED_CHECK,
    )

    # Dettmore e2e-73: a delta author rewrote three failed checks in place and the attempt held with no correction.
    assert EXECUTION_COMMAND_CHANGED in REPAIR_CORRECTABLE and EXECUTION_COMMAND_CHANGED in DELTA_FALLBACK
    error = EnvironmentDiscoveryError('required executable command c-lint changed after project execution',
                                      code=EXECUTION_COMMAND_CHANGED)
    [feedback] = _declaration_feedback(error, SimpleNamespace(), SimpleNamespace(retirable_checks={'c-lint'}))
    assert feedback['code'] == EXECUTION_COMMAND_CHANGED and feedback['guidance'].endswith(RETIRE_FAILED_CHECK)
    [plain] = _declaration_feedback(error, SimpleNamespace(), SimpleNamespace(retirable_checks=set()))
    assert RETIRE_FAILED_CHECK not in plain['guidance']


def test_a_command_failing_in_two_executions_must_change_method():
    """Dettmore e2e-75: install-ruby failed five times, each repair a variant of copying ruby out of an image."""
    from types import SimpleNamespace

    from coordinare.services.environment_discovery import (
        CHANGE_METHOD_INSTRUCTION,
        _attempt_history,
    )

    once = SimpleNamespace(execution_attempt_history=[{'attempt': 1, 'failed_ids': ['install-ruby']}])
    assert 'persistent_failure_ids' not in _attempt_history(once)
    twice = SimpleNamespace(execution_attempt_history=[
        {'attempt': 1, 'failed_ids': ['install-ruby', 'c-node']},
        {'attempt': 2, 'failed_ids': ['install-ruby']}])
    context = _attempt_history(twice)
    assert context['persistent_failure_ids'] == ['install-ruby']
    assert context['change_method'] == CHANGE_METHOD_INSTRUCTION
    assert 'not a change of method' in CHANGE_METHOD_INSTRUCTION


def test_runtime_guidance_forbids_copying_tools_out_of_images_and_names_no_stack():
    import re

    from coordinare.services import environment_capabilities

    source = open(environment_capabilities.__file__).read()  # noqa: SIM115
    start = source.index('"Never copy a runtime')
    rule = ''.join(re.findall(r'"([^"]*)"', source[start:source.index('"An install', start)]))
    assert 'Never copy a runtime, compiler or other tool out of a container image' in rule
    assert "language's own installer or version manager" in rule
    for stack in ('ruby', 'rbenv', 'rails', 'python', 'pyenv', 'node', 'nvm'):
        assert stack not in rule.lower()
